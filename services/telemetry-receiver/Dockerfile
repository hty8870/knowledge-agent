# Generic telemetry receiver container. Runtime configuration is supplied by the operator.
FROM python:3.12-slim@sha256:876416ecde9aca2bcc90e1fb0c7a9500bbf749f5788b70f82d4c5a5c2357f8b4

WORKDIR /app

COPY requirements.lock ./
ARG PIP_INDEX_URL=https://mirrors.cloud.tencent.com/pypi/simple
RUN pip install --index-url "$PIP_INDEX_URL" --require-hashes --no-cache-dir -r requirements.lock

COPY app.py ./
COPY telemetry_idempotency.py ./
COPY scripts/ ./scripts/

RUN useradd --create-home telemetry
USER telemetry

EXPOSE 8471
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8471"]
