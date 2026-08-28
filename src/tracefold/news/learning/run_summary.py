"""One human-readable projection of one golden-path run (#253 §8).

`news learning run` writes three artifacts an operator already had — a readiness report, a standalone
`compile_live` baseline and an optimization run report — and then this module reduces them to the four
questions the whole plane exists to answer:

```text
1. how many accepted, independent examples are in this dataset?
2. what did Stable score on them?
3. did GEPA find an instruction worth testing?
4. what happens next?
```

Three properties make it a projection rather than a fourth authority.

It reads only fields the three reports already publish; it computes no score, re-derives no Objective Plan
and re-reads no corpus. It carries identifiers, counts and scalars — never news text, never a Prompt, never
an endpoint or a credential. And it refuses to imply a comparison it cannot support: the standalone number
and the GEPA seed number are two *physical* runs, so `same_population` is an explicit verdict over named
checks rather than an assumption, and the difference between the two scalars is published as
`numeric_drift` instead of being smoothed away or read as proof that something is broken.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal

from ..artifact_identity import canonical_sha, reject_nonfinite_json, reject_secret_material
from .contracts import dataset_coverage

# v2 (#259): `dataset` carries the frozen corpus's `coverage` block, forwarded from readiness. Bumped for
# the same reason the readiness schema was — a consumer keying off this string has to be able to tell
# whether the block is there, rather than discovering it by indexing into an object that predates it.
RUN_SUMMARY_SCHEMA = "tracefold.news.gepa_run_summary.v2"

CheckStatus = Literal["match", "mismatch", "not_comparable"]

# The `development_selection` scalar this summary calls "the standalone baseline", named by its exact
# address in the baseline report. GEPA's own aggregate is the mean of the metric over the selection half
# with an unanswered case scored `failure_score = 0.0`, and `case_macro_failure_as_zero` is that same
# mean — which is only true because #251 elects one representative per connected fact cluster, so the
# case mean and the cluster mean over this half are the same number.
_STANDALONE_SELECTION_PATH = "subsets.development_selection.case_macro_failure_as_zero"
_GEPA_SEED_SELECTION_PATH = "trajectory.val_aggregate_scores[0]"

# A `REJECTED` whose reasons say "this corpus cannot support an optimization" is answered by more accepted
# examples; one that says "the budget ran out" or "the proposal was refused" is not, and telling an operator
# to go label more news would be wrong. The readiness vocabulary (`train_target_missing`,
# `split_requires_two_clusters`, `dataset_agent_cohort_mismatch`, …) carries no `news_` prefix at all; the
# compile plane's corpus refusals are the five codes below. Everything else keeps Stable and says why.
_CORPUS_REFUSAL_CODES = (
    "news_program_compile_no_verified_failure_clusters",
    "news_program_compile_no_correct_control_clusters",
    "news_program_compile_objective_blocked",
    "news_program_compile_objective_split_unavailable",
    "news_program_compile_split_",
)


def _at(payload: Mapping[str, Any] | None, path: str) -> Any:
    """One dotted read over a report, absent-tolerant.

    Absent-tolerant on purpose: a `REJECTED` the Objective Plan refused before the first call publishes no
    split, no metric and no trajectory, and this summary has to describe that run rather than crash on it.
    """

    cursor: Any = payload
    for part in path.split("."):
        if not isinstance(cursor, Mapping):
            return None
        cursor = cursor.get(part)
    return cursor


def _score(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _seed_selection_score(optimization: Mapping[str, Any]) -> float | None:
    """The seed Program's score *inside this GEPA run*, and nothing else.

    GEPA evaluates the seed candidate on the whole selection half before it proposes anything, and records
    it as trajectory index 0. Substituting the standalone scalar here — the thing #225 made look tempting,
    where a separately run baseline read `0.0` against an in-run seed of `0.475` — would turn the one number
    that says whether GEPA improved on its own starting point into a restatement of a different run.
    """

    scores = _at(optimization, "trajectory.val_aggregate_scores")
    if not isinstance(scores, Sequence) or isinstance(scores, (str, bytes)) or not scores:
        return None
    return _score(scores[0])


def _best_selection_score(optimization: Mapping[str, Any]) -> float | None:
    scores = _at(optimization, "trajectory.val_aggregate_scores")
    best = _at(optimization, "trajectory.best_idx")
    if not isinstance(scores, Sequence) or isinstance(scores, (str, bytes)):
        return None
    if not isinstance(best, int) or isinstance(best, bool) or not 0 <= best < len(scores):
        return None
    return _score(scores[best])


_UNCHECKED = object()


def _check(name: str, standalone: Any, gepa: Any, *, expected: Any = _UNCHECKED, note: str = "") -> dict[str, Any]:
    """One named identity, with every observed value printed whatever the verdict.

    Both values are printed even when they match, because the summary's job is to let a second reader
    re-run the comparison rather than take the verdict on trust.

    `expected` makes a check three-way, and the third party is usually readiness. That is not decoration:
    readiness, the baseline and GEPA each rebuild the Objective Plan from the same sealed export, and #253
    §9 PR-K0 asks that all three be shown to use one representative population rather than two of them
    agreeing while the report an operator read first quietly described a different corpus.
    """

    observed = [standalone, gepa] + ([] if expected is _UNCHECKED else [expected])
    if any(value is None for value in observed):
        status: CheckStatus = "not_comparable"
    else:
        status = "match" if all(value == observed[0] for value in observed) else "mismatch"
    row: dict[str, Any] = {"name": name, "status": status, "standalone": standalone, "gepa": gepa}
    if expected is not _UNCHECKED:
        row["expected"] = expected
    if note:
        row["note"] = note
    return row


def _population_checks(
    *,
    development_dataset_sha: str,
    readiness: Mapping[str, Any],
    baseline: Mapping[str, Any] | None,
    optimization: Mapping[str, Any],
    task_route: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Every identity the two scalars must share before they may be compared at all.

    Dataset, projection, representative set, split, metric, Program and model binding — the list #253 §8
    fixes. A difference in any of them means the two numbers describe different experiments, and no amount
    of arithmetic on them says anything.
    """

    checks = [
        _check(
            "development_dataset_sha",
            _at(baseline, "identity.development_dataset_sha"),
            _at(optimization, "dataset.development_dataset_sha256"),
            expected=development_dataset_sha or None,
            note="expected is the --development argument",
        ),
        _check(
            "episode_projection_root_sha256",
            _at(baseline, "identity.episode_projection_root_sha256"),
            _at(optimization, "objective.episode_projection_root_sha256"),
            expected=_at(readiness, "identity.episode_projection_root_sha256"),
            note="expected is readiness",
        ),
        _check(
            "episode_count",
            _at(baseline, "identity.episode_count"),
            _at(optimization, "dataset.episode_count"),
            expected=_at(readiness, "identity.episode_count"),
            note="expected is readiness",
        ),
        _check(
            "optimizer_case_root_sha256",
            _at(baseline, "objective.optimizer_case_root_sha256"),
            _at(optimization, "objective.optimizer_case_root_sha256"),
            expected=_at(readiness, "objective.optimizer_case_root_sha256"),
            note="expected is readiness; #251 elects one representative per connected fact cluster",
        ),
        _check(
            "optimizer_case_n",
            _at(baseline, "objective.optimizer_case_n"),
            _at(optimization, "objective.optimizer_case_n"),
            expected=_at(readiness, "objective.optimizer_case_n"),
            note="expected is readiness",
        ),
        _check(
            "optimizer_cluster_n",
            _at(baseline, "objective.optimizer_cluster_n"),
            _at(optimization, "objective.optimizer_cluster_n"),
            expected=_at(readiness, "objective.optimizer_cluster_n"),
            note="expected is readiness",
        ),
        _check(
            "split_train_case_root_sha256",
            _at(baseline, "objective.split.train.case_root_sha256"),
            _at(optimization, "split.train.case_root_sha256"),
            expected=_at(readiness, "split.train.case_root_sha256"),
            note="expected is readiness",
        ),
        _check(
            "split_selection_case_root_sha256",
            _at(baseline, "objective.split.development_selection.case_root_sha256"),
            _at(optimization, "split.development_selection.case_root_sha256"),
            expected=_at(readiness, "split.development_selection.case_root_sha256"),
            note="expected is readiness",
        ),
        # The whole ruler, hashed: weights, dimensions, hard gates, the metric's own source, the policy it
        # scores against, the review rubric and the equivalence judge's endpoint, instruction and schema.
        # Comparing a projection of it would let the half nobody projected change unobserved.
        _metric_check(baseline=baseline, optimization=optimization),
        _check(
            "program_sha256",
            _at(baseline, "identity.program_sha256"),
            _at(optimization, "parent_program_sha256"),
            expected=_at(readiness, "identity.program_sha256"),
            note="expected is readiness",
        ),
        # The two model-binding rows are checked against the route this run composed rather than against
        # readiness, because readiness only names the endpoint a compile *would* use; these two reports say
        # what one actually ran on.
        _check(
            "task_model",
            _at(baseline, "identity.runtime_model.compile_task_model"),
            _at(optimization, "model_identities.task.model"),
            expected=task_route.get("model") or None,
            note="expected is the task route this run composed",
        ),
    ]
    checks.append(_task_endpoint_check(baseline=baseline, optimization=optimization, task_route=task_route))
    return checks


def _task_endpoint_check(
    *,
    baseline: Mapping[str, Any] | None,
    optimization: Mapping[str, Any],
    task_route: Mapping[str, Any],
) -> dict[str, Any]:
    """Both reports describe the one task endpoint this run resolved.

    The two reports fingerprint that endpoint under different schemas — the baseline records
    `configured_endpoint_model_v1`, the optimizer records `model_execution_identity.v1` — so the digests
    are not equal even when the endpoint is identical, and comparing them directly would fail closed on
    every honest run. Each side is therefore checked against the digest of the endpoint the run composed,
    which is the claim that actually matters: neither report is describing some other host.
    """

    observed_baseline = _at(baseline, "identity.runtime_model.compile_task_endpoint_sha256")
    observed_gepa = _at(optimization, "model_identities.task.endpoint_fingerprint")
    expected_baseline = task_route.get("baseline_endpoint_sha256")
    expected_gepa = task_route.get("optimizer_endpoint_fingerprint")
    if observed_baseline is None or observed_gepa is None or not expected_baseline or not expected_gepa:
        status: CheckStatus = "not_comparable"
    elif observed_baseline == expected_baseline and observed_gepa == expected_gepa:
        status = "match"
    else:
        status = "mismatch"
    return {
        "name": "task_endpoint_resolved_once",
        "status": status,
        "standalone": observed_baseline,
        "gepa": observed_gepa,
        "expected_standalone": expected_baseline or None,
        "expected_gepa": expected_gepa or None,
        "note": "two digest schemas over one endpoint; each side is checked against the endpoint this run composed",
    }


# The one field excluded from the metric-receipt comparison, and the reason it is safe to exclude: the
# judge's admission ceiling is a *budget*, not a ruler — it cannot change what "better" means. The two legs
# hold it for different reasons (the optimizer declares one for a run that may last hours; the baseline is
# bounded by its corpus instead), so comparing the receipts whole would fail closed on every honest run.
# Both observed ceilings are printed in the row, so the exclusion is stated rather than silent.
_JUDGE_CEILING_PATH = ("semantic_judge", "execution", "max_model_calls")


def _metric_check(*, baseline: Mapping[str, Any] | None, optimization: Mapping[str, Any]) -> dict[str, Any]:
    """The ruler both legs were scored by, compared whole except for the judge's own call ceiling."""

    standalone_receipt = _at(baseline, "identity.metric")
    gepa_receipt = _at(optimization, "metric")
    row = _check(
        "metric_sha256",
        _sha_or_none(_without_judge_ceiling(standalone_receipt)),
        _sha_or_none(_without_judge_ceiling(gepa_receipt)),
        note=(
            "tracefold.news.compile_metric_receipt.v4 excluding semantic_judge.execution.max_model_calls, "
            "which bounds spend and cannot change what better means"
        ),
    )
    row["standalone_judge_max_model_calls"] = _judge_ceiling(standalone_receipt)
    row["gepa_judge_max_model_calls"] = _judge_ceiling(gepa_receipt)
    return row


def _judge_ceiling(receipt: Any) -> Any:
    cursor: Any = receipt
    for part in _JUDGE_CEILING_PATH:
        if not isinstance(cursor, Mapping):
            return None
        cursor = cursor.get(part)
    return cursor


def _without_judge_ceiling(receipt: Any) -> Any:
    """The receipt with the excluded field normalized away, and everything else byte-identical."""

    if not isinstance(receipt, Mapping):
        return receipt
    judge = receipt.get("semantic_judge")
    if not isinstance(judge, Mapping):
        return receipt
    execution = judge.get("execution")
    if not isinstance(execution, Mapping):
        return receipt
    return {
        **receipt,
        "semantic_judge": {**judge, "execution": {**execution, "max_model_calls": None}},
    }


def _sha_or_none(value: Any) -> str | None:
    return canonical_sha(value) if isinstance(value, Mapping) else None


def _same_population(checks: Sequence[Mapping[str, Any]]) -> bool | None:
    """`False` on any mismatch, `None` when a leg never ran, `True` only when every check compared equal."""

    statuses = {str(check["status"]) for check in checks}
    if "mismatch" in statuses:
        return False
    if "not_comparable" in statuses:
        return None
    return True


def _judge_availability(*, baseline: Mapping[str, Any] | None, optimization: Mapping[str, Any]) -> dict[str, Any]:
    """Whether the equivalence judge answered every time it was asked, on each leg separately.

    Not a population identity, and deliberately not folded into `same_population`: the two legs can read
    one identical corpus with one identical ruler and still produce two numbers that are not comparable,
    because a judge that went unavailable scores its free-text dimension as failure-as-zero. That makes the
    affected leg a lower bound rather than a measurement, and a reader comparing the two has to know which
    one it happened to.
    """

    standalone = _at(baseline, "semantic_judge.failures")
    gepa = _at(optimization, "usage.metric_judge_failures")
    degraded = bool(standalone or 0) or bool(gepa or 0)
    return {
        "standalone_judge_failures": standalone,
        "standalone_judge_model_calls": _at(baseline, "semantic_judge.model_calls"),
        "gepa_judge_failures": gepa,
        "gepa_judge_model_calls": _at(optimization, "usage.metric_judge_model_calls"),
        "degraded": degraded,
        "note": (
            "an unavailable judge scores its free-text dimension as failure-as-zero and can arm the "
            "factual_contradiction hard gate; the affected leg is a lower bound, not a measurement"
        ),
    }


def _next_action(outcome: str, reasons: Sequence[str], *, same_population: bool | None) -> str:
    # A refused comparison cannot recommend the step that depends on it. `ADVANCE` still means the
    # optimizer produced a patch, and the report still says so — but "go test this on future examples"
    # rests on a `before` number this run just declined to vouch for.
    if same_population is False:
        return "keep_stable"
    if outcome == "ADVANCE":
        return "future_test"
    if outcome == "NO_OP":
        return "keep_stable"
    corpus_shaped = any(
        not str(reason).startswith("news_") or str(reason).startswith(_CORPUS_REFUSAL_CODES) for reason in reasons
    )
    return "collect_more_gold" if corpus_shaped else "keep_stable"


def build_run_summary(
    *,
    development_dataset_sha: str,
    readiness: Mapping[str, Any],
    baseline: Mapping[str, Any] | None,
    optimization: Mapping[str, Any],
    task_route: Mapping[str, Any],
    artifacts: Mapping[str, str | None],
) -> dict[str, Any]:
    """Reduce one run's three reports to the #253 §8 summary. Reads them; never re-derives them."""

    outcome = str(_at(optimization, "outcome") or "")
    reasons = [str(reason) for reason in (_at(optimization, "reasons") or ())]
    standalone = _score(_at(baseline, _STANDALONE_SELECTION_PATH))
    seed = _seed_selection_score(optimization)
    checks = _population_checks(
        development_dataset_sha=development_dataset_sha,
        readiness=readiness,
        baseline=baseline,
        optimization=optimization,
        task_route=task_route,
    )
    same_population = _same_population(checks)
    summary = {
        "schema": RUN_SUMMARY_SCHEMA,
        # A projection of three retained artifacts, addressed by their own hashes. Re-deriving anything here
        # would make this a fourth place the truth lives.
        "run": {
            "development_dataset_sha": development_dataset_sha,
            "readiness_outcome": str(_at(readiness, "outcome") or ""),
            "readiness_blocking_reasons": [str(reason) for reason in (_at(readiness, "blocking_reasons") or ())],
            "baseline_executed": baseline is not None,
            "artifacts": dict(artifacts),
        },
        "dataset": {
            "development_sha": development_dataset_sha,
            "episode_root": _at(readiness, "identity.episode_projection_root_sha256"),
            "episode_count": _at(readiness, "identity.episode_count"),
            "accepted_case_n": _at(readiness, "corpus.case_n"),
            "connected_cluster_n": _at(readiness, "corpus.cluster_n"),
            # The count #253 §4 asks to be reported apart from the proposal pool and the accepted case
            # count: after #251 a connected fact cluster contributes exactly one optimizer example.
            "optimizer_representative_n": _at(readiness, "objective.optimizer_case_n"),
            "optimizer_cluster_n": _at(readiness, "objective.optimizer_cluster_n"),
            "optimizer_case_root": _at(readiness, "objective.optimizer_case_root_sha256"),
            "target_case_n": _at(readiness, "objective.target_case_n"),
            "control_case_n": _at(readiness, "objective.control_case_n"),
            "excluded_case_n": _at(readiness, "objective.excluded_case_n"),
            "train_root": _at(readiness, "split.train.case_root_sha256"),
            "selection_root": _at(readiness, "split.development_selection.case_root_sha256"),
            "train_cluster_n": _at(readiness, "split.train.cluster_n"),
            "selection_cluster_n": _at(readiness, "split.development_selection.cluster_n"),
            # The frozen corpus's own coverage, forwarded from readiness (#259 §5.2). The release profile
            # decides `offline` on the cluster-role and stratum counts here; `natural_day_n` and
            # `window_duration_hours` are in the block so an operator can see how concentrated the samples
            # are, and are read by no gate — a corpus that lands inside one UTC date is not thereby worse.
            "coverage": _coverage(readiness),
        },
        # Three baselines with three different jobs, named so they cannot be quoted as one another (#253 §3.2).
        "baseline": {
            "standalone_report_sha": _at(baseline, "report_sha256"),
            "standalone_selection_score": standalone,
            "standalone_selection_source": _STANDALONE_SELECTION_PATH,
            "standalone_selection_case_n": _at(baseline, "subsets.development_selection.case_n"),
            "standalone_selection_answered_n": _at(baseline, "subsets.development_selection.answered_n"),
            "gepa_seed_selection_score": seed,
            "gepa_seed_selection_source": _GEPA_SEED_SELECTION_PATH,
            # Not produced here and never inferable from a development number. It is Stable's score on
            # accepted examples that did not exist when the Candidate was made, and only an `ADVANCE`
            # earns the right to go collect them.
            "future_test_baseline": None,
            "future_test_note": (
                "produced by `news release evaluate --stage holdout` against a ValidationDataset frozen "
                "strictly after candidate registration; never by this command"
            ),
            "same_population": same_population,
            # Two physical runs of the same graph against the same corpus can still disagree in the last
            # digits, and #225 is the reason this is published rather than reconciled: a difference is a
            # fact about model execution, not by itself evidence that the dataset identity is wrong.
            "numeric_drift": None if standalone is None or seed is None else round(seed - standalone, 6),
            "numeric_drift_definition": "gepa_seed_selection_score - standalone_selection_score",
            "judge_availability": _judge_availability(baseline=baseline, optimization=optimization),
            "population_checks": checks,
        },
        "optimization": {
            "terminal": outcome,
            "candidate_sha": _at(optimization, "candidate_sha256"),
            "best_selection_score": _best_selection_score(optimization),
            "best_trajectory_index": _at(optimization, "trajectory.best_idx"),
            "trajectory_entries": len(_at(optimization, "trajectory.val_aggregate_scores") or ()),
            "metric_calls": _at(optimization, "usage.metric_calls"),
            "cost": _usage_cost(optimization),
            "reasons": reasons,
            "report_sha256": _at(optimization, "report_sha256"),
        },
        "next_action": _next_action(outcome, reasons, same_population=same_population),
    }
    reject_nonfinite_json(summary, path="gepa_run_summary")
    reject_secret_material(summary, path="gepa_run_summary")
    return summary


def _coverage(readiness: Mapping[str, Any]) -> dict[str, Any]:
    """Readiness's `coverage` block, in the one shape every producer of it publishes.

    Forwarded rather than restated field by field, and projected through the same `dataset_coverage` the
    readiness command uses, so a `gepa_readiness_report.v1` in an archived run directory yields the block
    with `null` values instead of an object a consumer falls off the end of. `null` rather than `0`
    throughout: those counts were never measured, and zeros would read as a measured corpus of nothing.
    """

    coverage = _at(readiness, "coverage")
    return dataset_coverage(coverage if isinstance(coverage, Mapping) else {})


def _usage_cost(optimization: Mapping[str, Any]) -> dict[str, Any]:
    """What the run spent, from the report's own usage block. No prices, no re-tallying."""

    usage = _at(optimization, "usage")
    if not isinstance(usage, Mapping):
        return {}
    return {
        key: usage.get(key)
        for key in (
            "task_model_calls",
            "reflection_model_calls",
            "metric_judge_model_calls",
            "metric_judge_failures",
            "actual_cost_microusd",
            "metric_judge_cost_imputed",
            "transport_failures",
            "transport_retries",
        )
    }


__all__ = ["RUN_SUMMARY_SCHEMA", "build_run_summary"]
