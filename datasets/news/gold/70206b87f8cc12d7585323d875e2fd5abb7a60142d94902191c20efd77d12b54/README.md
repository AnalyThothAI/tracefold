# News taxonomy Gold corpus

This directory publishes the frozen development corpus used by issues
[#456](https://github.com/AnalyThothAI/tracefold/issues/456) and
[#492](https://github.com/AnalyThothAI/tracefold/issues/492).

## Contents

- `manifest.json` is the canonical payload of frozen Dataset
  `70206b87f8cc12d7585323d875e2fd5abb7a60142d94902191c20efd77d12b54`.
- `clusters.jsonl` contains one canonical JSON row per
  `news_fact_cluster_v1` contract cluster. Each row has `cluster_id` and
  `cases`; every case is the exact development episode projection consumed by
  the formal GEPA run.

The corpus contains 432 pinned cases in 420 clusters. Twelve clusters contain
two cases; all others contain one.

## Identity

- Dataset artifact SHA-256: `70206b87f8cc12d7585323d875e2fd5abb7a60142d94902191c20efd77d12b54`
- Episode projection root SHA-256: `0468b6bf7bbc2246e7a197333ec5e1cf2fc7b922d69fb5fddb598856cf0c0212`
- `manifest.json` byte SHA-256: `88065af1edaeb3da2439a5359beff217288c33826ea643a7425067878856c466`
- `clusters.jsonl` byte SHA-256: `799fb51535e18c535d5ee94a8af9c26a1a3648ce03e39f25c2e6ce0e53b7a73f`
- Projection code revision: `c4fbd342c23ce5dfb0691cf8705205f51ae553bd`

The historical revision is intentional. Later retrieval changes alter the
rendered episode context; replaying with current code would not reproduce the
formal run's projection root.

## Boundary

This is an inspectable publication snapshot, not another business truth.
PostgreSQL retains the append-only evidence and review ledger. The dataset is
development Gold, not a future holdout, and its presence in Git grants no
runtime, candidate, release, canary, deployment, or capital authority.

Cluster identity follows the frozen `news_fact_cluster_v1` contract: exact
source/text identity plus reviewer-recorded `duplicate_of` connections.
Cross-source semantic paraphrases may remain separate when no reviewer linked
them.

Verify the publication with:

```console
uv run pytest tests/news/test_published_news_gold_dataset.py
```
