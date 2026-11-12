# arxiv-search

Self-hosted arXiv index for a Raspberry Pi:

- **Index**: SQLite + FTS5 full-text search over arXiv metadata (title, abstract, authors) for
  your categories (default `cs.IR, cs.CL, cs.AI, cs.LG`, about 500k papers, about 1.4 GB).
- **Daily updater** (systemd timer): fetches newly submitted papers from the arXiv API, has
  **Gemini** score every new paper for *relevance* (to your `profile.md`) and *importance* (0–10,
  with a one-line TL;DR and reason), then sends a **Telegram** digest plus the top papers. Finally it
  reads each new paper's **author block** (arXiv HTML, or page 1 of the PDF) and parses the
  **organizations** behind it with rules (no LLM): company or university, lab, type and email
  domain, and which authors belong to each.
- **Search UI** (systemd service): search with filters for **author**, **organization** (name, lab or
  email domain, e.g. `World Bank` or `worldbankgroup.org`), organization type (company, academia,
  ...), category, date, minimum Gemini scores and "found in the last N days", sorted by best match,
  Gemini scores or newest. Author names and organizations are clickable. Includes
  paper pages and a stats page with run history and Gemini spend.

```
arXiv API ──► update (daily timer) ──► SQLite (papers + FTS5 + analysis)
                  │        │                        ▲
                  │        └─► Gemini (batches of 20 papers, JSON scores)
                  └─► Telegram (digest + top N)     │
                                         web UI (FastAPI) ◄── browser on your LAN
```

## Requirements

- Raspberry Pi 4/5 with **64-bit** Raspberry Pi OS (Bookworm or later, Python 3.11+). On 32-bit
  OS some dependencies have no prebuilt wheels.
- About 3 GB of free disk. An SSD is better than an SD card for SQLite.
- A Gemini API key (<https://aistudio.google.com/apikey>) and a Telegram bot (see below).

## Raspberry Pi setup

### 1. Prepare the Pi

1. Flash **Raspberry Pi OS Lite (64-bit)** with Raspberry Pi Imager. In the Imager settings, set the
   hostname (`raspberrypi`), a username and password, Wi-Fi if you need it, and **enable SSH**.
2. Boot the Pi, log in from your computer and install the system packages:

   ```bash
   ssh pi@raspberrypi.local            # use the username you set in the Imager
   sudo apt-get update && sudo apt-get install -y git python3-venv sqlite3
   python3 --version                   # must be 3.11 or newer
   uname -m                            # must be aarch64 (64-bit)
   ```

### 2. Get the code

```bash
git clone https://github.com/phoenix-sea/arxiv_search.git ~/arxiv_search
mkdir -p ~/arxiv_search/data
```

### 3. Copy the initial index

**Option A: copy the database you already have** (fastest, 1.4 GB). On your computer, flush
SQLite's write-ahead log first so the single file is complete, then copy it:

```bash
cd ~/src/arxiv_search
sqlite3 data/arxiv.db "PRAGMA wal_checkpoint(TRUNCATE);"
scp data/arxiv.db pi@raspberrypi.local:~/arxiv_search/data/arxiv.db
```

**Option B: build it on the Pi** from the Kaggle snapshot
(<https://www.kaggle.com/datasets/Cornell-University/arxiv>, free account, ~1.9 GB zip). Copy
`archive.zip` to the Pi, finish step 4, then run
`.venv/bin/arxiv-search import-kaggle ~/archive.zip` (about 10 minutes on a Pi 5).

### 4. Install

```bash
cd ~/arxiv_search
./deploy/install.sh
```

The script creates the virtualenv, installs the dependencies, creates `.env` from `.env.example`
(`chmod 600`), migrates the database, installs the systemd units and starts the web UI and the
daily timer.

### 5. Configure

```bash
nano .env          # GEMINI_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, PUBLIC_URL
nano profile.md    # your interests: this is what Gemini's "relevance" score means
sudo systemctl restart arxiv-search-web
```

Get a Gemini key at <https://aistudio.google.com/apikey>. Set `PUBLIC_URL` to the address you open
the UI with (e.g. `http://raspberrypi.local:8000`); Telegram links point there. All settings are
listed under [Configuration](#configuration-env).

**Telegram bot:**

1. In Telegram, message **@BotFather**, send `/newbot` and copy the token into `TELEGRAM_BOT_TOKEN`.
2. Open your new bot and send it any message.
3. `.venv/bin/arxiv-search telegram-chat-id` prints the line to put in `.env`.
4. `.venv/bin/arxiv-search test-telegram` sends a test message.

### 6. Check, then run the first update

```bash
.venv/bin/arxiv-search test-gemini -n 3                # scores the 3 newest papers, saves nothing
.venv/bin/arxiv-search test-affiliations 2610.36910    # parses one paper's affiliations, saves nothing
sudo systemctl start arxiv-search-update.service --no-block
journalctl -fu arxiv-search-update                     # Ctrl-C stops watching, not the update
```

The first run fetches everything since the newest paper in the index (about 450 papers per day
since the 2026-10-29 snapshot, so about 6k on 2026-11-11). It scores them with Gemini (about $3),
sends you a digest plus the top 15 on Telegram, and then parses their affiliations, which takes
several hours at arXiv's rate limit. After that, each daily run (03:00 UTC) handles about 300–800
papers in under an hour.

### 7. Open the UI

Open `http://raspberrypi.local:8000` from any device on your network. Search works on the whole
index right away. Gemini scores and affiliations appear for papers as they are processed; older
papers can be backfilled (see [Backfill](#backfill-optional) and [Affiliations](#affiliations)).

### Updating

```bash
cd ~/arxiv_search
git pull
.venv/bin/pip install -e .
./deploy/install.sh           # re-applies the systemd units; keeps .env and the database
sudo systemctl restart arxiv-search-web
```

## Configuration (`.env`)

| Variable | Default | |
|---|---|---|
| `GEMINI_API_KEY` | | required for analysis |
| `GEMINI_MODEL` | `gemini-3.5-flash-lite` | `gemini-2.5-flash-lite` is about 5× cheaper |
| `GEMINI_THINKING_LEVEL` | `minimal` | thinking tokens are billed as output; empty = model default |
| `GEMINI_BATCH_SIZE` / `GEMINI_RPM` | `20` / `10` | papers per request, and requests per minute |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | | notifications are off unless both are set |
| `NOTIFY_MIN_RELEVANCE` / `NOTIFY_MIN_IMPORTANCE` | `7` / `0` | which new papers get their own message |
| `NOTIFY_MAX_PER_RUN` | `15` | individual messages per run; the digest counts the rest |
| `CATEGORIES` | `cs.IR,cs.CL,cs.AI,cs.LG` | |
| `PROFILE_PATH` | `profile.md` | **edit this**: it is what "relevance" means |
| `LOOKBACK_DAYS` | `3` | each run re-checks this many days before the newest stored paper |
| `AFFILIATIONS` | `1` | `0` turns off affiliation parsing in the daily update |
| `PUBLIC_URL` | `http://raspberrypi.local:8000` | used for links in Telegram messages |
| `DB_PATH`, `WEB_HOST`, `WEB_PORT` | `data/arxiv.db`, `0.0.0.0`, `8000` | |

Changes take effect on the next update run. After changing web settings, run
`sudo systemctl restart arxiv-search-web`.

## Gemini cost

These are estimates at list prices (standard tier, Nov 2026), assuming about 420 input and 140
output tokens per paper. Actual token use is recorded on the **Stats** page.

| Work | Papers | gemini-3.5-flash-lite | gemini-2.5-flash-lite |
|---|---|---|---|
| Daily updates | ~13k / month | ~$6 / month | ~$1.3 / month |
| First catch-up | ~4.4k | ~$2 | ~$0.4 |
| Backfill 2025+ | ~190k | ~$90 | ~$19 |
| Backfill 2023+ | ~310k | ~$150 | ~$30 |
| Backfill everything | ~500k | ~$240 | ~$49 |

### Backfill (optional)

Papers from the initial index are searchable but not scored. Scoring them is optional, resumable,
and asks for confirmation (`--yes`) above 1,000 papers:

```bash
.venv/bin/arxiv-search analyze --since 2025-01-01                       # prints count + cost estimate
.venv/bin/arxiv-search analyze --since 2025-01-01 --model gemini-2.5-flash-lite --yes
```

To run it unattended, edit `ExecStart` in `/etc/systemd/system/arxiv-search-backfill.service`,
then run `sudo systemctl daemon-reload && sudo systemctl start arxiv-search-backfill`. It stops
cleanly if Gemini quota runs out, and you can start it again later.

Backfilled papers are never sent to Telegram. Only papers found by the daily updater are.

### Affiliations

arXiv metadata has author names, but affiliations for only about 1.5% of papers. So the updater
downloads each new paper's author block from arXiv (the HTML version, cut off at the abstract; page 1
of the PDF when there is no HTML or the HTML block has only names) and parses it with rules in
`orgparse.py`: no Gemini, no cost. It finds organizations by keywords (University, Inc., Lab, ...)
and a list of well-known ones, units (Department of ..., ... Lab), email domains, and links authors
by markers (^1, *, †) or by which names come before each affiliation. arXiv allows one request every
3 seconds, so this adds about 30 minutes to a daily run of about 500 papers. It runs after the
Telegram messages, so it doesn't delay them.

To check a paper without saving anything:

```bash
.venv/bin/arxiv-search test-affiliations 2610.36910   # prints the author block and the parsed orgs
```

The author block is stored with the result, so after improving the rules you can re-parse
everything without downloading again: `arxiv-search affiliations --reparse`.

Papers already in the index have no affiliations until you backfill them. This is resumable and
asks for `--yes` above 1,000 papers. It is limited by the arXiv download speed, roughly
900 papers an hour: ~11 hours for one month of papers, ~4.5 days for 2026, ~9 days for 2025+.

```bash
.venv/bin/arxiv-search affiliations --since 2026-10-01         # prints count and time estimate
.venv/bin/arxiv-search affiliations --since 2026-01-01 --yes
```

To run it unattended, edit `--since` in `/etc/systemd/system/arxiv-search-affiliations.service`,
then run `sudo systemctl daemon-reload && sudo systemctl start arxiv-search-affiliations --no-block`.
It resumes where it stopped. It can run while the daily update runs: all arxiv-search processes
share one arXiv rate limit (a lock file next to the database), so together they still make at most
one request every 3.5 seconds, and the backfill simply slows down during the update.

## Operations

```bash
systemctl list-timers arxiv-search-update.timer   # next run (daily 03:00 UTC, after arXiv's announcement)
journalctl -u arxiv-search-update --since today   # updater log
journalctl -fu arxiv-search-web                   # UI log
.venv/bin/arxiv-search stats                      # counts + last runs
.venv/bin/arxiv-search update --no-notify         # manual run without messages
.venv/bin/arxiv-search update --no-affiliations   # skip the slow affiliation step this time
.venv/bin/arxiv-search update --since 2026-11-01  # re-scan a window (already-known papers are not re-sent)
```

- **Failures:** if an update fails, it is logged on the Stats page and you get a ⚠️ message on
  Telegram. Papers whose Gemini analysis failed are retried on the next run.
- **Missed runs:** the timer is `Persistent`, so a Pi that was off at 03:00 runs the update at boot.
- **Gaps:** the update window always starts from the newest stored paper, so gaps of any length
  are filled.
- **Backups:** stop the timer, then copy `data/arxiv.db`.
- **Security:** the UI has no login and binds to `0.0.0.0`. Keep it on your LAN, or set
  `WEB_HOST=127.0.0.1` and reach it over SSH or Tailscale. `.env` holds your keys and is
  `chmod 600`.
- **arXiv rules:** arxiv-search makes one request at a time, at least 3.5 s apart, following the
  arXiv API terms. The limit is shared by all its processes on the machine (`data/.arxiv-ratelimit`). arXiv metadata is CC0. Full texts stay on arXiv: the UI links to them.

## Commands

```
arxiv-search init | import-kaggle PATH | update | analyze | affiliations | serve | stats
             telegram-chat-id | test-telegram | test-gemini [-n N] | test-affiliations ID...
```

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest -q
```

Layout: `arxiv_search/` contains `arxiv_api.py` (fetch), `kaggle.py` (initial import), `db.py`
(SQLite/FTS5), `gemini.py` (analysis), `telegram.py`, `pipeline.py` (daily run), `web.py` and
`templates/` (UI), `affiliations.py` (author block download), `orgparse.py` (affiliation rules), and
`cli.py`. `deploy/` has the systemd units and the install script.
