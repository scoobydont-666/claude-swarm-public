FROM python:3.12-slim AS base

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

COPY pyproject.toml uv.lock* ./

# BuildKit secret: pass GH_TOKEN for any private git deps (e.g., future hydra-ipc pin).
# Usage: DOCKER_BUILDKIT=1 docker build --secret id=gh_token,env=GH_TOKEN -t claude-swarm .
RUN --mount=type=secret,id=gh_token \
    if [ -f /run/secrets/gh_token ]; then \
        git config --global url."https://$(cat /run/secrets/gh_token)@github.com/".insteadOf "https://github.com/"; \
    fi && \
    (uv sync --frozen --no-dev --no-install-project 2>/dev/null || uv sync --no-dev --no-install-project) && \
    git config --global --unset-all url."https://github.com/".insteadOf 2>/dev/null || true

COPY src/ ./src/

RUN uv sync --frozen --no-dev 2>/dev/null || uv sync --no-dev

ENV PYTHONPATH=/app/src

EXPOSE 9192

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import httpx; r = httpx.get('http://127.0.0.1:9192/health'); r.raise_for_status()"

CMD ["uv", "run", "uvicorn", "dashboard:app", "--host", "0.0.0.0", "--port", "9192"]
