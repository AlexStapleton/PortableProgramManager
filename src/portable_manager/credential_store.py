"""Encrypt and decrypt secrets using the Windows Data Protection API (DPAPI).

DPAPI ties the ciphertext to the current Windows user account, so only the
same user on the same machine can decrypt.  No extra dependencies are needed
— everything goes through ``ctypes`` and ``kernel32``/``crypt32``.

Fallback: on non-Windows platforms (or if the DPAPI calls fail) the module
silently falls back to plaintext so the application remains functional.
"""

from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes
import logging
import platform

log = logging.getLogger(__name__)

_IS_WINDOWS = platform.system() == "Windows"

# Prefix markers so we can distinguish encrypted vs plaintext tokens in JSON.
_DPAPI_PREFIX = "dpapi:"

# ── Windows DPAPI structures ──────────────────────────────────────────────


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", ctypes.wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


def _blob_from_bytes(data: bytes) -> _DATA_BLOB:
    blob = _DATA_BLOB()
    blob.cbData = len(data)
    blob.pbData = ctypes.cast(ctypes.create_string_buffer(data, len(data)),
                              ctypes.POINTER(ctypes.c_char))
    return blob


# ── Public API ────────────────────────────────────────────────────────────


def encrypt_token(plaintext: str) -> str:
    """Return an opaque string safe to persist in a JSON file.

    On Windows the token is encrypted with DPAPI and base64-encoded with a
    ``dpapi:`` prefix.  On other platforms (or on error) the plaintext is
    returned unchanged so the application still works.
    """
    if not plaintext:
        return ""
    if not _IS_WINDOWS:
        return plaintext
    try:
        data_in = _blob_from_bytes(plaintext.encode("utf-8"))
        data_out = _DATA_BLOB()

        if not ctypes.windll.crypt32.CryptProtectData(
            ctypes.byref(data_in),
            None,   # description (unused)
            None,   # optional entropy
            None,   # reserved
            None,   # prompt struct
            0,      # flags
            ctypes.byref(data_out),
        ):
            log.warning("CryptProtectData failed; storing token in plaintext")
            return plaintext

        enc_bytes = ctypes.string_at(data_out.pbData, data_out.cbData)
        # Free the buffer allocated by Windows.
        ctypes.windll.kernel32.LocalFree(data_out.pbData)
        return _DPAPI_PREFIX + base64.b64encode(enc_bytes).decode("ascii")
    except Exception:
        log.warning("DPAPI encryption failed; storing token in plaintext", exc_info=True)
        return plaintext


def is_encrypted_token(stored: str) -> bool:
    """True if *stored* is a DPAPI-protected value written by :func:`encrypt_token`."""
    return isinstance(stored, str) and stored.startswith(_DPAPI_PREFIX)


def decrypt_token(stored: str) -> str:
    """Recover the plaintext token from a value previously returned by
    :func:`encrypt_token`.

    If the value does not carry the ``dpapi:`` prefix it is assumed to be
    plaintext (legacy / non-Windows) and returned as-is.
    """
    if not stored:
        return ""
    if not stored.startswith(_DPAPI_PREFIX):
        return stored  # plaintext (legacy or non-Windows)
    if not _IS_WINDOWS:
        log.warning("DPAPI-encrypted token found on non-Windows platform; cannot decrypt")
        return ""
    try:
        enc_bytes = base64.b64decode(stored[len(_DPAPI_PREFIX):])
        data_in = _blob_from_bytes(enc_bytes)
        data_out = _DATA_BLOB()

        if not ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(data_in),
            None,   # description out (unused)
            None,   # optional entropy
            None,   # reserved
            None,   # prompt struct
            0,      # flags
            ctypes.byref(data_out),
        ):
            log.error("CryptUnprotectData failed; token may be corrupt or from another user")
            return ""

        plaintext = ctypes.string_at(data_out.pbData, data_out.cbData).decode("utf-8")
        ctypes.windll.kernel32.LocalFree(data_out.pbData)
        return plaintext
    except Exception:
        log.error("DPAPI decryption failed", exc_info=True)
        return ""
