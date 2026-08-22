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

ARG TRACEFOLD_NEWS_PROGRAM_PROFILE=d_stable

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
COPY deploy/news-program-v3-rollback /tmp/tracefold-news-program-v3-rollback
COPY --from=web-builder /app/web/dist ./src/tracefold/web/dist

RUN set -eu; \
    case "${TRACEFOLD_NEWS_PROGRAM_PROFILE}" in \
        d_stable) \
            ;; \
        program_v3_rollback) \
            rollback_sha="$(python -c 'import json; print(json.load(open("/tmp/tracefold-news-program-v3-rollback/registry.json", encoding="utf-8"))["stable"])')"; \
            find src/tracefold/news/agents/programs -mindepth 1 -maxdepth 1 -type d -exec rm -rf '{}' +; \
            cp /tmp/tracefold-news-program-v3-rollback/registry.json src/tracefold/news/agents/programs/registry.json; \
            cp -R "/tmp/tracefold-news-program-v3-rollback/${rollback_sha}" src/tracefold/news/agents/programs/; \
            ;; \
        *) \
            echo "Unknown TRACEFOLD_NEWS_PROGRAM_PROFILE=${TRACEFOLD_NEWS_PROGRAM_PROFILE}" >&2; \
            exit 2; \
            ;; \
    esac

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
        if UV_HTTP_TIMEOUT=300 UV_CONCURRENT_DOWNLOADS=1 uv sync --frozen --no-dev \
            && python -m venv /opt/coinglass-cli \
            && UV_HTTP_TIMEOUT=300 UV_CONCURRENT_DOWNLOADS=1 uv pip install \
                --python /opt/coinglass-cli/bin/python \
                "git+https://github.com/AnalyThothAI/coinglass-cli.git@dc8f9d253a8dc1fded6fabcef93c96feeaa4b826" \
            && /opt/coinglass-cli/bin/python -c 'import coinglass_cli'; then \
            exit 0; \
        fi; \
        rm -rf /opt/coinglass-cli; \
        sleep "$((attempt * 5))"; \
    done; \
    exit 1

RUN set -eu; \
    loaded_sha="$(/app/.venv/bin/python -c 'from tracefold.news.agents.semantic_program import load_stable_program_artifact; print(load_stable_program_artifact().program_sha256)')"; \
    if [ "${TRACEFOLD_NEWS_PROGRAM_PROFILE}" = "program_v3_rollback" ]; then \
        expected_sha="$(python -c 'import json; print(json.load(open("/tmp/tracefold-news-program-v3-rollback/registry.json", encoding="utf-8"))["stable"])')"; \
        [ "${loaded_sha}" = "${expected_sha}" ] || { \
            echo "Rollback Program identity mismatch: loaded=${loaded_sha} expected=${expected_sha}" >&2; \
            exit 2; \
        }; \
    fi


FROM python:3.13-slim-bookworm

ARG TRACEFOLD_BUILD_REVISION
ARG TRACEFOLD_NEWS_PROGRAM_PROFILE=d_stable
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TRACEFOLD_RUNTIME_REVISION=${TRACEFOLD_BUILD_REVISION}

LABEL org.opencontainers.image.revision=${TRACEFOLD_BUILD_REVISION} \
    io.tracefold.news.program.profile=${TRACEFOLD_NEWS_PROGRAM_PROFILE}

WORKDIR /app

COPY --from=python-deps /app /app
COPY --from=python-deps /opt/coinglass-cli /opt/coinglass-cli

ENV PATH="/app/.venv/bin:${PATH}"

EXPOSE 8765 8766

CMD ["tracefold", "serve"]
