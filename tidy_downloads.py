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
import macos_meta

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
    managed folders, and directories that aren't bundles.

    Paths are resolved first (e.g. /tmp -> /private/tmp, and any other symlink)
    so the "is this inside a managed folder?" check is reliable no matter how
    the OS reports the event path. This is what stops us from re-processing the
    files we ourselves just moved (a move fires an on_moved event whose
    destination is inside a managed sub-folder)."""
    if path.name.startswith("."):
        return False
    rpath = Path(os.path.realpath(path))
    root = Path(os.path.realpath(watched_root))
    if rpath == Path(os.path.realpath(config.LOG_PATH)):
        return False
    try:
        rel_first = rpath.relative_to(root).parts[0]
    except (ValueError, IndexError):
        return False  # not directly under the watched folder -> ignore
    if rel_first in config.MANAGED_TOP_LEVEL:
        return False  # our own category folders (including our own moves)
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
        # Accept the grace period in hours and/or minutes.
        grace_hours = delay.get("hours")
        if grace_hours is None and "minutes" not in delay:
            grace_hours = config.DESKTOP_GRACE_HOURS
        grace_hours = (grace_hours or 0) + delay.get("minutes", 0) / 60
        return age_hours >= grace_hours
    if mode == "next_day":
        return created_time(path).date() < datetime.now().date()
    return True


def destination_dir(watched_root: Path, key: str, path: Path, folder_cfg: dict) -> Path:
    base = watched_root / config.CATEGORIES[key]["path"]
    buckets = folder_cfg.get("date_buckets", "none")
    group = config.CATEGORIES[key]["group"]
    # Only the configured groups (e.g. Images) get month/week sub-folders;
    # everything else stays flat in its category folder.
    if buckets != "none" and group in config.DATE_BUCKET_GROUPS:
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
        key = os.path.realpath(path)
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
        if force:
            # Sweep mode: existing files are already complete, so skip the
            # (slow) download-stability wait — just ignore in-progress temps.
            if path.suffix.lower() in config.TEMP_EXTENSIONS or not path.exists():
                return
        elif not wait_until_stable(path):
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
        # Capture the file's original "Date Added" before moving, since the
        # move would otherwise reset it to now and make the filed file look
        # like it arrived today (Finder groups by Date Added).
        original_added = macos_meta.get_date_added(path)
        dest_dir.mkdir(parents=True, exist_ok=True)
        final = unique_destination(dest_dir, path.name)
        shutil.move(str(path), str(final))
        # Restore Date Added (fall back to the file's creation time). Best
        # effort — never let this fail the move.
        desired = original_added
        if not desired:
            st = final.stat()
            desired = getattr(st, "st_birthtime", None) or st.st_mtime
        macos_meta.set_date_added(final, desired)
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


def run_repair_dates(folders: list[dict]) -> None:
    """One-time fix: files filed by an older version (or the initial sweep) had
    their 'Date Added' reset to the sort time, so they clump under 'Today' in
    Finder. Reset each filed file's Date Added to its true creation time. Only
    touches our own managed category folders — never the user's other folders."""
    files_fixed = 0
    dirs_fixed = 0
    for folder_cfg in folders:
        root = folder_cfg["path"]
        for top in config.MANAGED_TOP_LEVEL:
            base = root / top
            if not base.exists():
                continue
            # Walk bottom-up so each folder's "Date Added" can be set to the
            # newest of its contents. Folders we create during sorting are born
            # "today", so without this a 2025-08 bucket shows under Today.
            for dirpath, dirnames, filenames in os.walk(base, topdown=False):
                dp = Path(dirpath)
                if (dp.suffix.lower() in config.BUNDLE_EXTENSIONS or
                        any(par.suffix.lower() in config.BUNDLE_EXTENSIONS for par in dp.parents)):
                    continue
                newest = 0.0
                for fn in filenames:
                    if fn.startswith("."):
                        continue
                    p = dp / fn
                    try:
                        st = p.stat()
                    except OSError:
                        continue
                    real = getattr(st, "st_birthtime", None) or st.st_mtime
                    if macos_meta.set_date_added(p, real):
                        files_fixed += 1
                    newest = max(newest, real)
                for dn in dirnames:
                    da = macos_meta.get_date_added(dp / dn)
                    if not da:
                        try:
                            st = (dp / dn).stat()
                            da = getattr(st, "st_birthtime", None) or st.st_mtime
                        except OSError:
                            da = 0
                    newest = max(newest, da or 0)
                if newest > 0 and macos_meta.set_date_added(dp, newest):
                    dirs_fixed += 1
        print(f"[{root.name}] repaired Date Added.")
    print(f"Done: fixed {files_fixed} files and {dirs_fixed} folders.")


def _iter_managed_items(leaf: Path):
    """Yield files and bundle directories under a category leaf, without
    descending into bundles."""
    for dirpath, dirnames, filenames in os.walk(leaf):
        dp = Path(dirpath)
        bundles = [d for d in dirnames if (dp / d).suffix.lower() in config.BUNDLE_EXTENSIONS]
        dirnames[:] = [d for d in dirnames if d not in bundles]
        for d in bundles:
            yield dp / d
        for f in filenames:
            if not f.startswith("."):
                yield dp / f


def _remove_empty_dirs(root: Path) -> None:
    """Remove now-empty sub-folders under our managed folders (e.g. month
    folders emptied by flattening). Leaves the top-level managed folders."""
    for top in config.MANAGED_TOP_LEVEL:
        base = root / top
        if not base.exists():
            continue
        for dirpath, _dirnames, _filenames in os.walk(base, topdown=False):
            dp = Path(dirpath)
            if dp == base or dp.suffix.lower() in config.BUNDLE_EXTENSIONS:
                continue
            try:
                remaining = [x for x in dp.iterdir() if x.name != ".DS_Store"]
                if not remaining:
                    for junk in dp.iterdir():
                        junk.unlink()
                    dp.rmdir()
            except OSError:
                pass


def run_rebucket(folders: list[dict]) -> None:
    """Reorganize already-filed files so the on-disk layout matches the current
    bucketing config: groups in DATE_BUCKET_GROUPS get month/week folders,
    everything else is flattened back into its category folder. Idempotent."""
    import shutil
    moved = 0
    for folder_cfg in folders:
        root = folder_cfg["path"]
        buckets = folder_cfg.get("date_buckets", "none")
        for rel in sorted({c["path"] for c in config.CATEGORIES.values()}):
            leaf = root / rel
            if not leaf.exists():
                continue
            group = next(c["group"] for c in config.CATEGORIES.values() if c["path"] == rel)
            should_bucket = buckets != "none" and group in config.DATE_BUCKET_GROUPS
            for item in list(_iter_managed_items(leaf)):
                if should_bucket:
                    ct = created_time(item)
                    name = ct.strftime("%Y-%m") if buckets == "monthly" else ct.strftime("%Y-W%V")
                    desired = leaf / name
                else:
                    desired = leaf
                if item.parent == desired:
                    continue
                original_added = macos_meta.get_date_added(item)
                desired.mkdir(parents=True, exist_ok=True)
                final = unique_destination(desired, item.name)
                shutil.move(str(item), str(final))
                if original_added:
                    macos_meta.set_date_added(final, original_added)
                moved += 1
        _remove_empty_dirs(root)
        print(f"[{root.name}] reorganized to match the month-folder scheme.")
    print(f"Done: moved {moved} item(s).")
    run_repair_dates(folders)  # fix Date Added on files + the month folders


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
    parser.add_argument("--repair-dates", action="store_true", help="Reset filed files' Date Added to their real creation date (one-time fix).")
    parser.add_argument("--rebucket", action="store_true", help="Reorganize filed files to match the month-folder scheme (Images bucketed, rest flat).")
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
    if args.repair_dates:
        run_repair_dates(_targets(args.target))
        return
    if args.rebucket:
        run_rebucket(_targets(args.target))
        return
    if args.sweep:
        run_sweep(args.target, dry_run=args.dry_run)
        return

    run_watch(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
