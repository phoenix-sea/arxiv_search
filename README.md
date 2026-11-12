# arxiv-search

Self-hosted arXiv index for a Raspberry Pi.

- **Index:** arXiv papers in cs.IR, cs.CL, cs.AI and cs.LG (SQLite full-text search). The initial
  index comes from the Kaggle arXiv snapshot.
- **Affiliations:** for every paper, the author block (arXiv HTML, or page 1 of the PDF) is
  parsed with regular expressions into organization, unit and email domain, and indexed.
- **Daily job** (03:00 UTC), in priority order:
  1. crawl papers submitted since the newest one in the index;
  2. parse their affiliations;
  3. ask Gemini whether each new paper is worth interest;
  4. send a Telegram message (title, authors, affiliations, arXiv link) for each one it flags;
  5. backfill affiliations of older papers, newest first, until 02:30 UTC.
- **UI:** search with judgment buttons (*interesting* / *not interesting*) that store your labels;
  papers by affiliation, newest first; stats of the most frequent authors and affiliations in the
  last N days.

The Gemini prompt is a placeholder (`PROMPT` in `arxiv_search/gemini.py`) until it is written
from the labels.

## Raspberry Pi setup

Needs a Raspberry Pi 4/5 with 64-bit Raspberry Pi OS and about 3 GB of free disk.

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

**Option A: copy the database you already have** (1.4 GB). On your computer, flush SQLite's
write-ahead log first so the single file is complete, then copy it:

```bash
cd ~/src/arxiv_search
sqlite3 data/arxiv.db "PRAGMA wal_checkpoint(TRUNCATE);"
scp data/arxiv.db pi@raspberrypi.local:~/arxiv_search/data/arxiv.db
```

**Option B: build it on the Pi** from the Kaggle snapshot
(<https://www.kaggle.com/datasets/Cornell-University/arxiv>, free account, ~1.9 GB zip). Copy
`archive.zip` to the Pi, finish step 4, then run `.venv/bin/arxiv-search import-kaggle ~/archive.zip`.

### 4. Install

```bash
cd ~/arxiv_search
./deploy/install.sh
```

This creates the virtualenv, installs the dependencies, creates `.env` from `.env.example`,
prepares the database, installs the systemd units and starts the web UI and the daily timer.

### 5. Configure

```bash
nano .env
sudo systemctl restart arxiv-search-web
```

| Variable | Default | |
|---|---|---|
| `GEMINI_API_KEY` | | from <https://aistudio.google.com/apikey>; without it nothing is flagged |
| `GEMINI_MODEL` | `gemini-3.5-flash-lite` | |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | | without them no messages are sent |
| `CATEGORIES` | `cs.IR,cs.CL,cs.AI,cs.LG` | |
| `DB_PATH` | `data/arxiv.db` | |
| `WEB_PORT` | `8000` | |

**Telegram bot:** in Telegram, message **@BotFather**, send `/newbot` and copy the token into
`TELEGRAM_BOT_TOKEN`. Send your new bot any message, then open
`https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser; the number at `"chat":{"id":...}`
is your `TELEGRAM_CHAT_ID`.

### 6. Run the first job

```bash
sudo systemctl start --no-block arxiv-search-update.service
journalctl -fu arxiv-search-update        # Ctrl-C stops watching, not the job
```

The first run crawls everything since the newest paper in the index, so it takes longer than a
normal day. arXiv allows one request every 3 seconds; parsing affiliations needs one or two
downloads per paper, so about 900 papers an hour. After the new papers, the job keeps backfilling
older papers until 02:30 UTC and then stops, so the next run starts on time.

### 7. Open the UI

`http://raspberrypi.local:8000` from any device on your network. The UI has no login: keep it on
your LAN.

### Updating

```bash
cd ~/arxiv_search
git pull
./deploy/install.sh            # keeps .env and the database
sudo systemctl restart arxiv-search-web
```

## Logs

```bash
journalctl -u arxiv-search-update --since today
journalctl -fu arxiv-search-web
systemctl list-timers arxiv-search-update.timer
```

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest -q
```
