"""One request at a time to arXiv, across processes.

arXiv allows one request every 3 seconds per client. The daily update and a
backfill can run at the same time, so the time of the last request is kept in
a lock file (next to the database) that every arxiv-search process honours.
"""
from __future__ import annotations

import fcntl
import time
from pathlib import Path


class RateLimit:
    def __init__(self, gap: float, path: str | Path | None = None):
        self.gap = gap
        self.path = Path(path) if path else None
        self._last = 0.0

    def wait(self) -> None:
        if self.gap <= 0:
            return
        if self.path is None:  # this process only
            delay = self.gap - (time.monotonic() - self._last)
            if delay > 0:
                time.sleep(delay)
            self._last = time.monotonic()
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)  # held while sleeping: other processes queue behind us
            try:
                f.seek(0)
                try:
                    last = float(f.read().strip() or 0)
                except ValueError:
                    last = 0.0
                delay = self.gap - (time.time() - last)
                if 0 < delay <= self.gap:
                    time.sleep(delay)
                f.seek(0)
                f.truncate()
                f.write(f"{time.time():.3f}")
                f.flush()
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
