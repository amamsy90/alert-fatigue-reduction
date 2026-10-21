"""SHA-256 sidecars for optional model files. Never pickle."""
from __future__ import annotations

import hashlib
from pathlib import Path


def digest(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


def _safetensors_header(blob: bytes) -> bool:
    """True when the bytes look like a safetensors file (length-prefixed JSON header)."""
    if len(blob) < 10:
        return False
    header_len = int.from_bytes(blob[:8], "little")
    if header_len <= 0 or header_len > len(blob) - 8:
        return False
    return blob[8 : 8 + 1] == b"{"


def looks_like_pickle(blob: bytes) -> bool:
    """Pickle protocols 2–5 start with ``\\x80`` and a version byte.

    A safetensors length can collide with that prefix, so a JSON header wins.
    """
    if _safetensors_header(blob):
        return False
    return len(blob) >= 2 and blob[0] == 0x80 and blob[1] in (2, 3, 4, 5)


def looks_unsafe(blob: bytes) -> bool:
    """Zip (including a skops or pickle jar) or a pickle stream."""
    return blob.startswith(b"PK") or looks_like_pickle(blob)


def save_bytes(path: Path, blob: bytes) -> str:
    if looks_unsafe(blob):
        raise ValueError(f"refusing to write {path.name}: pickle and zip are not weight files")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(blob)
    hexdigest = digest(blob)
    path.with_name(path.name + ".sha256").write_text(hexdigest + "\n", encoding="ascii")
    return hexdigest


def load_bytes(path: Path, expected: str | None = None) -> tuple[bytes | None, str]:
    """Return ``(bytes, "ok")`` or ``(None, status)``.

    Status is ``missing``, ``missing-hash``, ``hash-mismatch`` or ``bad-format``.
    A hash match is not enough: a pickle or a zip is still refused.
    """
    if not path.is_file():
        return None, "missing"
    sidecar = path.with_name(path.name + ".sha256")
    if not sidecar.is_file():
        return None, "missing-hash"
    recorded = sidecar.read_text(encoding="ascii").strip().split()
    if not recorded:
        return None, "missing-hash"
    blob = path.read_bytes()
    actual = digest(blob)
    if actual != recorded[0].lower():
        return None, "hash-mismatch"
    pinned = (expected or "").strip().lower()
    if pinned and pinned != actual:
        return None, "hash-mismatch"
    if looks_unsafe(blob):
        return None, "bad-format"
    return blob, "ok"
