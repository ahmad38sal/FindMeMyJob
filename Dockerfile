FROM python:3.11-slim

# System deps needed by Playwright Chromium + lxml + psycopg
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    git \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install uv for fast, deterministic installs
RUN pip install --no-cache-dir uv

# Copy dependency manifests first for layer caching
COPY pyproject.toml uv.lock ./
COPY src/ ./src/

# Install Python deps
RUN uv sync --no-dev

# Install Playwright Chromium (required for PDF rendering + apple_internal source)
RUN uv run playwright install --with-deps chromium

# Copy the rest of the project
COPY . .

# Railway sets PORT dynamically; default to 8000 for local docker runs
ENV PORT=8000

# Activate the uv venv so the uvicorn binary is on PATH
ENV PATH="/app/.venv/bin:$PATH"

EXPOSE $PORT

CMD uvicorn findmemyjob.main:app --host 0.0.0.0 --port $PORT
