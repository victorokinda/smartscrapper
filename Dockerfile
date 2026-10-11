FROM python:3.12-slim

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Create non-root user
RUN groupadd -g 1000 appuser && \
    useradd -u 1000 -g appuser -s /bin/bash -m appuser

# Install uv for fast, reliable package management
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

# Copy application files and install
COPY pyproject.toml README.md ./
COPY app/ ./app/
COPY static/ ./static/
RUN uv pip install --system --no-cache ".[dev]"

# Configure Playwright directory and install Chromium with system dependencies
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN apt-get update && \
    python -m playwright install --with-deps chromium && \
    mkdir -p /ms-playwright && \
    chown -R appuser:appuser /ms-playwright && \
    rm -rf /var/lib/apt/lists/*

# Prepare persistent mount directories with permissions
RUN mkdir -p /app/downloads /app/data && \
    chown -R appuser:appuser /app

USER appuser

ENV DOWNLOAD_DIR=/app/downloads
ENV DATA_DIR=/app/data
ENV PYTHONUNBUFFERED=1
ENV ENABLE_BROWSER_RENDERING=true

EXPOSE 8080

CMD ["uvicorn", "app.web:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
