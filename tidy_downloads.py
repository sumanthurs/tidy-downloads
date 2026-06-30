#!/usr/bin/env python3
"""
Tidy Downloads — the main program.

Run with no arguments to start the background watcher (this is what the macOS
LaunchAgent does). The other modes are for one-off, manual use from Terminal:

    tidy                       # watch Downloads + Desktop (foreground)
    tidy --sweep               # organize files already sitting in the folders
    tidy --dry-run             # show what would happen, move nothing
    tidy --once PATH           # print where a single file would go
    tidy --cleanup             # show old/junk files (changes nothing)
    tidy --cleanup --apply     # move them to _ToReview
    tidy --empty-reviewed      # (with --apply) send reviewed items to Trash
    tidy --restore             # bring _ToReview items back

See README.md for the friendly explanation.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

import config
import categorizer
import cleanup

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

_logger = logging.getLogger("tidy")


def setup_logging() -> None:
    config.APP_DIR.mkdir(parents=True, exist_ok=True)
    config.LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    _logger.setLevel(logging.INFO)
    handler = RotatingFileHandler(
        config.LOG_PATH, maxBytes=config.LOG_MAX_BYTES, backupCount=config.LOG_BACKUPS
    )
    handler.setFormatter(logging.Formatter("%(asctime)s | %(message)s", "%Y-%m-%d %H:%M:%S"))
    _logger.addHandler(handler)
    # Also echo to stdout so `launchctl` log files and foreground runs show it.
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(logging.Formatter("%(asctime)s | %(message)s", "%Y-%m-%d %H:%M:%S"))
    _logger.addHandler(stream)


def log(action: str, name: str, detail: str) -> None:
    """Write one pipe-delimited log line, e.g. 'MOVED | a.pdf | Documents/...'."""
    _logger.info("%-7s | %s | %s", action, name, detail)


# ---------------------------------------------------------------------------
# Filesystem helpers
# ---------------------------------------------------------------------------

def created_time(path: Path) -> datetime:
    """Best-available creation time (macOS has true birth time)."""
    st = path.stat()
    ts = getattr(st, "st_birthtime", None) or st.st_mtime
    return datetime.fromtimestamp(ts)


def should_process(path: Path, watched_root: Path) -> bool:
    """Filter out things we must never touch: hidden files, the log, our own
    managed folders, and directories that aren't bundles."""
    name = path.name
    if name.startswith("."):
        return False
    if path == config.LOG_PATH:
        return False
    # Skip our managed top-level folders and anything inside them.
    try:
        rel_first = path.relative_to(watched_root).parts[0]
        if rel_first in config.MANAGED_TOP_LEVEL:
            return False
    except (ValueError, IndexError):
        pass
    if path.is_dir() and path.suffix.lower() not in config.BUNDLE_EXTENSIONS:
        return False
    return True


def wait_until_stable(path: Path) -> bool:
    """Wait until the file has stopped changing size. Returns False if the file
    is a temp/partial download, disappears, or never settles in time."""
    if path.suffix.lower() in config.TEMP_EXTENSIONS:
        return False  # the browser will rename it; we'll get that event

    last_size = -1
    stable_for = 0.0
    waited = 0.0
    while waited < config.MAX_WAIT_SECONDS:
        if not path.exists():
            return False
        try:
            size = path.stat().st_size
        except OSError:
            return False
        if size == last_size:
            stable_for += config.POLL_INTERVAL
            if stable_for >= config.STABLE_SECONDS:
                return True
        else:
            stable_for = 0.0
            last_size = size
        time.sleep(config.POLL_INTERVAL)
        waited += config.POLL_INTERVAL
    return False


def unique_destination(dest_dir: Path, filename: str) -> Path:
    """Return a path inside dest_dir that does not exist yet, appending a
    timestamp (and counter if needed) on collision — never overwrites."""
    target = dest_dir / filename
    if not target.exists():
        return target
    stem = Path(filename).stem
    suffix = Path(filename).suffix
    stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    candidate = dest_dir / f"{stem}_{stamp}{suffix}"
    counter = 1
    while candidate.exists():
        candidate = dest_dir / f"{stem}_{stamp}-{counter}{suffix}"
        counter += 1
    return candidate


# ---------------------------------------------------------------------------
# Placement (per-folder rules: category filter, grace delay, date buckets)
# ---------------------------------------------------------------------------

def category_allowed(key: str, folder_cfg: dict) -> bool:
    allowed = folder_cfg.get("categories")
    if not allowed:
        return True
    group = config.CATEGORIES[key]["group"]
    return key in allowed or group in allowed


def passes_move_delay(path: Path, folder_cfg: dict, force: bool) -> bool:
    """Decide whether a file is 'old enough' to move now. Files that are too
    new are skipped silently and retried by the periodic re-scan."""
    if force:
        return True

    ignore_recent = folder_cfg.get("ignore_recent_hours", 0)
    age_hours = (datetime.now() - created_time(path)).total_seconds() / 3600
    if ignore_recent and age_hours < ignore_recent:
        return False

    delay = folder_cfg.get("move_delay", {"mode": "none"})
    mode = delay.get("mode", "none")
    if mode == "grace":
        return age_hours >= delay.get("hours", config.DESKTOP_GRACE_HOURS)
    if mode == "next_day":
        return created_time(path).date() < datetime.now().date()
    return True


def destination_dir(watched_root: Path, key: str, path: Path, folder_cfg: dict) -> Path:
    base = watched_root / config.CATEGORIES[key]["path"]
    buckets = folder_cfg.get("date_buckets", "none")
    if buckets == "monthly":
        base = base / created_time(path).strftime("%Y-%m")
    elif buckets == "weekly":
        base = base / created_time(path).strftime("%Y-W%V")
    return base


# ---------------------------------------------------------------------------
# Core: process one file
# ---------------------------------------------------------------------------

class Worker:
    def __init__(self, dry_run: bool):
        self.dry_run = dry_run
        self._in_progress: set[str] = set()
        self._lock = threading.Lock()

    def process(self, path: Path, folder_cfg: dict, force: bool = False) -> None:
        key = str(path)
        with self._lock:
            if key in self._in_progress:
                return
            self._in_progress.add(key)
        try:
            self._process_inner(path, folder_cfg, force)
        except categorizer.ContentReadError as exc:
            log("ERROR", path.name, f"{exc}, left in place")
        except Exception as exc:  # never crash the watcher over one file
            log("ERROR", path.name, f"Unexpected error: {exc}, left in place")
        finally:
            with self._lock:
                self._in_progress.discard(key)

    def _process_inner(self, path: Path, folder_cfg: dict, force: bool) -> None:
        watched_root = folder_cfg["path"]
        if not should_process(path, watched_root):
            return
        if not wait_until_stable(path):
            return
        if not path.exists():
            return
        if not passes_move_delay(path, folder_cfg, force):
            return  # too new; periodic re-scan will retry later

        category = categorizer.categorize(path)
        if not category_allowed(category, folder_cfg):
            return  # this folder isn't configured to organize this type

        dest_dir = destination_dir(watched_root, category, path, folder_cfg)
        rel = dest_dir.relative_to(watched_root)
        label = f"{watched_root.name}/{rel}/"

        if self.dry_run:
            log("DRY-RUN", path.name, f"would move -> {label}")
            return

        import shutil
        dest_dir.mkdir(parents=True, exist_ok=True)
        final = unique_destination(dest_dir, path.name)
        shutil.move(str(path), str(final))
        log("MOVED", path.name, f"{label}{('(renamed: ' + final.name + ')') if final.name != path.name else ''}")


# ---------------------------------------------------------------------------
# Watchdog integration
# ---------------------------------------------------------------------------

def build_observer(worker: "Worker", pool: ThreadPoolExecutor):
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler

    class Handler(FileSystemEventHandler):
        def __init__(self, folder_cfg: dict):
            self.folder_cfg = folder_cfg

        def _submit(self, raw_path: str) -> None:
            pool.submit(worker.process, Path(raw_path), self.folder_cfg)

        def on_created(self, event):
            self._submit(event.src_path)

        def on_moved(self, event):
            self._submit(event.dest_path)

    observer = Observer()
    for folder_cfg in config.WATCHED_DIRS:
        root = folder_cfg["path"]
        if not root.exists():
            continue
        observer.schedule(Handler(folder_cfg), str(root), recursive=False)
    return observer


def periodic_rescan(worker: "Worker", stop_event: threading.Event) -> None:
    """Re-scan watched folders on an interval. This is what eventually files
    Desktop items once their grace period has passed, and catches anything the
    live watcher missed."""
    while not stop_event.wait(config.RESCAN_INTERVAL_SECONDS):
        for folder_cfg in config.WATCHED_DIRS:
            sweep_folder(worker, folder_cfg, force=False)


# ---------------------------------------------------------------------------
# Sweep (existing backlog)
# ---------------------------------------------------------------------------

def sweep_folder(worker: "Worker", folder_cfg: dict, force: bool) -> None:
    root = folder_cfg["path"]
    if not root.exists():
        return
    for entry in sorted(root.iterdir()):
        if should_process(entry, root):
            worker.process(entry, folder_cfg, force=force)


def run_sweep(target: str, dry_run: bool) -> None:
    worker = Worker(dry_run=dry_run)
    for folder_cfg in _targets(target):
        log("SWEEP", folder_cfg["path"].name, "organizing existing files")
        sweep_folder(worker, folder_cfg, force=True)


def _targets(target: str) -> list[dict]:
    target = (target or "all").lower()
    if target == "all":
        return config.WATCHED_DIRS
    for folder_cfg in config.WATCHED_DIRS:
        if folder_cfg["path"].name.lower() == target:
            return [folder_cfg]
    print(f"Unknown target '{target}'. Use: downloads, desktop, or all.")
    sys.exit(2)


# ---------------------------------------------------------------------------
# Single-instance lock
# ---------------------------------------------------------------------------

def acquire_lock() -> bool:
    config.APP_DIR.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(config.LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_RDWR)
    except FileExistsError:
        # Check if the recorded PID is still alive; clear stale locks.
        try:
            pid = int(config.LOCK_PATH.read_text().strip() or "0")
            os.kill(pid, 0)
            return False  # still running
        except (ValueError, ProcessLookupError, PermissionError):
            config.LOCK_PATH.unlink(missing_ok=True)
            return acquire_lock()
        except Exception:
            return False
    os.write(fd, str(os.getpid()).encode())
    os.close(fd)
    return True


def release_lock() -> None:
    config.LOCK_PATH.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Watch mode
# ---------------------------------------------------------------------------

def run_watch(dry_run: bool) -> None:
    if not acquire_lock():
        print("Tidy Downloads is already running.")
        sys.exit(0)

    watched = ", ".join(str(f["path"]) for f in config.WATCHED_DIRS)
    log("START", f"v{config.VERSION}", f"watching: {watched}")

    worker = Worker(dry_run=dry_run)
    pool = ThreadPoolExecutor(max_workers=4)
    observer = build_observer(worker, pool)
    stop_event = threading.Event()

    rescan = threading.Thread(target=periodic_rescan, args=(worker, stop_event), daemon=True)

    def shutdown(*_args):
        stop_event.set()
        observer.stop()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    observer.start()
    rescan.start()
    try:
        observer.join()
    finally:
        pool.shutdown(wait=False)
        release_lock()
        log("STOP", f"v{config.VERSION}", "watcher stopped")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def run_once(raw_path: str) -> None:
    path = Path(raw_path).expanduser()
    if not path.exists():
        print(f"No such file: {path}")
        sys.exit(2)
    try:
        key = categorizer.categorize(path)
        print(f"{path.name} -> {config.CATEGORIES[key]['path']}/  (category: {key})")
    except categorizer.ContentReadError as exc:
        print(f"{path.name} -> could not read content: {exc}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Tidy Downloads — auto-organize Downloads & Desktop.")
    parser.add_argument("--sweep", action="store_true", help="Organize files already in the folders, then exit.")
    parser.add_argument("--dry-run", action="store_true", help="Show what would happen; move nothing.")
    parser.add_argument("--once", metavar="PATH", help="Print where a single file would go, then exit.")
    parser.add_argument("--cleanup", action="store_true", help="Find old/junk files (report only unless --apply).")
    parser.add_argument("--empty-reviewed", action="store_true", help="Send aged _ToReview items to Trash (with --apply).")
    parser.add_argument("--restore", action="store_true", help="Move _ToReview items back to their folder.")
    parser.add_argument("--apply", action="store_true", help="Actually perform cleanup actions (default is preview).")
    parser.add_argument("--target", default="all", help="downloads | desktop | all (default: all).")
    args = parser.parse_args()

    setup_logging()

    if args.once:
        run_once(args.once)
        return
    if args.cleanup:
        cleanup.run_cleanup(_targets(args.target), apply=args.apply, log=log)
        return
    if args.empty_reviewed:
        cleanup.run_empty_reviewed(_targets(args.target), apply=args.apply, log=log)
        return
    if args.restore:
        cleanup.run_restore(_targets(args.target), log=log)
        return
    if args.sweep:
        run_sweep(args.target, dry_run=args.dry_run)
        return

    run_watch(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
