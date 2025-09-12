# Stage 1: Build the application
FROM mirror.gcr.io/library/python:3.13-slim-bookworm AS builder

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:0.8.4 /uv /uvx /bin/

RUN apt-get update && apt-get install -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

ENV PATH="/root/.local/bin:$PATH"

ENV PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1 \
    PIP_NO_CACHE_DIR=off \
    PIP_DISABLE_PIP_VERSION_CHECK=on \
    PIP_DEFAULT_TIMEOUT=100 \
    PYSETUP_SETUPTOOLS_SCM_PRETEND_VERSION_FOR_PYPI="0.0.0" \
    VENV_PATH="/app/.venv"


# Copy only dependency files first for better layer caching
COPY pyproject.toml uv.lock* ./
COPY src /app/src

RUN uv sync --frozen --no-dev

# Stage 2: Final image
FROM mirror.gcr.io/library/python:3.13-slim-bookworm

WORKDIR /app

COPY --from=builder /app/.venv /opt/venv
COPY --from=builder /app/src /app/src

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1

EXPOSE 9000

CMD ["python", "-m", "uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "9000", "--proxy-headers", "--forwarded-allow-ips", "*"]
