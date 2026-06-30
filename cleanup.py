"""
Tidy Downloads — manual cleanup (old / junk file handling).

IMPORTANT: nothing here ever runs automatically and nothing is ever hard
deleted. The background watcher never calls this module. "Delete" means:

    1. stage the file into  <folder>/_ToReview/   (fully recoverable), then
    2. once it has sat there untouched for REVIEW_GRACE_DAYS, send it to the
       macOS Trash (still recoverable for ~30 days).

So a file crosses two safety nets before it is ever truly gone.
"""

from __future__ import annotations

import fnmatch
import hashlib
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

import config

LogFn = Callable[[str, str, str], None]


# ---------------------------------------------------------------------------
# Time / size helpers
# ---------------------------------------------------------------------------

def _age_days(path: Path) -> float:
    """Days since the file was last touched (prefers access time)."""
    st = path.stat()
    last = max(getattr(st, "st_atime", 0), st.st_mtime)
    return (time.time() - last) / 86400


def _size_mb(path: Path) -> float:
    return path.stat().st_size / (1024 * 1024)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------

def _iter_files(root: Path):
    """Yield files under a watched folder, skipping the _ToReview staging area
    and hidden files."""
    if not root.exists():
        return
    review = root / config.REVIEW_FOLDER_NAME
    for path in root.rglob("*"):
        if review in path.parents or path == review:
            continue
        if path.is_file() and not path.name.startswith("."):
            yield path


def is_protected(path: Path, root: Path) -> bool:
    """Files we must never select for cleanup."""
    if _age_days(path) < config.MIN_AGE_DAYS:
        return True
    # Anything filed under Documents/ is kept by default.
    try:
        if path.relative_to(root).parts[0] == "Documents":
            return True
    except (ValueError, IndexError):
        pass
    name_lower = path.name.lower()
    return any(fnmatch.fnmatch(name_lower, pat.lower()) for pat in config.KEEP_PATTERNS)


def candidate_reason(path: Path, root: Path) -> str | None:
    """Return a human-readable reason this file is a cleanup candidate, or None
    if it should be kept. Checks protections first."""
    if is_protected(path, root):
        return None

    ext = path.suffix.lower()
    age = _age_days(path)

    if ext in config.TEMP_EXTENSIONS and age > 1:
        return "Abandoned partial download"
    if ext in {".dmg", ".pkg"} and age > config.INSTALLER_DAYS:
        return f"Installer older than {config.INSTALLER_DAYS} days"
    if ext in {".zip", ".rar", ".7z", ".tar", ".gz", ".tgz"}:
        if age > config.ARCHIVE_DAYS:
            return f"Archive older than {config.ARCHIVE_DAYS} days"
        if (path.with_suffix("")).is_dir():
            return "Archive already extracted (matching folder exists)"
    try:
        if path.relative_to(root).parts[0] == "Misc" and age > config.MISC_DAYS:
            return f"In Misc/ and older than {config.MISC_DAYS} days"
    except (ValueError, IndexError):
        pass
    if _size_mb(path) > config.BIG_MB and age > config.STALE_DAYS:
        return f"Large (>{config.BIG_MB} MB) and older than {config.STALE_DAYS} days"
    if age > config.STALE_DAYS:
        return f"Not used in over {config.STALE_DAYS} days"
    return None


def _find_duplicates(files: list[Path]) -> dict[Path, str]:
    """Map every-but-the-oldest copy of identical files to a reason. Hashing is
    only done within same-size groups for speed."""
    by_size: dict[int, list[Path]] = {}
    for p in files:
        try:
            by_size.setdefault(p.stat().st_size, []).append(p)
        except OSError:
            continue
    dupes: dict[Path, str] = {}
    for group in by_size.values():
        if len(group) < 2:
            continue
        by_hash: dict[str, list[Path]] = {}
        for p in group:
            try:
                by_hash.setdefault(_sha256(p), []).append(p)
            except OSError:
                continue
        for copies in by_hash.values():
            if len(copies) < 2:
                continue
            copies.sort(key=lambda p: p.stat().st_mtime)  # keep the oldest
            for extra in copies[1:]:
                dupes[extra] = "Duplicate of an existing file"
    return dupes


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def run_cleanup(folders: list[dict], apply: bool, log: LogFn) -> None:
    grand_total = 0.0
    grand_count = 0
    for folder_cfg in folders:
        root = folder_cfg["path"]
        files = list(_iter_files(root))
        reasons: dict[Path, str] = {}
        for p in files:
            r = candidate_reason(p, root)
            if r:
                reasons[p] = r
        # Duplicates (skip ones already protected).
        for p, r in _find_duplicates(files).items():
            if p not in reasons and not is_protected(p, root):
                reasons[p] = r

        if not reasons:
            print(f"[{root.name}] Nothing to clean up.")
            continue

        review_dir = root / config.REVIEW_FOLDER_NAME
        total_mb = 0.0
        print(f"\n[{root.name}] {len(reasons)} candidate(s):")
        for p, reason in sorted(reasons.items()):
            mb = _size_mb(p)
            total_mb += mb
            print(f"  {mb:8.1f} MB  {p.name}  —  {reason}")
            if apply:
                review_dir.mkdir(parents=True, exist_ok=True)
                dest = _unique(review_dir / p.name)
                shutil.move(str(p), str(dest))
                log("STAGED", p.name, reason)
        grand_total += total_mb
        grand_count += len(reasons)
        print(f"  → {total_mb:.1f} MB total")

    if not apply:
        print(f"\nPreview only — nothing changed. {grand_count} file(s), {grand_total:.1f} MB.")
        print("Run again with --apply to move these into _ToReview/.")
    else:
        print(f"\nStaged {grand_count} file(s) ({grand_total:.1f} MB) into _ToReview/.")
        print("They go to the Trash after the grace period: run 'tidy --empty-reviewed --apply'.")


def run_empty_reviewed(folders: list[dict], apply: bool, log: LogFn) -> None:
    try:
        from send2trash import send2trash
    except Exception:
        print("send2trash is not installed; cannot move items to the Trash.")
        return

    total = 0
    for folder_cfg in folders:
        review_dir = folder_cfg["path"] / config.REVIEW_FOLDER_NAME
        if not review_dir.exists():
            continue
        for p in sorted(review_dir.iterdir()):
            if p.name.startswith("."):
                continue
            if _age_days(p) < config.REVIEW_GRACE_DAYS:
                continue
            total += 1
            if apply:
                send2trash(str(p))
                log("TRASHED", p.name, "Sent to macOS Trash")
            else:
                print(f"  would trash: {p.name}")

    if not apply:
        print(f"\nPreview only — {total} item(s) past the {config.REVIEW_GRACE_DAYS}-day grace period.")
        print("Run again with --apply to send them to the Trash.")
    else:
        print(f"Sent {total} item(s) to the Trash (recoverable for ~30 days).")


def run_restore(folders: list[dict], log: LogFn) -> None:
    total = 0
    for folder_cfg in folders:
        root = folder_cfg["path"]
        review_dir = root / config.REVIEW_FOLDER_NAME
        if not review_dir.exists():
            continue
        for p in sorted(review_dir.iterdir()):
            if p.name.startswith("."):
                continue
            dest = _unique(root / p.name)
            shutil.move(str(p), str(dest))
            log("RESTORE", p.name, f"{root.name}/")
            total += 1
    print(f"Restored {total} item(s) from _ToReview/.")


def _unique(target: Path) -> Path:
    """Never overwrite when staging/restoring."""
    if not target.exists():
        return target
    stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    return target.with_name(f"{target.stem}_{stamp}{target.suffix}")
