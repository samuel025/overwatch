FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir "fastapi>=0.110" "uvicorn>=0.29" "httpx>=0.27" "psycopg[binary]>=3.1" "psycopg-pool>=3.2" \
    "opentelemetry-sdk>=1.27" "opentelemetry-exporter-otlp-proto-http>=1.27" "opentelemetry-instrumentation-fastapi" \
    "opentelemetry-instrumentation-httpx"
COPY app.py chaos.py collector.py loadgen.py ./
# --limit-concurrency sheds load with 503s instead of falling over when a surge arrives.
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log", "--limit-concurrency", "200"]
