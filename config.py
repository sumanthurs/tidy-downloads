"""
Tidy Downloads — central configuration.

Everything a user might reasonably want to change lives here, in plain
constants. Edit a value, save the file, then restart the tool:

    ./uninstall_launch_agent.sh && ./install_launch_agent.sh

Nothing in this file requires programming knowledge beyond changing a number,
a True/False, or a name inside quotes.
"""

from __future__ import annotations

from pathlib import Path

HOME = Path.home()

# ---------------------------------------------------------------------------
# Timing
# ---------------------------------------------------------------------------

# A file is considered "finished downloading" once its size has stopped
# changing for this many seconds.
STABLE_SECONDS: int = 3

# How often (seconds) we re-check a file's size while waiting for it to settle.
POLL_INTERVAL: float = 1.0

# Give up waiting on a single file after this long (e.g. a huge, slow download
# that never settles). The file is left in place.
MAX_WAIT_SECONDS: int = 1800  # 30 minutes

# How often the background re-scan runs. This is what makes any "grace period"
# work: files that were too new to move earlier get picked up on a later pass.
# Keep this comfortably smaller than your shortest grace period so files get
# filed soon after they become eligible (Downloads grace is 20 min, see below).
RESCAN_INTERVAL_SECONDS: int = 300  # 5 minutes

# Default grace period for the Desktop (see WATCHED_DIRS below): a freshly
# saved file stays visible on the Desktop for this many hours before it is
# filed away, so it doesn't vanish while you're using it.
DESKTOP_GRACE_HOURS: int = 6

# File extensions that mean "still downloading" — we never touch these; we
# wait for the browser to rename them to the final name.
TEMP_EXTENSIONS: set[str] = {".crdownload", ".part", ".download", ".partial", ".tmp"}

# Directories that are treated as a single unit (moved whole, never opened).
BUNDLE_EXTENSIONS: set[str] = {".app"}

# ---------------------------------------------------------------------------
# Locations
# ---------------------------------------------------------------------------

APP_DIR = HOME / ".tidy-downloads"          # venv, logs, lock file live here
LOCK_PATH = APP_DIR / "tidy.lock"

LOG_PATH = HOME / "Downloads" / ".tidy-downloads-log.txt"
LOG_MAX_BYTES = 5 * 1024 * 1024             # rotate at ~5 MB
LOG_BACKUPS = 3

# Name of the staging folder used by the (manual) cleanup command.
REVIEW_FOLDER_NAME = "_ToReview"

# ---------------------------------------------------------------------------
# Categories
# ---------------------------------------------------------------------------
# Each category maps to a destination sub-path and a broad "group". The group
# is what the per-folder `categories` filter below matches against, so you can
# say "Desktop should only organize Images" without listing every sub-type.

CATEGORIES: dict[str, dict[str, str]] = {
    "Invoices":       {"path": "Documents/Invoices & Receipts",   "group": "Documents"},
    "Receipts":       {"path": "Documents/Invoices & Receipts",   "group": "Documents"},
    "Contracts":      {"path": "Documents/Contracts & Agreements", "group": "Documents"},
    "Resumes":        {"path": "Documents/Resumes & CVs",         "group": "Documents"},
    "OtherDocuments": {"path": "Documents/Other Documents",       "group": "Documents"},
    "Screenshots":    {"path": "Images/Screenshots",             "group": "Images"},
    "Photos":         {"path": "Images/Photos",                  "group": "Images"},
    "Videos":         {"path": "Videos",                         "group": "Videos"},
    "Audio":          {"path": "Audio",                          "group": "Audio"},
    "Installers":     {"path": "Installers",                     "group": "Installers"},
    "Archives":       {"path": "Archives",                       "group": "Archives"},
    "Misc":           {"path": "Misc",                           "group": "Misc"},
}

# Which category GROUPS get date (month/week) sub-folders. Empty = everything
# stays flat in its category folder (simplest to browse and bulk-delete; find
# files by name, sort the window by Date Created for date order). To bucket a
# group by month, add it here, e.g. {"Images"}, then run `tidy --rebucket`.
DATE_BUCKET_GROUPS: set[str] = set()

# Top-level folder names we create. The watcher ignores these so it never
# reacts to its own moves.
MANAGED_TOP_LEVEL: set[str] = {
    "Documents", "Images", "Videos", "Audio", "Installers", "Archives", "Misc",
    REVIEW_FOLDER_NAME,
}

# ---------------------------------------------------------------------------
# Watched folders
# ---------------------------------------------------------------------------
# Each entry is one folder to watch, with its own behavior:
#   path                : the folder to organize
#   move_delay          : {"mode": "none"}                  -> move as soon as stable
#                         {"mode": "grace", "hours": N}     -> wait N hours after creation
#                         {"mode": "next_day"}              -> only move files not created today
#   date_buckets        : "none" | "monthly" | "weekly"    -> add e.g. /2026-06/ sub-folders
#   categories          : None  -> organize everything
#                         [..]  -> only these category keys or groups (e.g. ["Images"])
#   ignore_recent_hours : never move anything modified within this many hours

WATCHED_DIRS: list[dict] = [
    {
        "path": HOME / "Downloads",
        # Leave a freshly downloaded file in place for 20 minutes so the usual
        # "download then immediately attach it somewhere" flow still finds it
        # loose in Downloads. After that it's filed automatically.
        "move_delay": {"mode": "grace", "minutes": 20},
        "date_buckets": "monthly",          # only DATE_BUCKET_GROUPS (Images) actually bucket
        "categories": None,                 # organize all file types
        "ignore_recent_hours": 0,
    },
    {
        "path": HOME / "Desktop",
        "move_delay": {"mode": "grace", "hours": DESKTOP_GRACE_HOURS},
        "date_buckets": "monthly",          # Screenshots/2026-06/, Images/2026-06/
        "categories": None,                 # organize all file types (folders never moved)
        "ignore_recent_hours": 0,
    },
]

# ---------------------------------------------------------------------------
# Cleanup (manual command only — the watcher never deletes anything)
# ---------------------------------------------------------------------------

STALE_DAYS = 90            # not opened/changed in this long -> stale
INSTALLER_DAYS = 14        # .dmg/.pkg older than this -> probably already installed
ARCHIVE_DAYS = 30          # .zip/.rar etc. older than this
MISC_DAYS = 60             # anything in Misc/ older than this
BIG_MB = 500               # "large" file threshold (used with STALE_DAYS)
MIN_AGE_DAYS = 7           # never touch anything changed within the last week
REVIEW_GRACE_DAYS = 14     # items sit in _ToReview this long before going to Trash

# Filenames matching any of these are never selected for cleanup.
KEEP_PATTERNS = ["*tax*", "*invoice*", "*contract*", "*passport*", "*-final*"]

# Tool version, written to the log on startup.
VERSION = "1.0.0"
