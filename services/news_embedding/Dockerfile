FROM python:3.12.13-slim-bookworm@sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2

ARG TORCH_VERSION=2.14.1+cpu
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu
ARG TRACEFOLD_BUILD_REVISION

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp \
    HF_HOME=/weights \
    HF_HUB_CACHE=/weights \
    TOKENIZERS_PARALLELISM=false \
    HF_HUB_DISABLE_TELEMETRY=1 \
    TRACEFOLD_BUILD_REVISION=${TRACEFOLD_BUILD_REVISION}
LABEL org.opencontainers.image.revision=${TRACEFOLD_BUILD_REVISION}
WORKDIR /service

COPY services/news_embedding/requirements.txt /service/requirements.txt
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install --index-url "${TORCH_INDEX_URL}" "torch==${TORCH_VERSION}" \
    && python -m pip install -r /service/requirements.txt

COPY services/news_embedding/server.py /service/server.py
COPY tracefold/news/claim_recall_calibration.json /service/calibration.json
EXPOSE 8080
ENTRYPOINT ["python", "/service/server.py"]
CMD ["serve"]
