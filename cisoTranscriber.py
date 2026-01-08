#!/usr/bin/env python3
"""
ciso_headlines_daily.py

Checks Libsyn each morning for a new CISO Series Cybersecurity Headlines MP3,
downloads it, transcribes it via OpenAI, then summarizes it.

Requires:
  pip install openai requests python-dotenv
Env:
  export OPENAI_API_KEY="..."
  export OPENAI_MODEL="gpt-4o-mini"  # optional
"""

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv
from openai import OpenAI


LIBSYN_BASE = "https://traffic.libsyn.com/secure/cisoseries"

FILENAME_PATTERNS = [
    "CSH_{yyyymmdd}.mp3",     # CSH_20260107.mp3
    "CSH-{yyyy}-{mm}-{dd}.mp3" # CSH-2026-01-02.mp3
]

DEFAULT_STATE_PATH = Path("ciso_state.json")
DEFAULT_OUT_DIR = Path("ciso_downloads")
DEFAULT_MODEL = "gpt-4o-mini"


def build_candidate_urls(day: dt.date) -> list[str]:
    yyyymmdd = day.strftime("%Y%m%d")
    yyyy = day.strftime("%Y")
    mm = day.strftime("%m")
    dd = day.strftime("%d")

    urls = []
    for pat in FILENAME_PATTERNS:
        fname = pat.format(yyyymmdd=yyyymmdd, yyyy=yyyy, mm=mm, dd=dd)
        urls.append(f"{LIBSYN_BASE}/{fname}")
    return urls


def url_exists(url: str, timeout: int = 20) -> bool:
    try:
        r = requests.head(url, allow_redirects=True, timeout=timeout)
        # Some hosts may not support HEAD reliably; treat 405 as "try GET".
        if r.status_code == 405:
            r = requests.get(url, stream=True, timeout=timeout)
            return r.status_code == 200
        return r.status_code == 200
    except requests.RequestException:
        return False


def choose_available_audio_url(day: dt.date) -> str | None:
    for url in build_candidate_urls(day):
        if url_exists(url):
            return url
    return None


def load_state(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {"processed": {}}
    return {"processed": {}}


def save_state(path: Path, state: dict) -> None:
    path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def download_audio(url: str, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with requests.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        with open(out_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)


def transcribe_audio(client: OpenAI, audio_path: Path) -> str:
    # OpenAI Audio API supports gpt-4o-mini-transcribe, gpt-4o-transcribe, whisper-1, etc. :contentReference[oaicite:2]{index=2}
    with open(audio_path, "rb") as f:
        text = client.audio.transcriptions.create(
            model="gpt-4o-mini-transcribe",
            file=f,
            response_format="text",
        )
    return text


def summarize_transcript(client: OpenAI, model: str, transcript: str) -> str:
    # Responses API is recommended for new projects. :contentReference[oaicite:3]{index=3}
    resp = client.responses.create(
        model=model,
        input=[
            {"role": "system", "content": "You summarize cybersecurity podcast transcripts accurately and concisely."},
            {"role": "user", "content": (
                "Summarize this episode as:\n"
                "1) 8-12 bullets (each starts with a bold headline)\n"
                "2) 3 key takeaways\n"
                "3) Any action items for a security team\n\n"
                f"TRANSCRIPT:\n{transcript}"
            )},
        ],
    )
    return resp.output_text


def main():
    load_dotenv()
    ap = argparse.ArgumentParser()
    ap.add_argument("--days-back", type=int, default=2,
                    help="Check today, and up to N days back (default 2). Useful if they post late.")
    ap.add_argument("--state", type=Path, default=DEFAULT_STATE_PATH)
    ap.add_argument("--outdir", type=Path, default=DEFAULT_OUT_DIR)
    ap.add_argument("--dry-run", action="store_true", help="Only check URL availability; do not download/transcribe.")
    args = ap.parse_args()

    state = load_state(args.state)
    processed = state.setdefault("processed", {})

    # Use local date; if you run via cron in the morning, this matches your timezone.
    today = dt.date.today()

    chosen_day = None
    chosen_url = None
    for delta in range(0, args.days_back + 1):
        day = today - dt.timedelta(days=delta)
        url = choose_available_audio_url(day)
        if url:
            # Skip if already processed
            if processed.get(url):
                continue
            chosen_day = day
            chosen_url = url
            break

    if not chosen_url:
        print(f"No new episode found (checked {args.days_back + 1} day(s)).")
        return

    print(f"Found new episode for {chosen_day.isoformat()}: {chosen_url}")

    if args.dry_run:
        return

    openai_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not openai_key:
        print("Missing OPENAI_API_KEY.", file=sys.stderr)
        return 2

    model = os.getenv("OPENAI_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
    client = OpenAI(api_key=openai_key)

    # Pick a stable local filename based on the day we found
    audio_path = args.outdir / f"CSH_{chosen_day.strftime('%Y%m%d')}.mp3"
    transcript_path = args.outdir / f"CSH_{chosen_day.strftime('%Y%m%d')}.transcript.txt"
    summary_path = args.outdir / f"CSH_{chosen_day.strftime('%Y%m%d')}.summary.md"

    print(f"Downloading -> {audio_path}")
    download_audio(chosen_url, audio_path)

    print("Transcribing...")
    transcript = transcribe_audio(client, audio_path)
    transcript_path.write_text(transcript, encoding="utf-8")

    print("Summarizing...")
    summary = summarize_transcript(client, model, transcript)
    summary_path.write_text(summary, encoding="utf-8")

    processed[chosen_url] = {
        "date": chosen_day.isoformat(),
        "audio_path": str(audio_path),
        "transcript_path": str(transcript_path),
        "summary_path": str(summary_path),
        "processed_at": dt.datetime.now().isoformat(timespec="seconds"),
    }
    save_state(args.state, state)

    print("\nDone.")
    print(f"Transcript: {transcript_path}")
    print(f"Summary:    {summary_path}")


if __name__ == "__main__":
    main()
