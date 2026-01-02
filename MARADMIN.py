#!/usr/bin/env python3
"""MARADMIN checker -> OpenAI summary -> Slack (refined rules)

This is your MARADMIN alert script, upgraded with the refinements we developed.

Summary behavior
- Promotion lists / selected lists / name lists:
    * HIGH priority
    * 1–3 bullets max
    * No name roll-ups
    * Explicit: READ ASAP — name list inside
- Board dates / schedule messages:
    * One-liner + key dates only
- Board results:
    * 1–2 bullets, tell you to read for names
- 17XX / your MOS focus (1701/1702/1710/1720/1721):
    * Full actionable summary (deadlines, eligibility, actions)
- Not 17XX but relevant to your interests (AI/cyber/space/innovation):
    * 1–3 bullets, tagged FYI—Not 17XX
- Everything else:
    * 1 bullet + link

Feeds / state
- Pulls Marines.mil Messages RSS feed
- Detects new items since last run
- Sends ONE Slack message (chunked if needed)
- Persists seen IDs in a local JSON state file

Env:
  OPENAI_API_KEY=...
  SLACK_WEBHOOK_URL=...
  OPENAI_MODEL=gpt-4o-mini
  MARADMIN_FEED_URL=... (optional)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

import feedparser
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from openai import OpenAI


DEFAULT_MODEL = "gpt-4o-mini"
DEFAULT_STATE_FILE = ".maradmin_state.json"

DEFAULT_MARADMIN_FEED_URL = (
    "https://www.marines.mil/DesktopModules/ArticleCS/RSS.ashx"
    "?ContentType=6&Site=481&category=14336&max=10"
)

SLACK_MAX_CHARS = 35000  # buffer under Slack's max


# ----------------------------
# Your refined priorities
# ----------------------------

# Your “important MOSs”
HIGH_MOS: Set[str] = {"1701", "1702", "1710", "1720", "1721"}

# Your priority interest topics (allow short FYI summaries even if not 17XX)
PRIORITY_TOPICS = [
    # AI/ML
    "artificial intelligence",
    "ai",
    "machine learning",
    "ml",
    "llm",
    "data science",
    "data engineering",
    # Cyberspace/Cybersecurity
    "cyberspace",
    "cyber",
    "cybersecurity",
    "zero trust",
    "rmf",
    "ato",
    "dodin",
    "uscybercom",
    "marforcyber",
    "jfhq-dodin",
    "cmf",
    "oco",
    "dco",
    # Space
    "space",
    "satcom",
    "pnt",
    # Innovation
    "innovation",
    "experimentation",
    "pilot",
    "modernization",
    "software factory",
]

# Keyword buckets for classification
KW_PROMOTION_LIST = [
    "officer promotions",
    "enlisted promotions",
    "promotion authority",
    "selected for promotion",
    "promotion selection",
    "promotion list",
    "approved for promotion",
    "to the grade of",
    "promotions for",
]

KW_RESULTS = [
    "results",
    "selection list",
    "selected list",
    "board results",
    "approved selection",
]

KW_BOARD_SCHEDULE = [
    "promotion selection boards",
    "selection boards",
    "board will convene",
    "convening date",
    "board correspondence",
    "selection board",
    "board schedule",
    "projected",
]

# Regex helpers
MOS_RE = re.compile(r"\b(1[0-9]{3})\b")  # 4-digit MOS like 1721, 2651
MARADMIN_NUM_RE = re.compile(r"\bMARADMIN\s+(\d{1,4}/\d{2})\b", re.IGNORECASE)


# ----------------------------
# Utilities
# ----------------------------

def utc_now_iso_z() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def load_state(path: str) -> Dict[str, Any]:
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_state(path: str, state: Dict[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def fetch_rss_entries(feed_url: str) -> List[Dict[str, Any]]:
    feed = feedparser.parse(feed_url)
    if getattr(feed, "bozo", False):
        raise RuntimeError(f"RSS parse error: {getattr(feed, 'bozo_exception', 'unknown')}")

    entries: List[Dict[str, Any]] = []
    for e in feed.entries:
        guid = e.get("id") or e.get("guid") or e.get("link") or e.get("title")
        entries.append(
            {
                "guid": (guid or "").strip(),
                "title": (e.get("title", "") or "").strip(),
                "link": (e.get("link", "") or "").strip(),
                "summary": ((e.get("summary", "") or e.get("description", "")) or "").strip(),
                "published": ((e.get("published", "") or e.get("updated", "")) or "").strip(),
            }
        )
    return entries


def normalize_id(entry: Dict[str, Any]) -> str:
    return (entry.get("guid") or entry.get("link") or entry.get("title") or "").strip()


def find_new_entries(entries: List[Dict[str, Any]], seen_ids: Set[str]) -> List[Dict[str, Any]]:
    new: List[Dict[str, Any]] = []
    for e in entries:
        nid = normalize_id(e)
        if nid and nid not in seen_ids:
            new.append(e)
    return new


def http_get(url: str, timeout: int = 25) -> str:
    """Fetch a Marines.mil page.

    Marines.mil can return HTTP 403 to non-browser requests. To reduce that,
    we send a more complete, browser-like header set and use a Session.

    If the site still returns 403, the caller should fall back to using the
    RSS entry's summary text (which often contains the full message body).
    """

    session = requests.Session()

    # A realistic desktop Chrome UA and headers (helps with basic WAF rules).
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Referer": "https://www.marines.mil/",
        "DNT": "1",
        "Upgrade-Insecure-Requests": "1",
        # These are harmless if ignored; some WAFs look for them.
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-User": "?1",
    }

    r = session.get(url, headers=headers, timeout=timeout, allow_redirects=True)
    r.raise_for_status()
    return r.text


def looks_like_full_message(text: str) -> bool:
    """Heuristic: RSS summaries sometimes contain the full MARADMIN/ALMAR text."""
    t = (text or "").lower()
    if not t:
        return False
    return (
        "maradmin" in t
        or "msgid/genadmin" in t
        or "r " in t  # e.g., leading "R 301230Z DEC 25" lines
    )


def clean_rss_summary(summary_html: str) -> str:
    """RSS summaries are often HTML; convert to plain text and normalize."""
    if not summary_html:
        return ""
    soup = BeautifulSoup(summary_html, "html.parser")
    text = soup.get_text("\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text


def extract_message_text(html: str) -> str:
    """Extract readable MARADMIN text from Marines.mil "Messages Display" page."""
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text("\n")
    text = re.sub(r"\n{3,}", "\n\n", text).strip()

    markers = [
        r"\bMARADMINS?\s*:\s*\d+/\d+\b",
        r"\bMARADMIN\s+\d+/\d+\b",
        r"\bMSGID/GENADMIN\b",
    ]

    start_idx = None
    for pat in markers:
        m = re.search(pat, text, flags=re.IGNORECASE)
        if m:
            start_idx = m.start()
            break

    if start_idx is None:
        return text[:12000]

    return text[start_idx : start_idx + 20000].strip()


# ----------------------------
# Classification + mode selection
# ----------------------------

def contains_any(text: str, phrases: List[str]) -> bool:
    t = (text or "").lower()
    return any(p.lower() in t for p in phrases)


def extract_mos_codes(text: str) -> Set[str]:
    return set(MOS_RE.findall(text or ""))


def mos_relevance(text: str) -> Tuple[bool, List[str]]:
    """Returns (is_17xx_or_high_mos, list_of_high_mos_hits)."""
    mos = extract_mos_codes(text)
    high_hits = sorted(mos.intersection(HIGH_MOS))
    any_17xx = any(m.startswith("17") for m in mos)
    return (bool(high_hits) or any_17xx), high_hits


def classify_maradmin(title: str, body: str) -> str:
    """Coarse classification."""
    text = f"{title}\n{body}".lower()

    # Promotion list / selected list / name list: prioritize + READ ASAP
    if contains_any(text, KW_PROMOTION_LIST) and (contains_any(text, KW_RESULTS) or "promot" in text):
        return "PROMOTION_LIST_READ_ASAP"

    # Board schedule / board correspondence
    if contains_any(text, KW_BOARD_SCHEDULE) and ("board" in text or "selection" in text):
        return "BOARD_DATES_ONE_LINER"

    # Results (non-promo-list)
    if contains_any(text, KW_RESULTS):
        return "RESULTS_BRIEF"

    return "GENERAL"


def choose_summary_mode(category: str, title: str, body: str) -> Dict[str, Any]:
    """Decide summary mode + bullet count based on your refined preferences."""
    text = f"{title}\n{body}"
    is_17xx, _high_hits = mos_relevance(text)
    is_priority_topic = contains_any(text, PRIORITY_TOPICS)

    if category == "PROMOTION_LIST_READ_ASAP":
        return {"mode": "read_asap", "bullets": 3}
    if category == "BOARD_DATES_ONE_LINER":
        return {"mode": "dates_only", "bullets": 14}
    if category == "RESULTS_BRIEF":
        return {"mode": "brief_results", "bullets": 2}

    if is_17xx:
        return {"mode": "full_17xx", "bullets": 6}
    if is_priority_topic:
        return {"mode": "fyi_not_17xx", "bullets": 3}
    return {"mode": "minimal", "bullets": 1}


def extract_maradmin_number(title: str, body: str) -> Optional[str]:
    m = MARADMIN_NUM_RE.search(f"{title}\n{body}")
    return m.group(1) if m else None


# ----------------------------
# OpenAI summary
# ----------------------------

def build_llm_instructions(mode: str, bullets: int) -> str:
    base = (
        "You summarize USMC MARADMINS for a Cyberspace Officer.\n"
        "Output ONLY bullet points (no headings, no intro).\n"
        "Do NOT invent details; use only the provided text. If unknown, say 'Not stated'.\n"
        "Keep bullets tight: 1 sentence where possible, max 2 sentences.\n"
        "Prefer concrete dates/deadlines and required actions.\n"
    )

    if mode == "read_asap":
        return (
            base
            + f"Provide 1–{bullets} bullets MAX.\n"
            "This is a PROMOTION/SELECTION LIST with names.\n"
            "- Do NOT summarize or list names.\n"
            "- MUST include 'READ ASAP — name list inside.'\n"
            "Focus on: what rank(s), what population (Active/AR/Reserve), what month/timeframe, and any admin notes.\n"
        )

    if mode == "dates_only":
        return (
			base
			+ 
            "This is a promotion selection board schedule / dates message.\n"
            "- First bullet: a one-sentence summary.\n"
            "- Remaining bullets: key dates only (board correspondence due dates and convening dates).\n"
            f"Provide up to {bullets} bullets total.\n"
            "No extra commentary.\n"
        )

    if mode == "brief_results":
        return (
            base
            + f"Provide 1–{bullets} bullets MAX.\n"
            "This is BOARD RESULTS.\n"
            "- Do NOT summarize names.\n"
            "- Tell the reader to open/read the MARADMIN for names.\n"
        )

    if mode == "fyi_not_17xx":
        return (
            base
            + f"Provide 1–{bullets} bullets MAX.\n"
            "Tag the first bullet with 'FYI—Not 17XX'.\n"
            "Focus on: what it is, who it applies to, and any deadline/timeline.\n"
        )

    if mode == "minimal":
        return base + "Provide exactly 1 bullet.\n"

    # full_17xx
    return (
        base
        + f"Provide 4–{bullets} bullets.\n"
        "This MARADMIN is relevant to 17XX / MOS 1701/1702/1710/1720/1721.\n"
        "If the MARADMIN lists multiple MOSs, only include details relevant to 17XX / those MOSs.\n"
        "Emphasize deadlines/timelines, eligibility, and required actions.\n"
    )


def summarize_maradmin(
    client: OpenAI,
    model: str,
    title: str,
    link: str,
    published: str,
    maradmin_text: str,
    mode: str,
    bullets: int,
) -> List[str]:
    instructions = build_llm_instructions(mode=mode, bullets=bullets)

    user_input = (
        f"Title: {title}\n"
        f"Link: {link}\n"
        f"Published: {published}\n\n"
        "MARADMIN text:\n"
        f"{maradmin_text}"
    )

    resp = client.responses.create(
        model=model,
        instructions=instructions,
        input=user_input,
        temperature=0.2,
    )

    out = (resp.output_text or "").strip()

    lines: List[str] = []
    for line in out.splitlines():
        s = line.strip()
        if not s:
            continue
        s = re.sub(r"^[\-\u2022\*]+\s*", "", s)  # strip leading '-', '•', '*'
        if s:
            lines.append(s)

    if not lines:
        lines = ["No extractable summary produced from the available text."]

    # Soft cap: do not exceed the requested max
    return lines[: max(1, bullets)]


# ----------------------------
# Slack formatting
# ----------------------------

def entry_label(mode: str, maradmin_number: Optional[str]) -> str:
    num = maradmin_number or "MARADMIN"
    if mode == "read_asap":
        return f"🚨 [PROMOTION LIST — READ ASAP] {num}"
    if mode == "dates_only":
        return f"[BOARD SCHEDULE] {num}"
    if mode == "brief_results":
        return f"[RESULTS — READ FOR NAMES] {num}"
    if mode == "full_17xx":
        return f"[17XX] {num}"
    if mode == "fyi_not_17xx":
        return f"[FYI—Not 17XX] {num}"
    return f"[ADMIN/LOW RELEVANCE] {num}"


def build_slack_message(new_entries: List[Dict[str, Any]], summaries: Dict[str, Dict[str, Any]]) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    header = f"*New MARADMINS detected* ({len(new_entries)}) — {stamp}\n"

    parts: List[str] = [header]
    for e in new_entries:
        title = e.get("title", "MARADMIN")
        link = e.get("link", "")
        published = e.get("published", "")
        nid = normalize_id(e)

        info = summaries.get(nid, {})
        mode = info.get("mode", "minimal")
        maradmin_number = info.get("maradmin_number")

        label = entry_label(mode=mode, maradmin_number=maradmin_number)
        parts.append(f"*<{link}|{title}>*  _(Published: {published})_\n_{label}_")

        bullets = info.get("bullets", [])
        for b in bullets:
            parts.append(f"• {b}")

        parts.append("")  # spacer line

    return "\n".join(parts).strip()


def chunk_for_slack(msg: str, max_chars: int = SLACK_MAX_CHARS) -> List[str]:
    if len(msg) <= max_chars:
        return [msg]

    lines = msg.splitlines(True)  # keep newlines
    chunks: List[str] = []
    buf = ""
    for line in lines:
        if len(buf) + len(line) > max_chars and buf:
            chunks.append(buf)
            buf = ""
        buf += line
    if buf:
        chunks.append(buf)
    return chunks


def post_to_slack(webhook_url: str, text: str) -> None:
    r = requests.post(webhook_url, json={"text": text}, timeout=20)
    if r.status_code >= 300:
        raise RuntimeError(f"Slack webhook error {r.status_code}: {r.text[:400]}")


# ----------------------------
# CLI + main
# ----------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="MARADMIN alert with refined summarization rules")
    p.add_argument("--feed-url", default=os.getenv("MARADMIN_FEED_URL", DEFAULT_MARADMIN_FEED_URL))
    p.add_argument("--model", default=os.getenv("OPENAI_MODEL", DEFAULT_MODEL))
    p.add_argument("--state-file", default=DEFAULT_STATE_FILE)
    p.add_argument("--max", type=int, default=10, help="Max RSS entries to process per run")
    p.add_argument("--dry-run", action="store_true", help="Do not post to Slack; print output")
    p.add_argument("--force", action="store_true", help="Treat all fetched entries as new")
    p.add_argument("--show-raw", action="store_true", help="Print parsed MARADMIN text for new items")
    return p.parse_args()


def main() -> int:
    load_dotenv()
    args = parse_args()

    feed_url = args.feed_url
    model = args.model
    state_path = args.state_file

    openai_key = os.getenv("OPENAI_API_KEY", "").strip()
    slack_webhook = os.getenv("SLACK_WEBHOOK_URL", "").strip()

    if not openai_key and not args.show_raw:
        print("ERROR: OPENAI_API_KEY is not set.", file=sys.stderr)
        return 2
    if not slack_webhook and (not args.dry_run) and (not args.show_raw):
        print("ERROR: SLACK_WEBHOOK_URL is not set (or use --dry-run).", file=sys.stderr)
        return 2

    state = load_state(state_path)
    # Backward compatible: accept different keys if you ever changed them
    seen_list = (
        state.get("seen_ids")
        or state.get("processed")
        or state.get("processed_urls")
        or state.get("seen")
        or []
    )
    seen_ids: Set[str] = set(seen_list if isinstance(seen_list, list) else [])

    entries = fetch_rss_entries(feed_url)[: max(1, args.max)]
    new_entries = entries if args.force else find_new_entries(entries, seen_ids)

    if not new_entries:
        # Update last_run and exit
        state["last_run_utc"] = utc_now_iso_z()
        state["seen_ids"] = sorted(seen_ids)
        save_state(state_path, state)
        return 0

    client = OpenAI(api_key=openai_key) if openai_key else None

    summaries: Dict[str, Dict[str, Any]] = {}
    for e in new_entries:
        nid = normalize_id(e)
        title = e.get("title", "MARADMIN")
        link = e.get("link", "")
        published = e.get("published", "")
        rss_summary = e.get("summary", "")

        try:
            # Prefer RSS summary if it already contains the full message text.
            maradmin_text = ""
            if looks_like_full_message(rss_summary):
                maradmin_text = clean_rss_summary(rss_summary)

            # If RSS summary isn't sufficient, fetch the article page.
            if not maradmin_text:
                html = http_get(link)
                maradmin_text = extract_message_text(html)

            maradmin_number = extract_maradmin_number(title, maradmin_text)

            category = classify_maradmin(title, maradmin_text)
            decision = choose_summary_mode(category, title, maradmin_text)
            mode = decision["mode"]
            bullets = int(decision["bullets"])

            if args.show_raw:
                print(f"\n--- {title} ---")
                print(f"Link: {link}")
                print(f"Mode: {mode} | Category: {category} | MARADMIN: {maradmin_number or 'Not stated'}")
                print(maradmin_text[:4000])
                summaries[nid] = {
                    "mode": mode,
                    "maradmin_number": maradmin_number,
                    "bullets": ["(show-raw enabled; not summarized)", f"Mode: {mode}", "Open the link to read."],
                }
            else:
                bullet_lines = summarize_maradmin(
                    client=client,  # type: ignore[arg-type]
                    model=model,
                    title=title,
                    link=link,
                    published=published,
                    maradmin_text=maradmin_text,
                    mode=mode,
                    bullets=bullets,
                )
                summaries[nid] = {
                    "mode": mode,
                    "maradmin_number": maradmin_number,
                    "bullets": bullet_lines,
                }

            seen_ids.add(nid)

        except requests.HTTPError as ex:
            # Common case: Marines.mil returns 403 to non-browser clients.
            # Fall back to the RSS summary if available; otherwise keep it clean.
            fallback = clean_rss_summary(rss_summary)
            if fallback:
                try:
                    maradmin_text = fallback
                    maradmin_number = extract_maradmin_number(title, maradmin_text)
                    category = classify_maradmin(title, maradmin_text)
                    decision = choose_summary_mode(category, title, maradmin_text)
                    mode = decision["mode"]
                    bullets = int(decision["bullets"])

                    if args.show_raw:
                        summaries[nid] = {
                            "mode": mode,
                            "maradmin_number": maradmin_number,
                            "bullets": ["(show-raw enabled; using RSS summary)", f"Mode: {mode}", "Open the link to read."],
                        }
                    else:
                        bullet_lines = summarize_maradmin(
                            client=client,  # type: ignore[arg-type]
                            model=model,
                            title=title,
                            link=link,
                            published=published,
                            maradmin_text=maradmin_text,
                            mode=mode,
                            bullets=bullets,
                        )
                        # If we had to fall back, add a quiet note only when useful.
                        if ex.response is not None and ex.response.status_code == 403:
                            bullet_lines = bullet_lines[:]
                            bullet_lines.append("(Note: full text fetch blocked; using RSS excerpt — open link for full details.)")
                        summaries[nid] = {
                            "mode": mode,
                            "maradmin_number": maradmin_number,
                            "bullets": bullet_lines,
                        }
                except Exception:
                    summaries[nid] = {
                        "mode": "minimal",
                        "maradmin_number": None,
                        "bullets": ["Open the link to read this MARADMIN."],
                    }
            else:
                summaries[nid] = {
                    "mode": "minimal",
                    "maradmin_number": None,
                    "bullets": ["Open the link to read this MARADMIN."],
                }
            seen_ids.add(nid)

        except Exception:
            # Keep Slack clean—no stack traces or noisy errors.
            summaries[nid] = {
                "mode": "minimal",
                "maradmin_number": None,
                "bullets": ["Open the link to read this MARADMIN."],
            }
            seen_ids.add(nid)

    message = build_slack_message(new_entries, summaries)
    chunks = chunk_for_slack(message)

    if args.dry_run or args.show_raw:
        for c in chunks:
            print(c)
            print("\n" + "=" * 80 + "\n")
    else:
        for c in chunks:
            post_to_slack(slack_webhook, c)

    state["seen_ids"] = sorted(seen_ids)
    state["last_run_utc"] = utc_now_iso_z()
    save_state(state_path, state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
