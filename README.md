# Stone_Slack_Alerts

Slack alerts for USMC MARADMIN messages and daily cyber/defense news summaries. The repo contains two Python scripts that fetch RSS feeds, summarize with OpenAI, and post to a Slack Incoming Webhook.

## What this repo does

- `MARADMIN.py` watches the Marines.mil MARADMIN RSS feed, applies MOS- and topic-based rules, summarizes new items, and posts a single Slack message per run.
- `News.py` summarizes the latest CISO Series Cyber Security Headlines rollup and filters RealClearDefense items by date and interest keywords before posting to Slack.

## Features

- OpenAI-powered summaries with tuned rules for 17XX/MOS relevance and board/promotion messages.
- Slack message chunking to stay under Slack limits.
- Local JSON state so you do not repost the same items every run.
- Optional dry-run modes to print output without posting.

## Requirements

- Python 3.9+ (3.11+ recommended)
- Slack Incoming Webhook URL
- OpenAI API key
- Internet access to RSS sources

Python dependencies (install via pip):

- `openai`
- `feedparser`
- `python-dotenv`
- `requests`
- `beautifulsoup4`
- `tzdata` (optional on some Windows installs)

## Setup

1. Create and activate a virtual environment (optional but recommended):

   ```powershell
   python -m venv venv
   .\venv\Scripts\Activate.ps1
   ```

2. Install dependencies:

   ```powershell
   pip install -r requirements.txt
   ```

3. Create a `.env` file in the repo root:

   ```dotenv
   OPENAI_API_KEY=your_openai_key
   SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...
   OPENAI_MODEL=gpt-4o-mini

   # Optional overrides
   MARADMIN_PROMPT_BASE="You summarize USMC MARADMINS for a cyber operations audience."
   MARADMIN_PROMPT_READ_ASAP="Provide 1-{bullets} bullets MAX..."
   MARADMIN_PROMPT_FULL_17XX="Provide 4-{bullets} bullets..."
   CISO_PROMPT_INSTRUCTIONS="You are a cyber news summarizer..."
   RCD_PROMPT_INSTRUCTIONS="You summarize defense/security articles..."

   MARADMIN_FEED_URL=https://www.marines.mil/DesktopModules/ArticleCS/RSS.ashx?ContentType=6&Site=481&category=14336&max=10
   CISO_FEED_URL=https://rss.libsyn.com/shows/289580/destinations/2260670.xml
   RCD_FEED_URL=https://www.realcleardefense.com/index.xml
   ```

## Running the scripts

### MARADMIN alerts

Dry-run (prints to console):

```powershell
python MARADMIN.py --dry-run
```

Useful options:

- `--max` max RSS entries per run (default: 10)
- `--force` treat all fetched entries as new
- `--show-raw` print parsed MARADMIN text instead of summaries
- `--state-file` path to state JSON (default: `.maradmin_state.json`)
- `--model` override OpenAI model (also via `OPENAI_MODEL`)

### News alerts

Dry-run (prints to console):

```powershell
python News.py --dry-run
```

Useful options:

- `--ciso-max-bullets` max bullets for CISO rollup
- `--ciso-sentences` sentences per bullet
- `--rcd-window-days` 0=today only, 1=today+yesterday
- `--rcd-max-items` max RCD items per run
- `--rcd-bullets-per-article` bullets per RCD article
- `--force` ignore seen IDs (still respects window)
- `--debug` print feed pipeline counters
- `--state-file` path to state JSON (default: `news_state.json`)

## State files

- `.maradmin_state.json` tracks seen MARADMIN IDs and last run time.
- `news_state.json` tracks seen IDs per feed and last run metadata.

You can delete the state files to reprocess everything or use `--force`.

## Scheduling

On Windows Task Scheduler, create a task that runs a command like:

```powershell
C:\Path\To\python.exe C:\Path\To\Stone_Slack_Alerts\MARADMIN.py
```

Repeat for `News.py` as needed (daily or multiple times per day).

## Troubleshooting

- `Missing OPENAI_API_KEY` or `Missing SLACK_WEBHOOK_URL`: check your `.env` or environment variables.
- Marines.mil 403: `MARADMIN.py` will fall back to RSS summaries when it cannot fetch the full page.
- Slack webhook errors: verify the URL and that the webhook is enabled in your workspace.
- If summaries look wrong, test with `--show-raw` (MARADMIN) or `--dry-run` (both scripts).

## Notes

- OpenAI usage costs depend on model and volume. Check your account limits.
- Do not commit `.env` or state files with secrets.

## Prompt overrides (optional)

To keep personal wording out of the public code, you can move prompts into `.env`.
These accept optional format variables (for example `{bullets}`, `{max_bullets}`, `{sentences}`, `{bullets_per_article}`).

MARADMIN prompt env vars:

- `MARADMIN_PROMPT_BASE`
- `MARADMIN_PROMPT_READ_ASAP`
- `MARADMIN_PROMPT_DATES_ONLY`
- `MARADMIN_PROMPT_BRIEF_RESULTS`
- `MARADMIN_PROMPT_FYI_NOT_17XX`
- `MARADMIN_PROMPT_MINIMAL`
- `MARADMIN_PROMPT_FULL_17XX`

News prompt env vars:

- `CISO_PROMPT_INSTRUCTIONS`
- `RCD_PROMPT_INSTRUCTIONS`
