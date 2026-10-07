"""FastAPI on-call alert queue: wires the security middleware and the routes.

The code lives in product/web/: security.py (login, body limit, audit),
schemas.py, uploads.py, and one module per group of routes.
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from product.logging_config import configure_logging
from product.monitoring import metrics_middleware
from product.store import QueueStore
from product.web import context, evaluate_routes, job_routes, ops_routes
from product.web.security import BodyLimit, _is_private, audit_changes, require_login  # noqa: F401
from product.web.security import router as login_router

configure_logging()

app = FastAPI(
    title="On-call alert queue",
    description="Escalate / ask / hide under a daily budget for cloud microservice on-call.",
    version="1.0.0",
)

# Order matters: the last one added runs first. audit_changes sees every
# outcome (401/403/413 too), BodyLimit stops big bodies before login runs,
# require_login guards the routes.
app.middleware("http")(require_login)
app.add_middleware(BodyLimit)
app.middleware("http")(audit_changes)
app.middleware("http")(metrics_middleware)  # outermost: times everything, refusals included

context.STATIC_DIR.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(context.STATIC_DIR)), name="static")
app.include_router(login_router)
app.include_router(ops_routes.router)
app.include_router(job_routes.router)
app.include_router(evaluate_routes.router)


def use_store(store: QueueStore) -> QueueStore:
    """Point the app at another queue store (tests use a temporary database)."""
    return context.use_store(store)


def __getattr__(name: str):
    # `product.app.STORE` stays readable; it always means the store in use.
    if name == "STORE":
        return context.STORE
    raise AttributeError(name)


def create_app() -> FastAPI:
    return app
