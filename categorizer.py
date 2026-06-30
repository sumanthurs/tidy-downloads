"""
Tidy Downloads — detection logic.

Given a file, decide which category it belongs to. Detection runs in layers
(fast and cheap first, slower content inspection last); the first confident
match wins. Every function returns a CATEGORY KEY (see config.CATEGORIES) or
None to "fall through" to the next layer.

The keyword and signal lists at the top are meant to be edited. Add or remove
words to tune how files are sorted — no other code needs to change.
"""

from __future__ import annotations

import re
from pathlib import Path

# ===========================================================================
# EDITABLE KEYWORD / SIGNAL LISTS
# ===========================================================================

# --- Layer 1: filename keywords (matched case-insensitively, word-aware) ---
FILENAME_INVOICE = [
    "invoice", "receipt", "bill", "order", "payment", "tax-invoice",
    "tax invoice", "statement",
]
FILENAME_RESUME = ["resume", "cv", "curriculum-vitae", "curriculum vitae"]
FILENAME_CONTRACT = [
    "contract", "agreement", "nda", "terms", "offer-letter", "offer letter",
    "msa", "sow",
]
SCREENSHOT_PREFIXES = ["screenshot", "screen shot", "cleanshot"]

# --- Layer 2: phrases searched for inside PDF / .docx text ---
INVOICE_SIGNALS = ["invoice number", "amount due", "bill to", "tax invoice"]
RECEIPT_SIGNALS = ["receipt", "payment received", "thank you for your purchase"]
RESUME_SIGNALS = ["work experience", "education", "skills"]
CONTRACT_SIGNALS = [
    "this agreement", "hereby agree", "non-disclosure", "party of the first part",
]

# --- File-type baselines (Layer 0) ---
VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm"}
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".flac", ".aac"}
INSTALLER_EXTS = {".dmg", ".pkg", ".app"}
ARCHIVE_EXTS = {".zip", ".rar", ".7z", ".tar", ".gz", ".tgz"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".heic", ".webp", ".gif", ".bmp", ".tiff", ".tif"}

# Common screen resolutions — a weak, last-resort hint that an image with no
# camera data is a screenshot. (width, height); reversed pairs are also checked.
SCREEN_RESOLUTIONS = {
    (1920, 1080), (2560, 1440), (3840, 2160), (1440, 900), (1680, 1050),
    (2880, 1800), (3024, 1964), (3456, 2234), (2560, 1600), (1366, 768),
}

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
PHONE_RE = re.compile(r"(\+?\d[\d\s().-]{7,}\d)")


# ===========================================================================
# Errors
# ===========================================================================

class ContentReadError(Exception):
    """Raised when a file's content genuinely could not be read (corrupt,
    permission denied, etc.). The caller logs an ERROR and leaves the file in
    place. Note: a PDF with *no extractable text* is NOT an error."""


# ===========================================================================
# Small text helpers
# ===========================================================================

def _normalize(text: str) -> str:
    """Lower-case and turn any run of non-alphanumerics into single spaces.
    'Invoice_Amazon-2026.pdf' -> 'invoice amazon 2026'."""
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _matches_any(stem: str, keywords: list[str]) -> bool:
    """True if the (normalized) filename stem contains any keyword. Single-word
    keywords match whole tokens (so 'cv' won't match 'service'); multi-word
    keywords match as a normalized substring."""
    norm = _normalize(stem)
    tokens = set(norm.split())
    for kw in keywords:
        nkw = _normalize(kw)
        if not nkw:
            continue
        if " " in nkw:
            if nkw in norm:
                return True
        elif nkw in tokens:
            return True
    return False


def _count_signals(text_lower: str, signals: list[str]) -> int:
    return sum(1 for s in signals if s in text_lower)


# ===========================================================================
# Layer 0 — extension baseline
# ===========================================================================

def categorize_by_extension(path: Path) -> str | None:
    """Fast path for media/installer/archive types. Returns None for PDFs,
    .docx and images so they continue to filename / content inspection."""
    ext = path.suffix.lower()
    if ext in VIDEO_EXTS:
        return "Videos"
    if ext in AUDIO_EXTS:
        return "Audio"
    if ext in INSTALLER_EXTS:
        return "Installers"
    if ext in ARCHIVE_EXTS:
        return "Archives"
    return None


# ===========================================================================
# Layer 1 — filename matching
# ===========================================================================

def categorize_by_filename(path: Path) -> str | None:
    stem = path.stem
    norm = _normalize(path.name)
    if any(norm.startswith(_normalize(p)) for p in SCREENSHOT_PREFIXES):
        return "Screenshots"
    if _matches_any(stem, FILENAME_INVOICE):
        return "Invoices"
    if _matches_any(stem, FILENAME_RESUME):
        return "Resumes"
    if _matches_any(stem, FILENAME_CONTRACT):
        return "Contracts"
    return None


# ===========================================================================
# Layer 2 — document content (PDF / .docx)
# ===========================================================================

def _classify_document_text(text: str) -> str:
    """Apply the shared signal logic to extracted document text. Always returns
    a Documents category; 'OtherDocuments' when nothing is confident
    (fewer than 2 signal hits)."""
    lower = text.lower()
    first_500 = lower[:500]

    scores = {
        "Invoices": _count_signals(lower, INVOICE_SIGNALS),
        "Receipts": _count_signals(lower, RECEIPT_SIGNALS),
        "Contracts": _count_signals(lower, CONTRACT_SIGNALS),
    }

    # Resume needs the section words AND a contact detail near the top.
    resume_words = _count_signals(lower, RESUME_SIGNALS)
    has_contact = bool(EMAIL_RE.search(first_500) or PHONE_RE.search(first_500))
    if resume_words >= 2 and has_contact:
        scores["Resumes"] = resume_words + 1  # bonus so it wins ties

    best = max(scores, key=scores.get)
    if scores[best] >= 2:
        return best
    return "OtherDocuments"


def _extract_pdf_text(path: Path, max_pages: int = 2) -> str:
    """Extract text from the first `max_pages` pages. Returns '' when the PDF
    has no extractable text (scanned/encrypted) — that is not an error."""
    import pdfplumber

    try:
        parts: list[str] = []
        with pdfplumber.open(str(path)) as pdf:
            for page in pdf.pages[:max_pages]:
                parts.append(page.extract_text() or "")
        return "\n".join(parts)
    except Exception as exc:  # genuinely unreadable
        raise ContentReadError(f"Could not read PDF content: {exc}") from exc


def categorize_pdf_content(path: Path) -> str:
    return _classify_document_text(_extract_pdf_text(path))


def _extract_docx_text(path: Path) -> str:
    import docx  # python-docx

    try:
        document = docx.Document(str(path))
        return "\n".join(p.text for p in document.paragraphs)
    except Exception as exc:
        raise ContentReadError(f"Could not read DOCX content: {exc}") from exc


def categorize_docx_content(path: Path) -> str:
    return _classify_document_text(_extract_docx_text(path))


# ===========================================================================
# Layer 2 — images
# ===========================================================================

def categorize_image(path: Path) -> str:
    """Photo vs screenshot, using EXIF camera tags, screenshot metadata, the
    filename, and (last resort) the pixel dimensions."""
    from PIL import Image, ExifTags

    # Enable HEIC support if available (iPhone photos).
    try:
        import pillow_heif  # noqa: F401
        pillow_heif.register_heif_opener()
    except Exception:
        pass

    try:
        with Image.open(str(path)) as img:
            width, height = img.size
            exif = img.getexif()
            info = {str(k).lower(): str(v) for k, v in (img.info or {}).items()}
    except Exception as exc:
        raise ContentReadError(f"Could not read image: {exc}") from exc

    # 1) Real camera photos carry Make / Model EXIF tags.
    make = exif.get(_exif_tag(ExifTags, "Make"))
    model = exif.get(_exif_tag(ExifTags, "Model"))
    if make or model:
        return "Photos"

    # 2) Screenshot hints: filename, PNG metadata, or known screen resolution.
    norm_name = _normalize(path.name)
    if any(norm_name.startswith(_normalize(p)) for p in SCREENSHOT_PREFIXES):
        return "Screenshots"

    meta_blob = " ".join(info.get(k, "") for k in ("software", "imagedescription")).lower()
    if "screenshot" in meta_blob or "screen shot" in meta_blob:
        return "Screenshots"

    if (width, height) in SCREEN_RESOLUTIONS or (height, width) in SCREEN_RESOLUTIONS:
        return "Screenshots"

    # 3) Default for images.
    return "Photos"


def _exif_tag(exif_tags, name: str) -> int:
    """Resolve an EXIF tag name (e.g. 'Make') to its numeric id."""
    for tag_id, tag_name in exif_tags.TAGS.items():
        if tag_name == name:
            return tag_id
    return -1


# ===========================================================================
# Orchestrator
# ===========================================================================

def categorize(path: Path) -> str:
    """Run the layers in order and return a final category key. Always returns
    something (defaults to 'Misc'). May raise ContentReadError if a document or
    image is genuinely unreadable — the caller logs that and leaves the file."""
    ext = path.suffix.lower()

    key = categorize_by_extension(path)
    if key:
        return key

    key = categorize_by_filename(path)
    if key:
        return key

    if ext == ".pdf":
        return categorize_pdf_content(path)
    if ext == ".docx":
        return categorize_docx_content(path)
    if ext in IMAGE_EXTS:
        return categorize_image(path)

    return "Misc"
