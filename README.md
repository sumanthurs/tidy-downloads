# Tidy Downloads

Your Downloads and Desktop, cleaned up automatically. Every time you download
or save a file, this tool quietly files it into the right folder for you —
invoices with invoices, screenshots with screenshots, installers with
installers. It runs in the background, so once it's set up you never have to
think about it.

It **never deletes your files** on its own. Sorting is automatic; clearing out
old junk is something *you* choose to do, and even then files go to the Trash
(recoverable), never gone for good.

> Personal macOS tool. Not an official Apple product.

---

## What it does

**Before** — one messy pile:

```
Downloads/   invoice.pdf  movie.mp4  Screenshot.png  app.dmg  photo.jpg  ...
```

**After** — everything filed:

```
Downloads/
├── Documents/
│   ├── Invoices & Receipts/
│   ├── Contracts & Agreements/
│   ├── Resumes & CVs/
│   └── Other Documents/
├── Images/   (Screenshots/ · Photos/)
├── Videos/   Audio/   Installers/   Archives/   Misc/
```

Your **Desktop** gets the same treatment, but screenshots and images are filed
into month folders (e.g. `Screenshots/2026-06/`), and brand-new files are left
alone for a few hours so they don't disappear while you're using them.

---

## How it works (plain version)

1. The tool watches your Downloads and Desktop folders.
2. When a new file finishes downloading, it works out what it is — first from
   the file name, then, for PDFs / Word docs / images, by peeking inside (an
   "Invoice Number" inside a PDF means it's an invoice; camera info inside a
   photo means it's a photo, not a screenshot).
3. It moves the file into the matching folder.
4. It writes a line in a log so you can see what it did.

It only organizes files that arrive **after** it starts. Your existing pile is
left alone unless you ask for it (see *Sort what's already there*).

---

## How to run commands

A few things below use **Terminal**. To open it: press `Cmd`+`Space`, type
`Terminal`, press `Enter`. Then copy a command, paste it, and press `Enter`.
You only need this for one-off tasks — everyday sorting needs no commands.

---

## Setup (do this once)

```bash
git clone <your-repo-url>
cd tidy-downloads
./install_launch_agent.sh
```

Then **grant permission** so macOS lets it read your files:

> System Settings → Privacy & Security → **Full Disk Access** → turn it on for
> the Python the installer prints (it lives at
> `~/.tidy-downloads/venv/bin/python`).

Without this, macOS may silently block the tool and nothing will happen.

That's it — it now starts automatically every time you log in.

---

## Everyday use

Nothing! Just download files as normal and watch them get filed. The commands
below are only for extra, optional tasks. After install you have a short
command called `tidy`:

| I want to…                               | Command                          |
|------------------------------------------|----------------------------------|
| Sort files already in Downloads/Desktop  | `tidy --sweep`                   |
| See what could be cleaned up (no change) | `tidy --cleanup`                 |
| Move old/junk files to a review folder   | `tidy --cleanup --apply`         |
| Send reviewed files to the Trash         | `tidy --empty-reviewed --apply`  |
| Put reviewed files back                  | `tidy --restore`                 |
| Test how one file would be sorted        | `tidy --once ~/Downloads/x.pdf`  |

Add `--target downloads` or `--target desktop` to limit a command to one
folder (default is both).

Tip: `--cleanup` always shows a list first and changes nothing. Add `--apply`
only when you're happy with the list.

---

## Sort what's already there

When you first install, your old files stay put. To organize the existing pile
once:

```bash
tidy --sweep
```

---

## Organizing your Desktop too

The Desktop is where macOS dumps every screenshot, so it fills up fast. This
tool files Desktop **screenshots and images** into month folders like
`Screenshots/2026-06/` — real folders, not just a temporary view.

Two things keep it from being annoying:

- **Recent files are left alone** for a few hours, so a file you just saved
  doesn't vanish while you're working with it.
- **Documents and folders you parked on the Desktop are left in place** — only
  screenshots/images are moved by default.

To change any of this (turn date folders on/off, include more file types,
adjust the delay), edit `config.py` — see *Customize* below.

---

## Cleaning up old files (optional, and safe)

The tool **never** deletes files on its own. When *you* run cleanup, "delete"
happens in two safe steps:

1. Old or junk files are moved to a `_ToReview` folder inside Downloads/Desktop
   — you can look through it and pull anything back.
2. Items left in `_ToReview` for 14 days are then sent to the macOS Trash,
   where they can still be recovered for ~30 days.

So a file survives **two** safety nets before it's truly gone. Important files
(invoices, contracts, anything in `Documents/`, or anything you opened
recently) are protected and won't be picked. Want a file back?
`tidy --restore`, or open the Trash.

---

## Check that it's running

```bash
launchctl list | grep tidydownloads
```

A line means it's running.

---

## See what it's been doing (the log)

```bash
tail -f ~/Downloads/.tidy-downloads-log.txt
```

Press `Ctrl`+`C` to stop watching. The log is a hidden file — in Finder press
`Cmd`+`Shift`+`.` to show hidden files. Each line shows the time, the action
(`MOVED` / `STAGED` / `TRASHED` / `SKIP` / `ERROR`), the file name, and where
it went.

---

## Customize

No programming needed for small tweaks:

- **Keywords** (which words send a file where): edit the lists at the top of
  `categorizer.py` (e.g. add `"subscription"` to the invoice words).
- **Settings** (folders watched, timings, ages, Desktop grace period, date
  folders): edit `config.py`.

After editing, restart the tool:

```bash
./uninstall_launch_agent.sh && ./install_launch_agent.sh
```

---

## Turn it off / uninstall

```bash
./uninstall_launch_agent.sh
```

This stops the tool and removes it from login. **It does not touch or delete
any of your files** — your Downloads and Desktop stay exactly as they are.

---

## Troubleshooting

- **Nothing is being sorted** → check Full Disk Access (Setup step 2), then
  confirm it's running (`launchctl list | grep tidydownloads`). Also check
  `~/.tidy-downloads/stderr.log`.
- **The `tidy` command isn't found** → add `~/.local/bin` to your PATH (the
  installer prints the exact line), or run
  `~/.tidy-downloads/venv/bin/python ~/path/to/tidy_downloads.py --sweep`.
- **A file went to the wrong folder** → that's just a sorting rule; tweak the
  keywords in `categorizer.py`.
- **I want a file back from cleanup** → `tidy --restore`, or open the Trash.

---

## Good to know

- macOS only (it uses macOS features).
- Never deletes files on its own; cleanup is manual and goes to the Trash.
- If it can't read a file, it leaves the file alone and notes it in the log —
  it won't crash or lose your file.
- Never overwrites: if a file with the same name already exists, the new one
  gets a timestamp added to its name.
