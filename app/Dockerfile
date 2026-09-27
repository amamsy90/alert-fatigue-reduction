FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src ./src
COPY product ./product
COPY examples ./examples

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin appuser \
 && mkdir -p /app/product/data \
 && chown -R appuser:appuser /app

ENV PYTHONPATH=/app
ENV SHADOW_MODE=1
ENV QUEUE_DB=/app/product/data/queue.db
# In-cluster / Compose Prometheus is almost always private; override to 0 to refuse.
ENV PROM_ALLOW_PRIVATE=1

USER appuser

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=2).read()"

CMD ["uvicorn", "product.app:app", "--host", "0.0.0.0", "--port", "8080"]
