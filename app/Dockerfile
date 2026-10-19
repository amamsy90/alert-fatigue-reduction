# CPU image. CI builds this file. The optional CUDA image is Dockerfile.gpu.
FROM python:3.12-slim

WORKDIR /app

# Exact versions with hashes, exported from uv.lock (`uv export --no-dev`); CI checks it is current.
COPY requirements.lock .
RUN pip install --no-cache-dir --require-hashes -r requirements.lock

COPY src ./src
COPY product ./product
COPY examples ./examples

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin appuser \
 && mkdir -p /app/product/data /app/data/sample_product \
 && chown -R appuser:appuser /app

ENV PYTHONPATH=/app
ENV SHADOW_MODE=1
ENV QUEUE_DB=/app/product/data/queue.db
# Private Prometheus (in-cluster / Compose) is refused by default (SSRF). Set 1 only when
# the app sits on a trusted network; cloud metadata is always refused.
ENV PROM_ALLOW_PRIVATE=0

USER appuser

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=2).read()"

# One worker on purpose: sessions, the login lock and rate limits live in this process
# (ADR 0003, docs/deployment.md §3a). Do not raise --workers.
CMD ["uvicorn", "product.app:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
