#!/usr/bin/env python3
"""MARADMIN checker -> OpenAI summary -> Slack (single summary mode)

MARADMIN alert script with a single, consistent summarization lens.

Summary behavior
- Always summarize as a Marine cyberspace warfare officer would.
- Keep key dates, deadlines, eligibility, and ineligibility details.
- Include a short recommendation.
- Do not list names; tell the reader to open the MARADMIN for names.

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
from typing import Any, Dict, List, Optional, Set

import feedparser
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from openai import OpenAI


DEFAULT_MODEL = "gpt-5-mini"
DEFAULT_STATE_FILE = ".maradmin_state.json"

DEFAULT_MARADMIN_FEED_URL = (
    "https://www.marines.mil/DesktopModules/ArticleCS/RSS.ashx"
    "?ContentType=6&Site=481&category=14336&max=10"
)

SLACK_MAX_CHARS = 35000  # buffer under Slack's max


# ----------------------------
# Summary behavior
# ----------------------------

SUMMARY_BULLETS = 5

# Regex helpers
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
# OpenAI summary
# ----------------------------

def extract_maradmin_number(title: str, body: str) -> Optional[str]:
    m = MARADMIN_NUM_RE.search(f"{title}\n{body}")
    return m.group(1) if m else None


def env_or_default(name: str, default: str) -> str:
    value = os.getenv(name, '').strip()
    return value if value else default


def format_prompt(template: str, **kwargs: Any) -> str:
    try:
        return template.format(**kwargs)
    except Exception:
        return template


def build_llm_instructions(bullets: int) -> str:
    base_default = (
        "You summarize USMC MARADMINS for a Marine cyberspace warfare officer.\n"
        "Output ONLY bullet points (no headings, no intro).\n"
        "Do NOT invent details; use only the provided text. If unknown, say 'Not stated'.\n"
        "Keep bullets tight: max 2 sentences.\n"
        "Do NOT omit dates/deadlines, eligibility, or ineligibility details when stated.\n"
        "Focus on: concrete dates/deadlines, required actions, who is affected, and who is eligible or ineligible.\n"
        "Include a final bullet starting with 'Recommendation:' tailored to a Marine Corps cyberspace warfare officer.\n"
    )
    base = env_or_default('MARADMIN_PROMPT_BASE', base_default)

    prompt_default = (
        base
        + f"Provide 3-{bullets} bullets total.\n"
        "If the MARADMIN lists names, do NOT summarize names; tell the reader to open the MARADMIN for names.\n"
    )
    return format_prompt(env_or_default('MARADMIN_PROMPT_STANDARD', prompt_default), bullets=bullets)


def summarize_maradmin(
    client: OpenAI,
    model: str,
    title: str,
    link: str,
    published: str,
    maradmin_text: str,
    bullets: int,
) -> List[str]:
    instructions = build_llm_instructions(bullets=bullets)

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
        s = re.sub(r"^[\-\u2022\*]+\s*", "", s)  # strip leading '-', '-', '*'
        if s:
            lines.append(s)

    if not lines:
        lines = ["No extractable summary produced from the available text."]

    # Soft cap: do not exceed the requested max
    return lines[: max(1, bullets)]


# ----------------------------
# Slack formatting
# ----------------------------

def entry_label(maradmin_number: Optional[str]) -> str:
    num = maradmin_number or "MARADMIN"
    return f"[MARADMIN] {num}"


def build_slack_message(new_entries: List[Dict[str, Any]], summaries: Dict[str, Dict[str, Any]]) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    header = f"*New MARADMINS detected* ({len(new_entries)}) - {stamp}\n"

    parts: List[str] = [header]
    for e in new_entries:
        title = e.get("title", "MARADMIN")
        link = e.get("link", "")
        published = e.get("published", "")
        nid = normalize_id(e)

        info = summaries.get(nid, {})
        maradmin_number = info.get("maradmin_number")

        label = entry_label(maradmin_number=maradmin_number)
        parts.append(f"*<{link}|{title}>*  _(Published: {published})_\n_{label}_")

        bullets = info.get("bullets", [])
        for b in bullets:
            parts.append(f"- {b}")

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
    p = argparse.ArgumentParser(description="MARADMIN alert with a single summary mode")
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

            bullets = SUMMARY_BULLETS

            if args.show_raw:
                print(f"\n--- {title} ---")
                print(f"Link: {link}")
                print(f"MARADMIN: {maradmin_number or 'Not stated'}")
                print(maradmin_text[:4000])
                summaries[nid] = {
                    "maradmin_number": maradmin_number,
                    "bullets": ["(show-raw enabled; not summarized)", "Open the link to read."],
                }
            else:
                bullet_lines = summarize_maradmin(
                    client=client,  # type: ignore[arg-type]
                    model=model,
                    title=title,
                    link=link,
                    published=published,
                    maradmin_text=maradmin_text,
                    bullets=bullets,
                )
                summaries[nid] = {
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
                    bullets = SUMMARY_BULLETS

                    if args.show_raw:
                        summaries[nid] = {
                            "maradmin_number": maradmin_number,
                            "bullets": ["(show-raw enabled; using RSS summary)", "Open the link to read."],
                        }
                    else:
                        bullet_lines = summarize_maradmin(
                            client=client,  # type: ignore[arg-type]
                            model=model,
                            title=title,
                            link=link,
                            published=published,
                            maradmin_text=maradmin_text,
                            bullets=bullets,
                        )
                        # If we had to fall back, add a quiet note only when useful.
                        if ex.response is not None and ex.response.status_code == 403:
                            bullet_lines = bullet_lines[:]
                            bullet_lines.append("(Note: full text fetch blocked; using RSS excerpt - open link for full details.)")
                        summaries[nid] = {
                            "maradmin_number": maradmin_number,
                            "bullets": bullet_lines,
                        }
                except Exception:
                    summaries[nid] = {
                        "maradmin_number": None,
                        "bullets": ["Open the link to read this MARADMIN."],
                    }
            else:
                summaries[nid] = {
                    "maradmin_number": None,
                    "bullets": ["Open the link to read this MARADMIN."],
                }
            seen_ids.add(nid)

        except Exception:
            # Keep Slack clean - no stack traces or noisy errors.
            summaries[nid] = {
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
