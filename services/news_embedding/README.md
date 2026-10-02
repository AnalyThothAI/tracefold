# Dedicated News embedding runtime

This service supplies `POST /v1/embeddings` and secretless `GET /readyz`. It has its
own Python 3.12 image and model cache. The application image and its dependency
lock contain no Torch or model weights. Default `make up` does not activate the
`news-embedding` profile.

The image copies `tracefold/news/claim_recall_calibration.json`; there is no second
model configuration. Startup loads that exact immutable revision offline and
verifies pooling, dimensions and inference dtype. It applies the calibrated token
cap, symmetric statement-only encoding with `prompt=""`, L2 normalization and
`trust_remote_code=False`. An incompatible snapshot fails startup. Requests retain
their input order. Each response declares the exact embedder identity, which the
application checks before saving any vectors. The service accepts at most two statements per batch and runs
one inference at a time. Admission waits at most 0.5 seconds for a short existing
batch, then returns 503 with `Retry-After: 1` if still busy.
Connections, body size and socket reads are bounded.

## Deployment

Select the calibrated winner and finish its runtime golden-vector proof before
starting a model. These commands operate only on the dedicated service, using the
same per-project deployment lock as application deployment. They do not migrate
the database or restart application roles.

For the audited CUDA runtime, export the runtime choice in the operator terminal:

```sh
export TRACEFOLD_NEWS_EMBEDDING_RUNTIME=cuda
make embedding-build
make embedding-download
make embedding-up
make embedding-status
```

CUDA uses physical GPU 1, Torch `2.11.0+cu128`, batch 2 and a PyTorch allocator cap
of 1,800,000,000 bytes. The CPU image uses Torch `2.14.1+cpu`; unset the runtime
variable to select CPU. Both pin the other audited dependencies in
`requirements.txt`. The CUDA override never modifies the main application image.
The host must support Docker GPU reservations through NVIDIA Container Toolkit.
The single CUDA profile reserves `all` devices and sets `CUDA_VISIBLE_DEVICES=1`,
so Torch loads the model only on physical GPU 1 (its visible `cuda:0`). This also
works within the [WSL GPU reservation constraints](https://docs.nvidia.com/cuda/wsl-user-guide/index.html#known-limitations-for-linux-cuda-applications).
Before starting a model, verify the container driver with a metadata-only probe:

```sh
docker run --rm --gpus all --entrypoint nvidia-smi tracefold-news-embedding:local
```

Use the image name for the actual Compose project when it differs. A daemon error
about selecting a GPU device driver means the container GPU runtime is not ready;
building or rendering the CUDA profile does not prove GPU execution.
Startup does not download weights. `embedding-download` explicitly downloads only
the selected revision's model/tokenizer/config files into an external bind cache.

Initialize operator files through the normal `make init` flow. Supply a private
Bearer key of at least 16 characters in `news_embedding_api_key`, with mode 0600.
Preserve an existing configured key; the default empty file must be filled before
starting this service. The Workers mount and this service mount refer to the same
file. The service UID/GID defaults to 1000; set
`TRACEFOLD_NEWS_EMBEDDING_UID`/`TRACEFOLD_NEWS_EMBEDDING_GID` to the operator file
owner on other hosts.

The default cache is `<TRACEFOLD_HOME>/embedding-cache`. Use
`TRACEFOLD_NEWS_EMBEDDING_CACHE` for a different private cache. Serving mounts it
read-only; the explicit download command mounts it writable. The default published
port is `127.0.0.1:8767`, while Workers in this project can use
`http://news-embedding:8080/v1`. Configure `llm.news_embedding` with the exact
calibrated model, that base URL, `api_key_file: news_embedding_api_key` and
`max_batch_size: 2`. Verify the application's golden/self-test availability before
declaring dense recall ready. The application compares fixed English, Chinese,
Russian and token-cap-sensitive probes against packaged real model vectors;
pairwise translation similarity alone cannot validate stored-vector compatibility.

`make embedding-status` reports the running immutable image and secretless
provenance: revision, cap, pooling, dtype, dimensions, normalization, calibration
digest, dependency versions and measured GPU peak allocator reservation.
`make embedding-down` stops this service and retains weights. Keep the same
`TRACEFOLD_NEWS_EMBEDDING_RUNTIME` selection for lifecycle commands.

## Checks

```sh
uv run --locked python -m pytest tests/deploy/test_news_embedding_service.py -q
```

The protocol tests use a real bounded HTTP server and injected deterministic
vectors. They cover order/cardinality, auth, malformed/oversized requests,
concurrent 503 handling, invalid vectors, capacity recovery and calibration
identity. Model loading and deployment-plan tests verify immutable revision,
token cap, pooling/dtype, GPU budget and mount/lifecycle isolation. Actual selected
model quality and golden-vector evidence remain separate release evidence.
