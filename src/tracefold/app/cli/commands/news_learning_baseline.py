from __future__ import annotations

import time
from argparse import Namespace
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from tracefold.news.artifact_identity import canonical_json

from .news_learning_documents import _write_json

if TYPE_CHECKING:
    # Annotation-only. A module-level import would pull the whole learning module tree
    # (transport, judge, optimizer) into every `tracefold news learning ...` invocation,
    # including the ones that never call a model.
    from tracefold.news.learning.baseline import BaselineMode

_BASELINE_MODES: tuple[BaselineMode, ...] = ("recorded", "compile_live", "runtime_live")


def _baseline_mode(raw: object) -> BaselineMode:
    """#150 split one ambiguous `live` into three modes. An unknown one is refused, never defaulted."""

    mode = str(raw)
    for known in _BASELINE_MODES:
        if mode == known:
            return known
    raise ValueError("news_program_baseline_mode_unknown")


def _baseline_model_route(mode: BaselineMode, *, settings: Any, artifact: Any) -> tuple[Any | None, dict[str, Any]]:
    """The Program a live mode runs on, plus the identity the report records.

    `recorded` spends no provider call and gets neither. The two live modes answer different questions
    and therefore build different things — see the comments in each branch.
    """

    from tracefold.app.learning_runtime import _endpoint_model_sha256, compose_news_program_runtime
    from tracefold.news.learning.baseline import build_compile_adapter, build_compile_program
    from tracefold.news.program.runtime import (
        PROGRAM_EVENT_SEMANTICS_MAX_TOKENS,
        PROGRAM_READER_CARD_MAX_TOKENS,
        PROGRAM_ROUTE_DEADLINE_SECONDS,
    )

    semantic_judge: Any = None
    runtime_identity: dict[str, Any] = {}
    if mode == "compile_live":
        # One task endpoint driving the production graph, which since #306 Phase 3 is literally what the
        # optimizer evaluates. Deliberately *not* the production *route*: no fallback slot is bound, so this
        # measures what the optimizer maximizes and the report says so in `execution_scope`.
        #
        # Fail closed the way `runtime_live` does. `compose_news_program_runtime` falls back to the literal
        # model name "unconfigured" with an empty key and base, `configured_lm_endpoint` never raises, and the
        # resulting object is not None — so on an unconfigured host every case ran, swallowed the same
        # connection error, and the run ended reporting a total Program failure instead of a missing config.
        composition = compose_news_program_runtime(settings)
        if not composition.program_configured:
            raise ValueError("news_program_baseline_compile_route_not_configured")
        endpoint = composition.event_semantics_primary
        semantic_judge = build_compile_program(
            artifact,
            build_compile_adapter(
                model_name=endpoint.model_name,
                api_key=endpoint.api_key,
                api_base=endpoint.api_base,
                timeout=float(PROGRAM_ROUTE_DEADLINE_SECONDS),
                max_tokens=max(PROGRAM_EVENT_SEMANTICS_MAX_TOKENS, PROGRAM_READER_CARD_MAX_TOKENS),
                model_kwargs=endpoint.model_kwargs,
                temperature=endpoint.temperature,
                structured_output=endpoint.structured_output,
            ),
        )
        # The model name alone cannot tell two endpoints apart: the local box and a hosted gateway can serve
        # the same name and produce different baselines. `runtime_live` records a per-slot fingerprint; this
        # mode records the same kind of thing for its one slot.
        runtime_identity = {
            "compile_task_model": endpoint.model_name,
            "compile_task_endpoint_sha256": _endpoint_model_sha256(endpoint),
        }
    elif mode == "runtime_live":
        # The configured four-slot Program with its own retry, fallback, deadline and circuit — built by the
        # same seam the Workers use, so a dedicated ReaderCard binding is honoured rather than silently
        # aliased to the EventSemantics primary.
        composition = compose_news_program_runtime(settings)
        semantic_judge = composition.semantic_judge(artifact)
        if semantic_judge is None:
            raise ValueError("news_program_baseline_runtime_route_not_configured")
        runtime_identity = {
            "slots": composition.secret_free_slot_identities(),
            "aliases": composition.slot_aliases(),
            "runtime_model_bindings_sha256": composition.runtime_model_bindings_sha256,
        }
    return semantic_judge, runtime_identity


def _readiness_model_targets(settings: Any) -> dict[str, Any]:
    """The endpoints a compile would run against, named and never quoted.

    Secret-free by construction: a model name and the same endpoint fingerprint `compile_live` records.
    `compiler_reflection_configured` is here because it is the one identity that silently turns a planned
    compile into `news_experiment_reflection_not_configured` after the corpus work is already done.
    """

    from tracefold.app.learning_runtime import _endpoint_model_sha256, compose_news_program_runtime

    composition = compose_news_program_runtime(settings)
    task: dict[str, Any] | None = None
    if composition.program_configured:
        endpoint = composition.event_semantics_primary
        task = {"model": endpoint.model_name, "endpoint_sha256": _endpoint_model_sha256(endpoint)}
    reflection = getattr(settings.llm, "news_compiler_reflection", None)
    return {
        "task": task,
        "program_route_configured": bool(composition.program_configured),
        "compiler_reflection_configured": bool(reflection is not None and reflection.configured),
    }


def _handle_learning_readiness(args: Namespace, settings: Any, stable: Any) -> tuple[int, dict[str, Any]]:
    """Explain one frozen development dataset before anyone spends a provider call on it.

    Read-only and zero-call: it re-projects the sealed corpus, builds the one Objective Plan the trusted
    compiler will rebuild, and reports what would be target, control and excluded — with the reason for
    every exclusion. `outcome` is the answer; the exit code stays 0 for a report that says `insufficient`,
    because refusing to optimize a corpus that cannot support it is a correct result, not a failure.
    """

    from tracefold.app.repository_session import postgres_connection
    from tracefold.news.artifact_identity import canonical_sha
    from tracefold.news.learning.contracts import LEARNING_PROFILE_ID, dataset_coverage, epoch_id_for_bundle
    from tracefold.news.learning.dataset import DevelopmentDatasetStore
    from tracefold.news.learning.objective import (
        DevelopmentEpisode,
        GepaObjectivePlan,
        build_gepa_objective_plan,
        build_readiness_report,
    )
    from tracefold.news.program.identity import EXECUTION_ENVELOPE_SHA256
    from tracefold.news.review.desk import REVIEW_RUBRIC_VERSION

    dataset_sha = str(args.development).strip()
    identity: dict[str, Any] = {
        "development_dataset_sha": dataset_sha,
        "learning_epoch": epoch_id_for_bundle(stable.bundle_sha),
        "profile_id": LEARNING_PROFILE_ID,
        "review_rubric_version": REVIEW_RUBRIC_VERSION,
        "execution_envelope_sha256": EXECUTION_ENVELOPE_SHA256,
        "program_version": stable.program_version,
        "program_sha256": stable.program_sha256,
        "stable_bundle_sha": stable.bundle_sha,
        "policy_sha256": stable.policy_sha256,
        "model_targets": _readiness_model_targets(settings),
        # Present on every path, including the one that never reaches a projection: a consumer keying on
        # `identity.episode_count` must read 0, not fall off the end of the object.
        "episode_count": 0,
        "episode_projection_root_sha256": None,
    }
    episodes: tuple[Any, ...] = ()
    plan = GepaObjectivePlan(blocking_reasons=("dataset_agent_cohort_mismatch",))
    # Present on every path for the same reason `identity.episode_count` is: a consumer must read `null`,
    # not fall off the end of the object. It stays `null` on the `dataset_agent_cohort_mismatch` path even
    # though the export loaded that payload before refusing, and that is deliberate: those counts —
    # `eligible_event_n` above all — were measured against a different arm's cohort, and this report's
    # `identity` names the current stable bundle. Publishing them here would file another arm's corpus
    # under this arm's name, which is a worse answer than "unknown".
    coverage: dict[str, Any] = dataset_coverage({})
    with postgres_connection(settings, role="serve") as conn:
        datasets = DevelopmentDatasetStore(conn, stable=stable)
        try:
            export = datasets.development_compile_export(dataset_sha)
        except ValueError as exc:
            # The one blocker in the #199 §4 vocabulary that has no episodes behind it: a dataset frozen
            # under a different arm cannot be projected at all. It is a readiness answer, so it is reported
            # as one — through the same builder, so a consumer never has to parse two report shapes. Every
            # other refusal (a validation-role SHA, an epoch mismatch, drifted evidence) is an error, not
            # an insufficiency, and still raises.
            if "news_learning_dataset_agent_cohort_mismatch" not in str(exc):
                raise
        else:
            episodes = tuple(DevelopmentEpisode.model_validate(episode) for episode in export.episodes)
            identity["episode_count"] = len(episodes)
            # The dataset's own sealed counts, republished rather than re-tallied from the episodes: the
            # eligible-Event and cluster-role numbers were computed against production at freeze time and
            # cannot be recovered from a projection of the cases that survived it.
            coverage = dataset_coverage(dict(export.dataset_payload.get("counts") or {}))
            # The exact root a candidate's `ProposalReceipt` records and the release gate re-derives, computed from
            # the same raw projection dicts rather than from the parsed models — same bytes, same address.
            identity["episode_projection_root_sha256"] = canonical_sha(list(export.episodes))
            plan = build_gepa_objective_plan(episodes)
    report = build_readiness_report(plan, episodes=episodes, identity=identity, coverage=coverage)
    if str(args.out):
        _write_json(str(args.out), report)
    summary: dict[str, Any] = {key: value for key, value in report.items() if key != "case_dispositions"}
    summary["case_dispositions_written_to"] = str(args.out) or None
    return 0, {"ok": True, "data": summary}


def _dataset_corpus(datasets: Any, dataset_sha: str) -> tuple[tuple[Any, ...], tuple[Any, ...], Any, dict[str, Any]]:
    """The frozen development corpus, its Objective Plan, and the identity the report has to publish.

    One projection, read once. `_project_episodes` is a per-case `_load_case` plus a reader-history
    rebuild, and — the part that matters — a review edited between two reads would leave the published
    projection root describing a corpus other than the one that was scored.

    Only `target + control` is scored. Excluded diagnostics are counted and named in `objective` and never
    enter a denominator: a formal optimizer baseline has to measure what the optimizer measures, and a
    retrieval miss averaged into the "before" number is exactly the kind of movement a candidate can be
    credited for without repairing anything. The whole export comes back beside the scored subset, because
    the retrieval receipt has to be computed over the corpus that still *contains* the retrieval misses.
    """

    from tracefold.news.artifact_identity import canonical_sha
    from tracefold.news.learning.objective import DevelopmentEpisode, build_gepa_objective_plan

    export = datasets.development_compile_export(dataset_sha)
    episodes = tuple(DevelopmentEpisode.model_validate(episode) for episode in export.episodes)
    plan = build_gepa_objective_plan(episodes)
    optimizer = set(plan.optimizer_case_ids)
    scored = tuple(episode for episode in export.episodes if str(episode["case_id"]) in optimizer)
    identity = {
        "development_dataset_sha": dataset_sha,
        # The exact root a candidate's `ProposalReceipt` records and the release gate re-derives, over the sealed
        # export rather than over the scored subset: readiness, this baseline, the record and the evaluator
        # have to agree about the corpus before they can agree about the split.
        "episode_projection_root_sha256": canonical_sha(list(export.episodes)),
        "episode_count": len(export.episodes),
        "scored_population": "objective_plan_target_and_control",
    }
    return scored, episodes, plan, identity


def _handle_learning_baseline(args: Namespace, settings: Any, stable: Any) -> tuple[int, dict[str, Any]]:
    """Score the stable Program offline. Read-only: no sandbox, no tariff, no container, no writes.

    The model transport lives behind `program_baseline`; this layer only reads the corpus and prints the
    receipt, so the architecture boundary that keeps provider plumbing out of the CLI still holds.
    """

    from tracefold.app.llm import configured_lm_endpoint
    from tracefold.app.repository_session import postgres_connection
    from tracefold.news.learning.baseline import (
        build_baseline_cases,
        build_judge,
        run_baseline,
    )
    from tracefold.news.learning.contracts import ClosedWindow
    from tracefold.news.learning.dataset import DevelopmentDatasetStore
    from tracefold.news.program.artifact import load_program_artifact

    mode = _baseline_mode(args.mode)
    action_source = str(args.action_source) or ("recorded" if mode == "recorded" else "policy")
    if mode == "recorded" and action_source != "recorded":
        # Scoring a retired arm's verdict against today's `decide()` compares two things that never coexisted.
        raise ValueError("news_program_baseline_recorded_mode_requires_recorded_decision")
    if mode != "recorded" and action_source == "recorded":
        # A live mode must score the fresh judgment through the frozen policy. Reusing a recorded decision
        # would compare outputs that never coexisted and invalidate the 45% final-action component.
        raise ValueError("news_program_baseline_live_mode_requires_policy_action")
    # Every other model-spending command in this plane makes its budget `required=True`; before #150 this one
    # defaulted to 500 cases, which in `runtime_live` is 1,000-3,000 sequential provider calls against the
    # single-slot box that is simultaneously serving live Triage. A sentence in OPERATIONS.md is not a bound.
    max_model_cases = int(getattr(args, "max_model_cases", 0) or 0)
    if mode != "recorded" and max_model_cases <= 0:
        raise ValueError("news_program_baseline_live_mode_requires_max_model_cases")
    dataset_sha = str(getattr(args, "dataset", "") or "").strip()
    moving_window = [name for name in ("from_ms", "to_ms") if getattr(args, name, None) is not None]
    if dataset_sha and (moving_window or bool(args.all_cohorts)):
        # A run measures one corpus. Silently preferring one input would publish a report whose window and
        # whose cases came from different questions.
        raise ValueError("news_program_baseline_dataset_excludes_moving_window")
    if not dataset_sha and len(moving_window) != 2:
        raise ValueError("news_program_baseline_requires_dataset_or_window")

    if dataset_sha and mode != "compile_live":
        # `--dataset` publishes `subsets.development_selection` as the formal *before* value a Candidate is
        # picked against, so it has to measure what the optimizer measures: the production graph on one
        # task endpoint. The other two modes measure something else, each in its own way.
        #
        # `recorded` scores the action that actually shipped, while the Objective Plan classifies under a
        # replayed `decide()` — it has to, because readiness, the trusted compiler and the release gate all
        # rebuild the plan from the sealed export, which carries no recorded decision. They disagree on any
        # case whose ledger state differed at ingest, and the report would then call a case a control and
        # zero it in the same document.
        #
        # `runtime_live` runs the four-slot production route with its retry, fallback, deadline and
        # circuit. That is a reliability question, and a number from it is not comparable to a candidate
        # selected on the cold graph however honestly it is labelled.
        #
        # Both stay available in the moving-window form, which names itself discovery.
        raise ValueError("news_program_baseline_dataset_requires_compile_live")
    if dataset_sha and not str(args.semantic_judge).strip():
        # `run_gepa` refuses to run without a metric judge, so an optimizer baseline without one is scored
        # by a different ruler than the optimizer it is the baseline for: `bind_metric(None)` compares
        # free-text retention byte-for-byte and fires `factual_contradiction` on every failed
        # `factual_fidelity`. The report records the judge identity, so two runs judged differently are
        # already visibly incomparable; this makes the un-judged one impossible rather than merely visible.
        raise ValueError("news_program_baseline_dataset_requires_semantic_judge")

    plan = None
    dataset_identity: dict[str, Any] = {}
    retrieval_population: tuple[Any, ...] | None = None
    with postgres_connection(settings, role="serve") as conn:
        datasets = DevelopmentDatasetStore(conn, stable=stable)
        if dataset_sha:
            episodes, retrieval_population, plan, dataset_identity = _dataset_corpus(datasets, dataset_sha)
        else:
            window = ClosedWindow(from_ms=int(args.from_ms), to_ms=int(args.to_ms))
            limit = int(args.limit) if mode == "recorded" else min(int(args.limit), max_model_cases)
            episodes = datasets.baseline_episodes(window, cohort=not bool(args.all_cohorts), limit=limit)
    if not episodes:
        code = (
            "news_program_baseline_dataset_has_no_optimizer_corpus"
            if dataset_sha
            else "news_program_baseline_no_accepted_reviews_in_window"
        )
        return 2, {
            "ok": False,
            "error": {"code": code, "blocking_reasons": list(plan.blocking_reasons) if plan else []},
        }
    if plan is not None and plan.blocking_reasons:
        # `subsets.development_selection` is the one number this report exists to publish, and a blocked
        # plan has no split to compute it from. A `frozen_development` report with an empty subsets block
        # would read as a measured zero. `news learning readiness` explains why, for free.
        raise ValueError(f"news_program_baseline_dataset_objective_blocked:{','.join(plan.blocking_reasons)}")
    if dataset_sha and max_model_cases < len(episodes):
        # A formal optimizer baseline covers the whole optimizer corpus or it is not one: a truncated run
        # would publish split roots that describe cases it never scored. The moving-window form stays
        # available for a cheap probe, and says `discovery` in its own receipt.
        raise ValueError(f"news_program_baseline_dataset_requires_full_corpus_budget:{len(episodes)}")
    artifact = load_program_artifact(stable.program_sha256)
    semantic_judge, runtime_identity = _baseline_model_route(mode, settings=settings, artifact=artifact)
    judge_model = str(args.semantic_judge).strip()
    judge = None
    if judge_model:
        # The judge belongs to the metric, not the Program, so it gets its own endpoint rather than the task
        # route: the compiler reflection endpoint when configured, otherwise the Triage fallback.
        reflection = getattr(settings.llm, "news_compiler_reflection", None)
        source = reflection if reflection is not None and reflection.configured else settings.llm.news_triage_fallback
        if not source.configured:
            raise ValueError("news_program_baseline_judge_endpoint_not_configured")
        if dataset_sha and source is not reflection:
            # The trusted compile judges on the compiler reflection route. A dataset-bound baseline that
            # fell through to the Triage fallback would be judged on a route the compile never uses, and
            # then published as the before value for it.
            raise ValueError("news_program_baseline_dataset_requires_compiler_reflection_judge")
        endpoint = configured_lm_endpoint(
            settings,
            model_name=judge_model,
            api_key=source.api_key,
            base_url=source.base_url,
            request_config=source.request,
        )
        # No admission ceiling, deliberately, and #253 tried the other way first. A judge that hits its
        # ceiling does not raise: it returns `unavailable`, `retains()` reads that as "not retained", a
        # failed `factual_fidelity` arms the `factual_contradiction` hard gate and the case scores zero.
        # An under-sized ceiling therefore publishes a *depressed baseline* that looks like a measurement.
        # The real bound here is `--max-model-cases`, which pins the corpus and so pins the judge's work.
        judge = build_judge(
            model_name=endpoint.model_name,
            api_key=endpoint.api_key,
            api_base=endpoint.api_base,
            model_kwargs=endpoint.model_kwargs,
            temperature=0 if endpoint.temperature is None else endpoint.temperature,
            structured_output=endpoint.structured_output,
        )
    report = run_baseline(
        build_baseline_cases(episodes, action_source=action_source),
        cohort_scope=("frozen_development" if dataset_sha else "all" if bool(args.all_cohorts) else "current"),
        objective=plan,
        dataset_identity=dataset_identity,
        retrieval_population=retrieval_population,
        mode=mode,
        artifact=artifact,
        judge=judge,
        semantic_judge=semantic_judge,
        runtime_identity=runtime_identity,
    )
    payload = report.model_dump(mode="json")
    payload["report_sha256"] = report.report_sha256
    if str(args.out):
        _write_json(str(args.out), payload)
    summary = {key: value for key, value in payload.items() if key != "cases"}
    summary["cases_written_to"] = str(args.out) or None
    return 0, {"ok": True, "data": summary}


def _drafter_context(view: Mapping[str, Any]) -> Any:
    """Rebuild the bounded TriageContext from a ReviewDesk evidence view.

    Mirrors `DevelopmentDatasetStore.build_context`: the focus fact is what the Program treats as the headline,
    and the card carries the Gate facts.
    """

    from tracefold.news.program.contracts import TriageContext

    evidence = dict(view.get("evidence") or {})
    card = dict(evidence.get("card") or {})
    focus = dict(evidence.get("focus_fact") or {})
    card["focus_fact_id"] = focus.get("fact_id")
    card["leader_title"] = focus.get("text") or card.get("leader_title")
    card["leader_description"] = focus.get("context") or card.get("leader_description")
    return TriageContext.from_card(
        card,
        watchlist=(),
        told_rows=[],
        now_ms=int(card.get("opened_at_ms") or 0),
        queue_lag_ms=0,
    )


_DESK_MAX_LOOKBACK_HOURS = 720


def _run_window(root: str) -> tuple[int, int] | None:
    """The absolute window an experiment run froze, or `None` when no run was named.

    This used to read the run's *case list* and draft the ones marked unaccepted. There are none, and
    there never can be: `baseline_episodes` reaches a case through an acceptance row, so a snapshot holds
    reviewed Events by construction. Drafting is for the rest of that same window — the Events the
    comparison could not score because nobody has judged them.
    """

    if not root.strip():
        return None
    from pathlib import Path

    from tracefold.news.learning.experiment.run import ExperimentRun

    window = ExperimentRun(Path(root)).manifest().window
    return int(window.from_ms), int(window.to_ms)


def _desk_lookback_hours(window: tuple[int, int], *, now_ms: int) -> int:
    """The look-back that reaches a run's window. The desk takes a width; a run has two edges."""

    from_ms, _to_ms = window
    if now_ms <= from_ms:
        raise ValueError("news_review_drafter_run_window_not_in_the_past")
    # Ceiling, so the look-back covers the window's leading edge rather than stopping just inside it.
    hours = -(-(now_ms - from_ms) // 3_600_000)
    if hours > _DESK_MAX_LOOKBACK_HOURS:
        raise ValueError("news_review_drafter_run_window_exceeds_desk_lookback")
    return int(hours)


def _within_window(row: Mapping[str, Any], window: tuple[int, int] | None) -> bool:
    """Whether one desk task belongs to the run's frozen window.

    The desk's look-back is a width ending at *now*, so it necessarily reaches past `to_ms` — a snapshot
    stops at the settlement grace on purpose — and rounds up to an hour before `from_ms`. Without this the
    drafts would grow a corpus for a window the run never froze while claiming to target it.
    """

    if window is None:
        return True
    from_ms, to_ms = window
    return from_ms <= int(row.get("opened_at_ms") or 0) < to_ms


def _handle_learning_draft_reviews(args: Namespace, settings: Any, stable: Any) -> tuple[int, dict[str, Any]]:
    """Propose `news_review_v4` rubrics with exact gold. The output is a file, never a review.

    `ReviewDesk.submit` appends an acceptance row unconditionally, so a draft written through that path would
    be accepted release evidence the instant it landed. The human stays the acceptance authority; this only
    turns "compose a judgment from scratch" into "confirm or reject one".

    Tasks come from the ReviewDesk queue rather than from `baseline_episodes`, which starts at
    `news_reviews` and therefore only ever returns Events that already have one — 170 against 6,186
    unreviewed. Drafting is for the ones nobody has judged yet, and the queue is also what gives the drafter
    the same task identity and the same evidence view a human reviewer opens.
    """

    del stable
    from tracefold.app.llm import configured_lm_endpoint
    from tracefold.app.repository_session import postgres_connection
    from tracefold.news.program.artifact import render_model_evidence_json
    from tracefold.news.review.desk import DeskQuery, Principal, ReviewDesk, TaskRef
    from tracefold.news.review.drafter import ReviewDrafter, build_draft_batch, build_drafter_lm

    reflection = getattr(settings.llm, "news_compiler_reflection", None)
    source = reflection if reflection is not None and reflection.configured else settings.llm.news_triage_fallback
    if not source.configured:
        raise ValueError("news_review_drafter_endpoint_not_configured")

    principal = Principal(subject="operator")
    hours = int(args.hours)
    tasks: list[dict[str, Any]] = []
    wanted = int(args.limit)
    # `--events-from` narrows the queue to the run's own window; everything else about it — the stratified
    # sampling, the cohort filter, the pending/all switch — is unchanged, because a corpus grown through
    # the fast loop has to carry the same stratum mix as one grown through the queue.
    window = _run_window(str(getattr(args, "events_from", "") or ""))
    if window is not None:
        hours = _desk_lookback_hours(window, now_ms=int(time.time() * 1000))
    with postgres_connection(settings, role="serve") as conn:
        desk = ReviewDesk(conn)
        # The queue pages at 100 and applies its own deterministic stratified sampling. Paginating through
        # it keeps that stratification — which is what makes a corpus carry the boundary/negative/retention
        # mix the release thresholds require — instead of bypassing the desk with a raw query.
        rows: list[Mapping[str, Any]] = []
        cursor = ""
        while len(rows) < wanted:
            queue = desk.open(
                DeskQuery(
                    view="queue",
                    mode="event",
                    status="all" if bool(args.include_reviewed) else "pending",
                    hours=hours,
                    limit=min(100, wanted - len(rows)),
                    cursor=cursor,
                ),
                principal=principal,
            )
            page = list(queue.get("tasks") or ())
            # The desk chose them; the run's window decides which of them belong to it.
            rows.extend(row for row in page if _within_window(row, window))
            cursor = str(queue.get("next_cursor") or "")
            if not page or not cursor:
                break
        for row in rows:
            view = desk.evidence(
                TaskRef(task_id=str(row["task_id"]), task_version=str(row["task_version"])), principal=principal
            )
            agent = dict(view.get("agent") or {})
            verdict = dict(agent.get("verdict") or {})
            if not verdict:
                continue  # degraded or unjudged: there is no card to review
            tasks.append(
                {
                    "task_id": str(row["task_id"]),
                    "task_version": str(row["task_version"]),
                    "event_id": str(row["event_id"]),
                    "headline_zh": str(verdict.get("headline_zh") or ""),
                    # The Program's own bounded projection, not the whole reviewer view. The full view
                    # carries every de-duplicated member and all provenance; handing that to a reasoning
                    # model made it think until it hit the output ceiling and returned nothing at all
                    # (6/6 empty on the first attempt). This is also the fairer question: judge the card
                    # against what the Program was actually shown.
                    "evidence_json": render_model_evidence_json(
                        _drafter_context(view).event_semantics_payload(), predictor="event_semantics"
                    ),
                    "card_json": canonical_json(
                        {
                            "verdict": verdict,
                            "final_decision": agent.get("final_decision"),
                            "override_rule": agent.get("override_rule"),
                        }
                    ),
                    # The ledger the model was shown when it judged, so the drafter can check novelty against
                    # the same history rather than against hindsight.
                    "told_json": canonical_json(list((agent.get("trace") or {}).get("told") or ())),
                }
            )
    if not tasks:
        return 2, {"ok": False, "error": {"code": "news_review_drafter_nothing_to_draft"}}

    # Same endpoint plumbing as the judge: a drafting model is a metric-side tool, not a Program route.
    endpoint = configured_lm_endpoint(
        settings,
        model_name=str(args.model),
        api_key=source.api_key,
        base_url=source.base_url,
        request_config=source.request,
    )
    batch = build_draft_batch(
        ReviewDrafter(
            build_drafter_lm(
                model_name=endpoint.model_name,
                api_key=endpoint.api_key,
                api_base=endpoint.api_base,
                model_kwargs=endpoint.model_kwargs,
                temperature=endpoint.temperature,
                max_tokens=4_096,
            )
        ),
        tasks,
    )
    payload = batch.model_dump(mode="json")
    payload["batch_sha256"] = batch.batch_sha256
    _write_json(str(args.out), payload)
    drafted = [entry for entry in batch.drafts if entry.error is None]
    with_gold = [entry for entry in drafted if entry.draft.expected is not None]
    return 0, {
        "ok": True,
        "data": {
            "drafts_written_to": str(args.out),
            "batch_sha256": batch.batch_sha256,
            "drafter": batch.drafter,
            "tasks": len(tasks),
            # The look-back actually used, so a run-scoped draft says which window it drew from rather
            # than leaving the reader to assume `--hours`.
            "lookback_hours": hours,
            "drafted": len(drafted),
            "unique_tasks": len({entry.task_id for entry in batch.drafts}),
            "with_gold": len(with_gold),
            "failed": len(batch.drafts) - len(drafted),
            "note": "proposals only - a human must accept each one through `tracefold news review submit`",
        },
    }


def _handle_learning_migrate_corpus(args: Namespace, settings: Any, stable: Any) -> tuple[int, dict[str, Any]]:
    """Carry a stale-cohort development dataset forward by replaying the current arm (#300).

    The database is held only at the edges: one read to export the episodes, one short write transaction
    to seal the carried subset. The replay itself — hours on the single-slot task endpoint — runs with no
    connection open, and `--from-receipt` freezes from an already-written receipt so a failure after the
    replay never re-pays it; the seal re-verifies the receipt's hash and the arm it was proven against.
    """

    import json as _json
    from pathlib import Path as _Path

    from tracefold.app.repository_session import postgres_connection
    from tracefold.news.learning.dataset import DevelopmentDatasetStore
    from tracefold.news.learning.ledger import LearningLedger
    from tracefold.news.learning.migration import run_corpus_migration
    from tracefold.news.learning.objective import DevelopmentEpisode
    from tracefold.news.program.artifact import load_program_artifact

    out_dir = _Path(str(args.out))
    out_dir.mkdir(parents=True, exist_ok=True)
    from_receipt = str(getattr(args, "from_receipt", "") or "")

    def _store(conn: Any) -> DevelopmentDatasetStore:
        return DevelopmentDatasetStore(
            conn,
            stable=stable,
            ledger=LearningLedger(conn, stable=stable, principal="operator"),
        )

    if from_receipt:
        receipt = _json.loads(_Path(from_receipt).read_text(encoding="utf-8"))
    else:
        artifact = load_program_artifact(stable.program_sha256)
        compile_program, runtime_identity = _baseline_model_route("compile_live", settings=settings, artifact=artifact)
        judge = _migration_judge(args, settings)
        with postgres_connection(settings, role="workers") as conn:
            export = _store(conn).development_migration_export(str(args.from_dataset))
        episodes = tuple(DevelopmentEpisode.model_validate(episode) for episode in export.episodes)
        delivered = {
            str(case.get("case_id")): str(case.get("event_id") or case.get("case_id"))
            for case in export.dataset_payload.get("cases") or ()
            if str(case.get("delivery_truth")) == "observed_sent"
        }
        if compile_program is None:  # pragma: no cover - `_baseline_model_route` raises first
            raise ValueError("news_program_baseline_compile_route_not_configured")
        receipt = run_corpus_migration(
            episodes,
            program=compile_program,
            judge=judge,
            max_model_cases=int(args.max_model_cases),
            from_dataset_sha=str(args.from_dataset),
            replay_identity={
                "bundle_sha": stable.bundle_sha,
                "program_sha256": stable.program_sha256,
                **runtime_identity,
            },
            delivered_event_ids_by_case=delivered,
        )
        _write_json(str(out_dir / "migration-receipt.json"), receipt)

    if not receipt["counts"]["equivalent"]:
        # The store refuses an empty carry loudly; a zero-exit here would convert that refusal into a
        # success an operator's `&&` chain sails past.
        return 1, {
            "ok": False,
            "error": "news_learning_migration_carries_no_cases",
            "data": {"counts": receipt["counts"], "out": str(out_dir)},
        }
    with postgres_connection(settings, role="workers") as conn, conn.transaction():
        manifest = _store(conn).freeze_migrated_dataset(from_dataset_sha=str(args.from_dataset), receipt=receipt)
    _write_json(str(out_dir / "migrated-dataset.json"), manifest.model_dump(mode="json"))
    return 0, {
        "ok": True,
        "data": {
            "receipt_sha256": receipt["receipt_sha256"],
            "counts": receipt["counts"],
            "migrated_dataset_sha": manifest.artifact_sha,
            "excluded_case_ids": (manifest.migration or {}).get("excluded_case_ids"),
            "out": str(out_dir),
        },
    }


def _migration_judge(args: Namespace, settings: Any) -> Any:
    """The card-equivalence judge on the compiler reflection route — the same restriction a dataset-bound
    baseline carries, because a migration seal is release evidence."""

    from tracefold.app.llm import configured_lm_endpoint
    from tracefold.news.learning.baseline import build_judge

    reflection = getattr(settings.llm, "news_compiler_reflection", None)
    if reflection is None or not reflection.configured:
        raise ValueError("news_program_baseline_judge_endpoint_not_configured")
    endpoint = configured_lm_endpoint(
        settings,
        model_name=str(args.semantic_judge).strip(),
        api_key=reflection.api_key,
        base_url=reflection.base_url,
        request_config=reflection.request,
    )
    return build_judge(
        model_name=endpoint.model_name,
        api_key=endpoint.api_key,
        api_base=endpoint.api_base,
        model_kwargs=endpoint.model_kwargs,
        temperature=0 if endpoint.temperature is None else endpoint.temperature,
        structured_output=endpoint.structured_output,
    )
