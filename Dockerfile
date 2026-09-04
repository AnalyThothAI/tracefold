FROM node:22-bookworm-slim@sha256:83f487e0a63425e5b4d146fb5e5be574bcbe1b7b843d3ebafdd95eaf7767a7e5 AS web-builder

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
RUN npm run build:checked


FROM python:3.13-slim-bookworm@sha256:c45a22ea000adfd9cda29364bbe7edd23001ce5cc2ad15857cfbf7766943b9ca AS python-deps

ARG UV_VERSION=0.11.7

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

RUN python -m pip install --no-cache-dir "uv==${UV_VERSION}"

COPY pyproject.toml uv.lock README.md alembic.ini ./
COPY tracefold ./tracefold

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
        UV_HTTP_TIMEOUT=300 UV_CONCURRENT_DOWNLOADS=1 uv sync --locked --no-dev \
        && exit 0; \
        sleep "$((attempt * 5))"; \
    done; \
    exit 1

RUN /app/.venv/bin/python -c \
    'from tracefold.news.program.artifact import load_stable_program_artifact; load_stable_program_artifact()'

RUN /app/.venv/bin/python -c \
    'import sys; from importlib.metadata import version; from nautilus_trader.live.node import TradingNode; assert sys.version_info[:2] == (3, 13); assert version("nautilus-trader") == "1.231.0"; assert TradingNode.__module__ == "nautilus_trader.live.node"'

FROM python:3.13-slim-bookworm@sha256:c45a22ea000adfd9cda29364bbe7edd23001ce5cc2ad15857cfbf7766943b9ca AS base

ARG TRACEFOLD_BUILD_REVISION
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TRACEFOLD_RUNTIME_REVISION=${TRACEFOLD_BUILD_REVISION}

LABEL org.opencontainers.image.revision=${TRACEFOLD_BUILD_REVISION}

WORKDIR /app

COPY --from=python-deps /app /app

ENV PATH="/app/.venv/bin:${PATH}"


# The execution runtime (#537 PR-2). It owns a live Binance account, so it gets its own image and
# its own `tracefold-runtime:<sha>` tag: a News/Serve/Workers deploy then changes no bytes this
# process runs, and `make up` has nothing to recreate. It carries no console bundle, because
# nothing in it serves one.
FROM base AS runtime

RUN cd / \
    && python -c 'from tracefold.app.nautilus.root import run_nautilus'

EXPOSE 8767

CMD ["tracefold", "nautilus", "run"]


# Last, therefore the default build target: `docker compose build migrate|serve|workers` and a bare
# `docker build .` must keep producing the console-carrying application image.
FROM base AS app

COPY --from=web-builder /app/web/dist /app/tracefold/web/dist

# The flat layout (#373) makes `/app` an importable package root in its own right, so a probe that
# runs from `/app` cannot tell the installed distribution from the copied tree, and static assets
# copied to the wrong destination would still be reached by a working-directory-relative lookup.
# This probe therefore runs from `/`: it resolves the console script and the package off the image,
# then asserts the frontend bundle sits exactly where `_frontend_dist_dir` walks the package parents
# to find it.
RUN cd / \
    && tracefold --help > /dev/null \
    && python -c \
    'from pathlib import Path; import tracefold; root = Path(tracefold.__file__).resolve().parent; assert root == Path("/app/tracefold"), root; assert (root / "web" / "dist" / "index.html").is_file(), root'

EXPOSE 8765 8766

CMD ["tracefold", "serve"]
