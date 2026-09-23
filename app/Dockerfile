FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src ./src
COPY product ./product
COPY data ./data

ENV PYTHONPATH=/app
ENV SHADOW_MODE=1
ENV QUEUE_DB=/app/product/data/queue.db

EXPOSE 8080

CMD ["uvicorn", "product.app:app", "--host", "0.0.0.0", "--port", "8080"]
