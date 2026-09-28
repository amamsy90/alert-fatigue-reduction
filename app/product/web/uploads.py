"""Upload and input helpers shared by the routes."""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import HTTPException, UploadFile


def _max_upload_bytes() -> int:
    try:
        return int(float(os.environ.get("MAX_UPLOAD_MB", "200")) * 1024 * 1024)
    except ValueError:
        return 200 * 1024 * 1024


async def _save_upload(upload: UploadFile | None, path: Path) -> Path | None:
    """Write an upload in chunks; refuse files above MAX_UPLOAD_MB (default 200)."""
    if upload is None or not upload.filename:
        return None
    limit = _max_upload_bytes()
    written = 0
    with path.open("wb") as out:
        while chunk := await upload.read(1024 * 1024):
            written += len(chunk)
            if written > limit:
                raise HTTPException(413, f"{upload.filename} is larger than {limit // (1024 * 1024)} MB")
            out.write(chunk)
    return path


def _budget(value) -> int:
    try:
        k = int(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, f"budget_k must be a whole number, got {value!r}") from exc
    if not 1 <= k <= 10_000:
        raise HTTPException(400, "budget_k must be between 1 and 10000")
    return k
