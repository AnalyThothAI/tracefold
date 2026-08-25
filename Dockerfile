FROM node:22-bookworm-slim AS web-builder

WORKDIR /app/web

COPY web/package.json web/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm \
    set -eu; \
    for attempt in 1 2 3 4 5; do \
        npm ci \
            --no-audit \
            --prefer-offline \
            --fetch-retries=6 \
            --fetch-retry-factor=2 \
            --fetch-retry-mintimeout=10000 \
            --fetch-retry-maxtimeout=120000 \
            --fetch-timeout=300000 \
        && exit 0; \
        npm cache verify || true; \
        sleep "$((attempt * 5))"; \
    done; \
    exit 1

COPY web ./
RUN npm run build


FROM python:3.13-slim-bookworm AS python-deps

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy

WORKDIR /app

RUN set -eu; \
    sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources; \
    printf 'Acquire::Retries "5";\nAcquire::http::Timeout "30";\nAcquire::https::Timeout "30";\n' > /etc/apt/apt.conf.d/80-retries; \
    for attempt in 1 2 3 4 5; do \
        apt-get update \
        && apt-get install -y --no-install-recommends build-essential git \
        && rm -rf /var/lib/apt/lists/* \
        && exit 0; \
        rm -rf /var/lib/apt/lists/*; \
        sleep "$((attempt * 5))"; \
    done; \
    exit 1

RUN python -m pip install --no-cache-dir uv

COPY pyproject.toml uv.lock README.md alembic.ini ./
COPY src ./src

RUN --mount=type=secret,id=github_token \
    --mount=type=cache,target=/root/.cache/uv \
    set -eu; \
    cleanup() { \
        if [ -n "${token:-}" ]; then \
            git config --global --unset-all url."https://x-access-token:${token}@github.com/".insteadOf || true; \
        fi; \
    }; \
    trap cleanup EXIT; \
    if [ -s /run/secrets/github_token ]; then \
        token="$(cat /run/secrets/github_token)"; \
        git config --global url."https://x-access-token:${token}@github.com/".insteadOf "https://github.com/"; \
    fi; \
    for attempt in 1 2 3 4 5; do \
        UV_HTTP_TIMEOUT=300 UV_CONCURRENT_DOWNLOADS=1 uv sync --frozen --no-dev \
        && exit 0; \
        sleep "$((attempt * 5))"; \
    done; \
    exit 1

RUN /app/.venv/bin/python -c \
    'from tracefold.news.program.graph import load_stable_program_artifact; load_stable_program_artifact()'

FROM python:3.13-slim-bookworm

ARG TRACEFOLD_BUILD_REVISION
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TRACEFOLD_RUNTIME_REVISION=${TRACEFOLD_BUILD_REVISION}

LABEL org.opencontainers.image.revision=${TRACEFOLD_BUILD_REVISION}

WORKDIR /app

COPY --from=python-deps /app /app
COPY --from=web-builder /app/web/dist /app/src/tracefold/web/dist

ENV PATH="/app/.venv/bin:${PATH}"

EXPOSE 8765 8766

CMD ["tracefold", "serve"]
