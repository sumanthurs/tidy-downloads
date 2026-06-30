"""
Read/write the macOS "Date Added" (kMDItemDateAdded) attribute.

Why this exists: moving a file resets its "Date Added" to the moment of the
move. Finder groups by Date Added, so files we file away would all clump under
"Today" even if they were downloaded months ago. These helpers let the mover
preserve a file's original Date Added so a tidied file still shows its real
date.

Implemented with the BSD getattrlist/setattrlist syscalls via ctypes
(ATTR_CMN_ADDEDTIME). Every function is a safe no-op on non-macOS or on any
error — preserving a date must NEVER cause a file move to fail.
"""

from __future__ import annotations

import ctypes
import struct
import sys

_IS_MAC = sys.platform == "darwin"

_ATTR_BIT_MAP_COUNT = 5
_ATTR_CMN_ADDEDTIME = 0x10000000  # from <sys/attr.h>


class _AttrList(ctypes.Structure):
    _fields_ = [
        ("bitmapcount", ctypes.c_ushort),
        ("reserved", ctypes.c_uint16),
        ("commonattr", ctypes.c_uint),
        ("volattr", ctypes.c_uint),
        ("dirattr", ctypes.c_uint),
        ("fileattr", ctypes.c_uint),
        ("forkattr", ctypes.c_uint),
    ]


def _libc():
    libc = ctypes.CDLL("libc.dylib", use_errno=True)
    for fn in (libc.setattrlist, libc.getattrlist):
        fn.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.c_void_p,
                       ctypes.c_size_t, ctypes.c_ulong]
        fn.restype = ctypes.c_int
    return libc


def _attrlist() -> _AttrList:
    al = _AttrList()
    al.bitmapcount = _ATTR_BIT_MAP_COUNT
    al.commonattr = _ATTR_CMN_ADDEDTIME
    return al


def get_date_added(path) -> float | None:
    """Return the file's Date Added as a Unix timestamp, or None if unset or
    on any error."""
    if not _IS_MAC:
        return None
    try:
        libc = _libc()
        buf = ctypes.create_string_buffer(64)
        if libc.getattrlist(str(path).encode(), ctypes.byref(_attrlist()), buf, len(buf), 0) != 0:
            return None
        # Buffer: [u_int32 length][struct timespec tv_sec(8) tv_nsec(8)], packed.
        sec, nsec = struct.unpack_from("<qq", buf.raw, 4)
        return float(sec) + nsec / 1e9 if sec > 0 else None
    except Exception:
        return None


def set_date_added(path, epoch: float) -> bool:
    """Set the file's Date Added to the given Unix timestamp. Returns True on
    success, False on non-macOS, a non-positive timestamp, or any error."""
    if not _IS_MAC or not epoch or epoch <= 0:
        return False
    try:
        libc = _libc()
        sec = int(epoch)
        nsec = int((epoch - sec) * 1e9)
        buf = struct.pack("qq", sec, nsec)
        return libc.setattrlist(str(path).encode(), ctypes.byref(_attrlist()), buf, len(buf), 0) == 0
    except Exception:
        return False
