#!/usr/bin/env python3
"""
News checker -> OpenAI summary -> Slack

Feeds:
1) CISO Series Cyber Security Headlines (Libsyn RSS)
   - ALWAYS summarizes the FIRST entry (daily roll-up)
2) RealClearDefense (RCD)
   - ONLY processes entries published within a window:
       today OR yesterday (America/New_York) by default
   - ONLY processes entries matching interest keywords
   - Tracks seen_ids in JSON so re-runs don't repost

State:
- news_state.json
- Per-feed state: state["feeds"]["ciso"], state["feeds"]["rcd"]

Env (.env recommended):
  OPENAI_API_KEY=...
  SLACK_WEBHOOK_URL=...
  OPENAI_MODEL=gpt-4o-mini
  CISO_FEED_URL=... (optional) default Libsyn URL
  RCD_FEED_URL=...  (optional) default https://www.realcleardefense.com/index.xml

Deps:
  pip install openai feedparser python-dotenv requests
  (Optional on some Windows installs) pip install tzdata
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Optional, Tuple

import feedparser
import requests
from dotenv import load_dotenv
from openai import OpenAI

# ----------------------------
# Defaults / Config
# ----------------------------
DEFAULT_MODEL = "gpt-5.2-mini"
DEFAULT_STATE_FILE = "news_state.json"

DEFAULT_CISO_FEED_URL = "https://rss.libsyn.com/shows/289580/destinations/2260670.xml"
DEFAULT_RCD_FEED_URL = "https://www.realcleardefense.com/index.xml"

SLACK_MAX_CHARS = 35000

# Honor your timezone for "same day" logic
try:
    from zoneinfo import ZoneInfo  # py3.9+
    LOCAL_TZ = ZoneInfo("America/New_York")
except Exception:
    LOCAL_TZ = None


# ----------------------------
# Time helpers
# ----------------------------
def utc_now_iso_z() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def local_today_date() -> datetime.date:
    if LOCAL_TZ is None:
        return datetime.now(timezone.utc).date()
    return datetime.now(LOCAL_TZ).date()


def parse_datetime_any(raw: str) -> Optional[datetime]:
    """
    Parse common RSS/Atom date formats into tz-aware datetime (UTC).
    Supports RFC2822 and many ISO8601 variants.
    """
    if not raw:
        return None

    # RFC 2822 / RFC 5322
    try:
        dt = parsedate_to_datetime(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        pass

    # ISO 8601
    try:
        s = raw.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def entry_local_date(entry_obj: Any) -> Optional[datetime.date]:
    """
    Determine entry publish date in local TZ (America/New_York).
    Uses feedparser parsed structs when possible; else parses strings.
    """
    tz = LOCAL_TZ or timezone.utc

    st = entry_obj.get("published_parsed") or entry_obj.get("updated_parsed")
    if st:
        # feedparser struct_time; treat as UTC to be safe
        dt_utc = datetime(*st[:6], tzinfo=timezone.utc)
        return dt_utc.astimezone(tz).date()

    raw = entry_obj.get("published") or entry_obj.get("updated") or ""
    dt = parse_datetime_any(raw)
    if dt is None:
        return None
    return dt.astimezone(tz).date()


# ----------------------------
# State helpers
# ----------------------------
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


def ensure_state_shape(state: Dict[str, Any]) -> Dict[str, Any]:
    """
    Ensure per-feed state exists.
    Also migrates older single-list state (top-level seen_ids) into feeds.ciso if present.
    """
    state.setdefault("feeds", {})

    # optional migration support
    if "seen_ids" in state and "ciso" not in state["feeds"]:
        old = state.get("seen_ids", [])
        if isinstance(old, list):
            state["feeds"]["ciso"] = {"seen_ids": old}

    state["feeds"].setdefault("ciso", {"seen_ids": []})
    state["feeds"].setdefault("rcd", {"seen_ids": []})
    return state


# ----------------------------
# Slack helpers
# ----------------------------
def chunk_for_slack(msg: str, max_chars: int = SLACK_MAX_CHARS) -> List[str]:
    if len(msg) <= max_chars:
        return [msg]
    lines = msg.splitlines(True)
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


def post_to_slack(webhook_url: str, message: str) -> None:
    r = requests.post(webhook_url, json={"text": message}, timeout=20)
    if r.status_code >= 300:
        raise RuntimeError(f"Slack webhook error {r.status_code}: {r.text[:400]}")


# ----------------------------
# RSS fetch helpers (requests + UA)
# ----------------------------
def fetch_feed_entries(feed_url: str) -> List[Dict[str, Any]]:
    """
    Fetch RSS/Atom via requests + browser-ish UA to avoid blocks, parse with feedparser.
    Returns normalized entries: id, title, link, published, text, local_date (YYYY-MM-DD).
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    resp = requests.get(feed_url, headers=headers, timeout=25)
    resp.raise_for_status()

    feed = feedparser.parse(resp.content)

    if getattr(feed, "bozo", False) and not feed.entries:
        raise RuntimeError(f"RSS parse error: {getattr(feed, 'bozo_exception', 'unknown')}")

    out: List[Dict[str, Any]] = []
    for e in feed.entries:
        entry_id = (e.get("id") or e.get("guid") or e.get("link") or e.get("title") or "").strip()
        title = (e.get("title") or "").strip()
        link = (e.get("link") or "").strip()
        published = (e.get("published") or e.get("updated") or "").strip()

        text = (e.get("summary") or e.get("description") or "").strip()
        if not text:
            content = e.get("content")
            if isinstance(content, list) and content:
                text = (content[0].get("value") or "").strip()

        ld = entry_local_date(e)
        out.append(
            {
                "id": entry_id,
                "title": title,
                "link": link,
                "published": published,
                "text": text,
                "local_date": ld.isoformat() if ld else "",
            }
        )
    return out


def normalize_id(entry: Dict[str, Any]) -> str:
    return (entry.get("id") or entry.get("link") or entry.get("title") or "").strip()


# ----------------------------
# RealClearDefense interest filtering
# ----------------------------
RCD_TOPIC_GROUPS: List[Tuple[str, re.Pattern]] = [
    ("USMC", re.compile(r"\b(usmc|marine corps|marines)\b", re.IGNORECASE)),
    ("CYBER", re.compile(r"\b(cyber|cyberspace|malware|ransomware|zero[- ]trust|dodin|cybercom|apt)\b", re.IGNORECASE)),
    ("SPACE", re.compile(r"\b(space|satellite|orbit|spacecom|space force|satcom|pnt)\b", re.IGNORECASE)),
    ("TECH", re.compile(r"\b(ai|artificial intelligence|machine learning|quantum|autonomous|unmanned|drone|uas|hypersonic|c4isr|electronic warfare|darpa|innovation)\b", re.IGNORECASE)),
    ("SEC", re.compile(r"\b(security|defense|threat|attack|espionage|intelligence)\b", re.IGNORECASE)),
]
RCD_INTEREST_RE = re.compile("|".join(p.pattern for _, p in RCD_TOPIC_GROUPS), re.IGNORECASE)


def rcd_is_interesting(title: str, text: str) -> bool:
    return bool(RCD_INTEREST_RE.search(f"{title}\n{text}"))


def rcd_tags(title: str, text: str) -> List[str]:
    blob = f"{title}\n{text}"
    tags = [name for name, pat in RCD_TOPIC_GROUPS if pat.search(blob)]
    return tags[:3]


def rcd_is_in_window(local_date_iso: str, days_back: int = 1) -> bool:
    """
    Allow items from today OR the previous `days_back` day(s) (local time).
    days_back=1 => today + yesterday.
    """
    if not local_date_iso:
        return False
    try:
        d = datetime.fromisoformat(local_date_iso).date()
    except Exception:
        return False
    today = local_today_date()
    delta_days = (today - d).days
    return 0 <= delta_days <= max(0, days_back)


# ----------------------------
# OpenAI summarizers
# ----------------------------
def summarize_ciso_rollup_to_bullets(
    client: OpenAI,
    model: str,
    episode: Dict[str, Any],
    max_bullets: int,
    sentences: int,
) -> str:
    max_bullets = max(1, max_bullets)
    sentences = max(1, sentences)
    instructions = (
        "You are a cyber news summarizer.\n"
        "Input is a daily roll-up containing multiple story blurbs + links (may contain HTML).\n"
        "Extract distinct stories and return ONLY Slack mrkdwn bullets.\n\n"
        "Format:\n"
        "- <URL|Title> - summary\n\n"
        "Rules:\n"
        f"- Limit to {max_bullets} bullets.\n"
        f"- Each bullet averages about {sentences} sentence(s).\n"
        "- Deduplicate repeated items.\n"
        "- Do NOT invent facts.\n"
    )

    user_input = (
        f"Episode title: {episode['title']}\n"
        f"Episode link: {episode['link']}\n"
        f"Published: {episode['published']}\n\n"
        "Roll-up text:\n"
        f"{episode['text']}"
    )

    resp = client.responses.create(model=model, instructions=instructions, input=user_input)
    out = (resp.output_text or "").strip()
    if not out:
        out = f"- <{episode['link']}|{episode['title']}> - (No roll-up text found.)"
    return out


def summarize_rcd_selected_entries(
    client: OpenAI,
    model: str,
    selected: List[Dict[str, Any]],
    bullets_per_article: int,
) -> str:
    bullets_per_article = max(5, min(bullets_per_article, 6))
    instructions = (
        "You summarize defense/security articles for a technically-minded reader.\n"
        "Return ONLY Slack mrkdwn bullets.\n\n"
        f"For EACH article, output exactly {bullets_per_article} bullets:\n"
        "1) - <URL|Title> - 1 sentence: what it is.\n"
        "2) - Why it matters - 2 sentence (impact/implication).\n"
        "3) (optional) - Watch-for - 2 sentence (trend/next step).\n\n"
        "Do NOT invent facts; use only provided title/snippet.\n"
    )

    parts = []
    for e in selected:
        parts.append(
            f"TAGS: {', '.join(e.get('tags', []))}\n"
            f"TITLE: {e['title']}\n"
            f"URL: {e['link']}\n"
            f"PUBLISHED: {e['published']}\n"
            f"SNIPPET:\n{e['text']}\n"
        )

    user_input = "ARTICLES:\n\n" + "\n---\n".join(parts)

    resp = client.responses.create(model=model, instructions=instructions, input=user_input)
    out = (resp.output_text or "").strip()
    if not out:
        out = "- (No RealClearDefense summary produced.)"
    return out


# ----------------------------
# CLI + main
# ----------------------------
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="News summarizer: CISO rollup + filtered RealClearDefense")
    p.add_argument("--state-file", default=DEFAULT_STATE_FILE)
    p.add_argument("--model", default=os.getenv("OPENAI_MODEL", DEFAULT_MODEL))

    p.add_argument("--dry-run", action="store_true", help="Print output; do not post to Slack")
    p.add_argument("--force", action="store_true", help="Ignore seen_ids (RCD still must be within window)")
    p.add_argument("--debug", action="store_true", help="Print debug counters")

    # CISO knobs
    p.add_argument("--ciso-max-bullets", type=int, default=12)
    p.add_argument("--ciso-sentences", type=int, default=2)

    # RCD knobs
    p.add_argument("--rcd-window-days", type=int, default=1, help="0=today only, 1=today+yesterday")
    p.add_argument("--rcd-max-items", type=int, default=5, help="Max selected RCD entries per run")
    p.add_argument("--rcd-bullets-per-article", type=int, default=2)

    return p.parse_args()


def main() -> int:
    load_dotenv()
    args = parse_args()

    openai_key = os.getenv("OPENAI_API_KEY", "").strip()
    slack_webhook = os.getenv("SLACK_WEBHOOK_URL", "").strip()

    if not openai_key:
        print("Missing OPENAI_API_KEY.", file=sys.stderr)
        return 2
    if not slack_webhook and not args.dry_run:
        print("Missing SLACK_WEBHOOK_URL (or use --dry-run).", file=sys.stderr)
        return 2

    ciso_url = os.getenv("CISO_FEED_URL", os.getenv("FEED_URL", DEFAULT_CISO_FEED_URL))
    rcd_url = os.getenv("RCD_FEED_URL", DEFAULT_RCD_FEED_URL)

    state = ensure_state_shape(load_state(args.state_file))
    state["last_run_utc"] = utc_now_iso_z()
    state["last_run_mode"] = "dry-run" if args.dry_run else "post"

    ciso_seen = set(state["feeds"]["ciso"].get("seen_ids", []))
    rcd_seen = set(state["feeds"]["rcd"].get("seen_ids", []))

    client = OpenAI(api_key=openai_key)
    sections: List[str] = []

    # ----------------------------
    # CISO (first entry only)
    # ----------------------------
    try:
        ciso_entries = fetch_feed_entries(ciso_url)
    except Exception as ex:
        print(f"[ERROR] CISO feed failed: {ex}", file=sys.stderr)
        ciso_entries = []

    if ciso_entries:
        ep = ciso_entries[0]
        ep_id = normalize_id(ep)

        state["feeds"]["ciso"]["last_seen_id"] = ep_id
        state["feeds"]["ciso"]["last_seen_title"] = ep["title"]
        state["feeds"]["ciso"]["last_seen_published"] = ep["published"]

        should_post = args.force or (ep_id not in ciso_seen)

        if args.debug:
            print(f"[DEBUG] CISO seen={ep_id in ciso_seen} should_post={should_post}", file=sys.stderr)

        if should_post or args.dry_run:
            bullets = summarize_ciso_rollup_to_bullets(
                client=client,
                model=args.model,
                episode={"title": ep["title"], "link": ep["link"], "published": ep["published"], "text": ep["text"]},
                max_bullets=args.ciso_max_bullets,
                sentences=args.ciso_sentences,
            )

            sections.append(
                f"*Cyber Security Headlines* - {ep['published']}\n<{ep['link']}|Episode link>\n\n{bullets}"
            )

            if not args.dry_run and ep_id:
                ciso_seen.add(ep_id)
                state["feeds"]["ciso"]["seen_ids"] = sorted(ciso_seen)
                state["feeds"]["ciso"]["last_posted_utc"] = utc_now_iso_z()

    # ----------------------------
    # RealClearDefense (window + interest + seen gate)
    # ----------------------------
    try:
        rcd_entries = fetch_feed_entries(rcd_url)
    except Exception as ex:
        print(f"[ERROR] RCD feed failed: {ex}", file=sys.stderr)
        rcd_entries = []

    today = local_today_date()
    if args.debug:
        print(f"[DEBUG] Local today={today.isoformat()} window_days={args.rcd_window_days}", file=sys.stderr)

    pipeline = {"total": len(rcd_entries), "in_window": 0, "new_in_window": 0, "interest_new_in_window": 0, "selected": 0}

    candidates: List[Dict[str, Any]] = []
    for e in rcd_entries:
        eid = normalize_id(e)
        if not eid:
            continue

        if not rcd_is_in_window(e.get("local_date", ""), days_back=args.rcd_window_days):
            continue
        pipeline["in_window"] += 1

        if (eid in rcd_seen) and (not args.force):
            continue
        pipeline["new_in_window"] += 1

        if not rcd_is_interesting(e["title"], e["text"]):
            continue
        pipeline["interest_new_in_window"] += 1

        e2 = dict(e)
        e2["id"] = eid
        e2["tags"] = rcd_tags(e["title"], e["text"])
        candidates.append(e2)

    candidates = candidates[: max(1, args.rcd_max_items)]
    pipeline["selected"] = len(candidates)

    state["feeds"]["rcd"]["last_scan_today_local"] = today.isoformat()
    state["feeds"]["rcd"]["last_pipeline_counts"] = pipeline
    state["feeds"]["rcd"]["last_scan_count"] = len(rcd_entries)

    if args.debug:
        print(f"[DEBUG] RCD pipeline: {pipeline}", file=sys.stderr)

    if candidates:
        rcd_bullets = summarize_rcd_selected_entries(
            client=client,
            model=args.model,
            selected=candidates,
            bullets_per_article=args.rcd_bullets_per_article,
        )

        tag_line = " / ".join(sorted({t for c in candidates for t in c.get("tags", [])})) or "Filtered"
        sections.append(
            f"*RealClearDefense (window: today+{args.rcd_window_days}d, filtered: {tag_line})* - {today.isoformat()}\n\n{rcd_bullets}"
        )

        if not args.dry_run:
            for e in candidates:
                rcd_seen.add(e["id"])
            state["feeds"]["rcd"]["seen_ids"] = sorted(rcd_seen)
            state["feeds"]["rcd"]["last_posted_utc"] = utc_now_iso_z()

    # ----------------------------
    # Post / print + save state
    # ----------------------------
    if sections:
        combined = ("\n\n" + ("-" * 30) + "\n\n").join(sections)
        for chunk in chunk_for_slack(combined):
            if args.dry_run:
                print(chunk)
            else:
                post_to_slack(slack_webhook, chunk)

    save_state(args.state_file, state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
