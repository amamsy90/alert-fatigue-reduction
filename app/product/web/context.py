"""Shared state of the web app: where files live and which queue store is in use.

Routes read `STORE` from this module at request time, so tests (and a future
app factory) can swap the store with `use_store` without touching the routes.
"""
from __future__ import annotations

import os
from pathlib import Path

from product.store import QueueStore

APP_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = APP_DIR / "static"
EVAL_DIR = APP_DIR / "data" / "evals"

STORE = QueueStore(os.environ.get("QUEUE_DB") or APP_DIR / "data" / "queue.db")


def use_store(store: QueueStore) -> QueueStore:
    """Point every route at another queue store (tests, or a second database)."""
    global STORE
    STORE = store
    return store
