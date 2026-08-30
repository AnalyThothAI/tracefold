from __future__ import annotations

import asyncio
import time
from argparse import Namespace
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from tracefold.platform.config.loader import load_settings

from .news_learning_baseline import (
    _handle_learning_baseline,
    _handle_learning_draft_reviews,
    _handle_learning_readiness,
)
from .news_learning_documents import (
    _read_json_or_yaml,
    _write_json,
)
from .news_learning_runtime import (
    _insert_learning_artifact,
    _learning_program_judges,
    _load_candidate_bundle,
)


def _parse_taxonomy_shadow_request(document: Mapping[str, Any], *, limit: int) -> tuple[str, list[tuple[str, Any]]]:
    from tracefold.news.program.contracts import TriageContext

    raw_cases = document.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise ValueError("news_taxonomy_shadow_cases_required")
    if len(raw_cases) > limit:
        raise ValueError("news_taxonomy_shadow_case_limit_exceeded")
    cases: list[tuple[str, Any]] = []
    for raw in raw_cases:
        if not isinstance(raw, Mapping):
            raise ValueError("news_taxonomy_shadow_case_invalid")
        case_id = str(raw.get("case_id") or "")
        if not case_id:
            raise ValueError("news_taxonomy_shadow_case_identity_required")
        cases.append((case_id, TriageContext.model_validate(raw.get("context"))))
    if len({case_id for case_id, _context in cases}) != len(cases):
        raise ValueError("news_taxonomy_shadow_case_identity_duplicate")
    return str(document.get("candidate_registration_sha256") or ""), cases


def _verify_taxonomy_shadow_registration(
    conn: Any,
    registration_sha: str,
    *,
    code_identity: Any,
    stable: Any,
    program: Any,
) -> Any:
    from tracefold.news.learning.taxonomy import verify_taxonomy_candidate_registration

    registration = verify_taxonomy_candidate_registration(
        conn,
        registration_sha,
        code_identity=code_identity,
        stable_bundle_sha256=stable.bundle_sha,
        runtime_model_bindings_sha256=stable.runtime_model_bindings_sha256,
        policy_sha256=stable.policy_sha256,
    )
    if (
        registration.taxonomy_program_sha256 != program.shadow_program_sha256
        or registration.taxonomy_model_binding_sha256 != program.model_binding_sha256
    ):
        raise ValueError("news_taxonomy_shadow_registration_program_mismatch")
    return registration


def _execute_taxonomy_shadow_cases(program: Any, cases: list[tuple[str, Any]]) -> list[tuple[str, Any]]:
    try:
        return [(case_id, program(context)) for case_id, context in cases]
    except MemoryError:
        raise
    except Exception:
        # Provider errors are typed observations. Anything escaping the Program is a defect;
        # fail the command without reflecting exception text that may contain credentials.
        raise RuntimeError("news_taxonomy_shadow_program_failed") from None


def _persist_taxonomy_shadow_observations(
    conn: Any,
    registration: Any,
    observations: list[tuple[str, Any]],
) -> list[dict[str, Any]]:
    created_at_ms = int(
        conn.execute("SELECT floor(extract(epoch FROM clock_timestamp()) * 1000)::bigint AS now_ms").fetchone()[
            "now_ms"
        ]
    )
    receipts: list[dict[str, Any]] = []
    for case_id, observation in observations:
        artifact_sha = _insert_learning_artifact(
            conn,
            kind="shadow_observation",
            payload=observation.model_dump(mode="json"),
            parent_sha=registration.artifact_sha256,
            created_at_ms=created_at_ms,
        )
        receipts.append(
            {
                "case_id": case_id,
                "event_id": observation.event_id,
                "outcome": observation.outcome,
                "physical_attempt_n": len(observation.attempts),
                "observation_sha256": observation.observation_sha256,
                "artifact_sha256": artifact_sha,
            }
        )
    return receipts


def _handle_taxonomy_shadow(args: Namespace, settings: Any) -> tuple[int, dict[str, Any]]:
    from tracefold.app.learning_runtime import active_arm_manifest, compose_news_program_runtime
    from tracefold.app.repository_session import postgres_connection
    from tracefold.news.learning.taxonomy import taxonomy_code_identity
    from tracefold.platform.postgres.client import transaction

    document = _read_json_or_yaml(str(args.file))
    registration_sha, cases = _parse_taxonomy_shadow_request(document, limit=int(args.limit))
    runtime = compose_news_program_runtime(settings)
    program = runtime.taxonomy_shadow_program()
    stable = active_arm_manifest(settings, runtime_composition=runtime)
    code_identity = taxonomy_code_identity()

    def verify(conn: Any) -> Any:
        return _verify_taxonomy_shadow_registration(
            conn,
            registration_sha,
            code_identity=code_identity,
            stable=stable,
            program=program,
        )

    with postgres_connection(settings, role="workers") as conn:
        verify(conn)
    observations = _execute_taxonomy_shadow_cases(program, cases)
    with postgres_connection(settings, role="workers") as conn, transaction(conn):
        registration = verify(conn)
        receipts = _persist_taxonomy_shadow_observations(conn, registration, observations)
    output = {
        "candidate_registration_sha256": registration_sha,
        "taxonomy_program_sha256": program.shadow_program_sha256,
        "taxonomy_model_binding_sha256": program.model_binding_sha256,
        "receipts": receipts,
    }
    _write_json(str(args.out), output)
    return 0, {"ok": True, "data": {**output, "report_written_to": str(args.out)}}


def _handle_learning(args: Namespace) -> tuple[int, dict[str, Any]]:
    from tracefold.app.repository_session import postgres_connection
    from tracefold.news.learning.contracts import epoch_id_for_bundle
    from tracefold.news.learning.evaluate import (
        CandidateEvaluator,
        CandidateManifest,
        ClosedWindow,
        DatasetSpec,
        EvaluationRequest,
        ProposalReceipt,
    )

    settings = load_settings(require_ws_token=False)
    action = str(getattr(args, "learning_command", "") or getattr(args, "release_command", ""))
    from tracefold.app.learning_runtime import active_arm_manifest

    try:
        if action == "canary":
            from tracefold.app.repository_session import repositories
            from tracefold.news.program.resources.candidates import compiled_canary_candidates
            from tracefold.news.release.canary import apply_canary_control, parse_canary_control
            from tracefold.news.release.runtime import artifact_valid_candidate_bundles

            subcommand = str(args.canary_command)
            payload = {
                "action": subcommand,
                "candidate_sha": getattr(args, "candidate", None),
                "activation_id": getattr(args, "activation", None),
                "reason": getattr(args, "reason", None),
            }
            command = parse_canary_control(payload)
            stable_bundle_sha = ""
            shipped: dict[str, str] = {}
            if subcommand in {"arm", "resume"}:
                stable = active_arm_manifest(settings)
                stable_bundle_sha = stable.bundle_sha
                shipped = artifact_valid_candidate_bundles(stable, compiled_canary_candidates())
            stamp = int(time.time() * 1000)
            with repositories(settings) as repos, repos.transaction():
                result = apply_canary_control(
                    repos,
                    command,
                    stable_bundle_sha=stable_bundle_sha,
                    shipped_candidates=shipped,
                    now_ms=stamp,
                )
            return 0, {"ok": True, "data": result}

        if action == "taxonomy-register":
            from tracefold.app.learning_runtime import compose_news_program_runtime
            from tracefold.news.learning.taxonomy import (
                TaxonomyCandidateRegistrationV1,
                taxonomy_code_identity,
                verify_taxonomy_active_deployment,
            )
            from tracefold.platform.postgres.client import transaction

            runtime = compose_news_program_runtime(settings)
            program = runtime.taxonomy_shadow_program()
            stable = active_arm_manifest(settings, runtime_composition=runtime)
            code_identity = taxonomy_code_identity()
            with postgres_connection(settings, role="workers") as conn, transaction(conn):
                registered_at_ms = int(
                    conn.execute(
                        "SELECT floor(extract(epoch FROM clock_timestamp()) * 1000)::bigint AS now_ms"
                    ).fetchone()["now_ms"]
                )
                deployment = verify_taxonomy_active_deployment(
                    conn,
                    stable_bundle_sha256=stable.bundle_sha,
                )
                registration = TaxonomyCandidateRegistrationV1.issue(
                    code_identity=code_identity,
                    deployment=deployment,
                    policy_sha256=stable.policy_sha256,
                    runtime_model_bindings_sha256=stable.runtime_model_bindings_sha256,
                    taxonomy_program_sha256=program.shadow_program_sha256,
                    taxonomy_model_binding_sha256=program.model_binding_sha256,
                    registered_at_ms=registered_at_ms,
                )
                artifact_sha = _insert_learning_artifact(
                    conn,
                    kind="candidate_registration",
                    payload=registration.model_dump(mode="json"),
                    parent_sha=None,
                    created_at_ms=registered_at_ms,
                )
                if artifact_sha != registration.artifact_sha256:
                    raise ValueError("news_taxonomy_candidate_registration_identity_mismatch")
            return 0, {
                "ok": True,
                "data": {
                    "candidate_registration_sha256": artifact_sha,
                    "registered_at_ms": registration.registered_at_ms,
                    "tested_git_sha": registration.tested_git_sha,
                    "taxonomy_program_sha256": registration.taxonomy_program_sha256,
                    "taxonomy_model_binding_sha256": registration.taxonomy_model_binding_sha256,
                },
            }

        if action == "taxonomy-shadow":
            return _handle_taxonomy_shadow(args, settings)

        if action == "taxonomy-evaluate":
            from tracefold.news.learning.taxonomy import (
                taxonomy_code_identity,
                verify_taxonomy_candidate_registration,
                verify_taxonomy_evaluation_cases,
                verify_taxonomy_regression_gates,
            )
            from tracefold.news.learning.taxonomy_evaluation import (
                TaxonomyEvaluationContextV1,
                build_taxonomy_evaluation_report,
            )
            from tracefold.platform.postgres.client import transaction

            document = _read_json_or_yaml(str(args.file))
            cases = document.get("cases")
            if not isinstance(cases, list):
                raise ValueError("news_taxonomy_evaluation_cases_required")
            stable = active_arm_manifest(settings)
            code_identity = taxonomy_code_identity()
            stamp = int(time.time() * 1000)
            with postgres_connection(settings, role="workers") as conn, transaction(conn):
                candidate_registration_sha256 = str(document.get("candidate_registration_sha256") or "")
                registration = verify_taxonomy_candidate_registration(
                    conn,
                    candidate_registration_sha256,
                    code_identity=code_identity,
                    stable_bundle_sha256=stable.bundle_sha,
                    runtime_model_bindings_sha256=stable.runtime_model_bindings_sha256,
                    policy_sha256=stable.policy_sha256,
                )
                regression_gates = verify_taxonomy_regression_gates(
                    conn,
                    document.get("regression_gates") or {},
                    code_identity=code_identity,
                    registration=registration,
                )
                gold_verification = verify_taxonomy_evaluation_cases(
                    conn,
                    cases,
                    registration=registration,
                )
                if gold_verification.shadow_population is None:  # pragma: no cover - verifier owns this invariant
                    raise RuntimeError("news_taxonomy_shadow_population_missing")
                taxonomy_report = build_taxonomy_evaluation_report(
                    gold_verification.cases,
                    context=TaxonomyEvaluationContextV1(
                        candidate_registration_sha256=candidate_registration_sha256,
                        candidate_registration=registration,
                        gold_ledger_root_sha256=gold_verification.ledger_root_sha256,
                        regression_gates=regression_gates,
                        shadow_population=gold_verification.shadow_population,
                    ),
                )
                payload = taxonomy_report.model_dump(mode="json")
                payload["report_sha256"] = taxonomy_report.report_sha256
                artifact_sha = _insert_learning_artifact(
                    conn,
                    kind="evaluation_report",
                    payload=payload,
                    parent_sha=None,
                    created_at_ms=stamp,
                )
            _write_json(str(args.out), payload)
            return 0, {
                "ok": True,
                "data": {
                    "artifact_sha": artifact_sha,
                    "report_sha256": taxonomy_report.report_sha256,
                    "outcome": taxonomy_report.outcome,
                    "case_n": taxonomy_report.case_n,
                    "cluster_n": taxonomy_report.cluster_n,
                    "report_written_to": str(args.out),
                },
            }

        stable = active_arm_manifest(settings)
        if action == "readiness":
            return _handle_learning_readiness(args, settings, stable)
        if action == "baseline":
            return _handle_learning_baseline(args, settings, stable)
        if action == "run":
            # #253 §7 Phase C. The one recommended path, composed from the three commands around it: it
            # holds no additional authority, and every artifact it writes is one of theirs.
            from .news_learning_run import _handle_learning_run

            return _handle_learning_run(args, settings, stable)
        if action == "draft-reviews":
            return _handle_learning_draft_reviews(args, settings, stable)
        if action == "optimize":
            from .news_learning_experiment import handle_research

            return handle_research(args, settings, stable)
        if action == "register":
            from tracefold.news.learning.contracts import PromptCandidateV1
            from tracefold.news.learning.objective import DevelopmentEpisode, build_gepa_objective_plan
            from tracefold.news.program.artifact import (
                apply_program_patch,
                load_stable_program_artifact,
                write_program_candidate_artifact,
            )
            from tracefold.news.release.candidate import validate_declared_objective_summary

            prompt = PromptCandidateV1.model_validate(_read_json_or_yaml(str(args.candidate)))
            parent = load_stable_program_artifact()
            # Everything a candidate has to satisfy to be *registrable*, in one place and none of it about
            # where the text came from (#202 §7). A patch a person wrote and a patch GEPA wrote are
            # admissible on identical terms; what differs is only whether the candidate carries its own
            # objective summary to be checked against the plan re-derived below.
            if parent.program_sha256 != stable.program_sha256:
                raise ValueError("news_learning_register_stable_program_mismatch")
            if prompt.parent_program_sha256 != parent.program_sha256:
                raise ValueError("news_learning_register_parent_not_active_stable")
            if prompt.target_runtime_manifest_sha256 != stable.runtime_model_bindings_sha256:
                raise ValueError("news_learning_register_runtime_manifest_mismatch")
            if prompt.development_dataset_sha256 != str(args.development):
                raise ValueError("news_learning_register_dataset_mismatch")
            candidate_artifact = apply_program_patch(parent, prompt.patch.applied_to(parent))
            from tracefold.news.learning.dataset import DevelopmentDatasetStore

            with postgres_connection(settings, role="serve") as export_conn:
                export = DevelopmentDatasetStore(
                    export_conn,
                    stable=stable,
                ).development_compile_export(str(args.development))
            plan = build_gepa_objective_plan(
                tuple(DevelopmentEpisode.model_validate(episode) for episode in export.episodes)
            )
            validate_declared_objective_summary(
                prompt.objective_summary,
                episode_projection_root_sha256=export.episode_projection_root_sha256,
                plan=plan,
            )
            if not plan.target_failure_cluster_ids:
                raise ValueError("news_program_compile_no_verified_failure_clusters")
            arm_payload = stable.model_dump(mode="json")
            arm_payload.update(program_sha256=candidate_artifact.program_sha256)
            candidate_arm = type(stable).model_validate(arm_payload)
            artifact_directory = write_program_candidate_artifact(
                candidate_artifact,
                artifact_root=Path(str(args.artifact_root)),
            )
            with postgres_connection(settings, role="workers") as conn, conn.transaction():
                development = conn.execute(
                    "SELECT artifact_sha FROM news_learning_artifacts "
                    "WHERE artifact_sha = %s AND kind = 'dataset' "
                    "AND payload->>'role' = 'development' AND payload->>'learning_epoch' = %s",
                    (str(args.development), epoch_id_for_bundle(stable.bundle_sha)),
                ).fetchone()
                if development is None:
                    raise ValueError("news_learning_development_dataset_not_found")
                registered_at_ms = int(
                    conn.execute(
                        "SELECT floor(extract(epoch FROM clock_timestamp()) * 1000)::bigint AS now_ms"
                    ).fetchone()["now_ms"]
                )
                receipt = ProposalReceipt.issue(
                    development_dataset_sha=str(args.development),
                    # The registrar's own projection, not the generator's claim.
                    development_episode_projection_root_sha256=export.episode_projection_root_sha256,
                    failure_cluster_ids=plan.target_failure_cluster_ids,
                    generator_kind="model" if prompt.optimizer else "human",
                    registered_at_ms=registered_at_ms,
                    declared_target_dimensions=plan.target_dimensions,
                    guardrails=(
                        "fixed_execution_envelope",
                        "development_only",
                        "holdout_unseen",
                        "no_dynamic_code",
                        "no_auto_promotion",
                    ),
                    program_parent_sha256=parent.program_sha256,
                    program_candidate_sha256=candidate_artifact.program_sha256,
                    prompt_candidate_sha256=prompt.candidate_sha256,
                )
                registered = CandidateManifest(
                    parent_stable_sha=stable.bundle_sha,
                    candidate_arm=candidate_arm,
                    hypothesis=str(args.hypothesis)
                    or "Repair the accepted failure clusters with the registered Prompt patch.",
                    target_dimensions=plan.target_dimensions,
                    development_dataset_sha=str(args.development),
                    proposal_receipt=receipt,
                )
                prompt_sha = _insert_learning_artifact(
                    conn,
                    kind="prompt_candidate",
                    payload=prompt.model_dump(mode="json"),
                    parent_sha=str(args.development),
                    created_at_ms=registered_at_ms,
                )
                if prompt_sha != prompt.candidate_sha256:
                    raise ValueError("news_learning_prompt_candidate_hash_mismatch")
                proposal_sha = _insert_learning_artifact(
                    conn,
                    kind="candidate_registration",
                    payload=receipt.registration_payload,
                    parent_sha=str(args.development),
                    created_at_ms=registered_at_ms,
                )
                if proposal_sha != receipt.registration_receipt_sha:
                    raise ValueError("news_learning_candidate_registration_hash_mismatch")
                sealed_proposal_sha = _insert_learning_artifact(
                    conn,
                    kind="proposal",
                    payload=receipt.model_dump(mode="json"),
                    parent_sha=str(args.development),
                    created_at_ms=registered_at_ms,
                )
                _insert_learning_artifact(
                    conn,
                    kind="candidate",
                    payload={
                        "candidate_sha": registered.candidate_sha,
                        "proposal_sha": sealed_proposal_sha,
                        "manifest": registered.model_dump(mode="json"),
                    },
                    parent_sha=stable.bundle_sha,
                    created_at_ms=registered_at_ms,
                )
            payload = {
                "candidate_sha": registered.candidate_sha,
                "candidate": registered.model_dump(mode="json"),
                "program_artifacts": {registered.candidate_arm.program_sha256: str(Path(artifact_directory).resolve())},
            }
            _write_json(str(args.out), payload)
            return 0, {"ok": True, "data": {"path": args.out, **payload}}

        candidate, artifact_paths = _load_candidate_bundle(str(getattr(args, "candidate", "") or ""))
        catalog = () if candidate is None else (candidate,)
        with postgres_connection(settings, role="workers") as conn:
            if action == "freeze":
                if args.role == "validation" and candidate is None:
                    raise ValueError("news_learning_validation_candidate_required")
                from tracefold.news.learning.dataset import DevelopmentDatasetStore
                from tracefold.news.learning.ledger import LearningLedger
                from tracefold.news.release.candidate import CandidateRegistry

                ledger = LearningLedger(conn, stable=stable, principal="operator")
                datasets = DevelopmentDatasetStore(conn, stable=stable, ledger=ledger)
                # Admission is the release plane's, and it happens here rather than inside the freeze:
                # candidate validation re-derives the Objective Plan from the corpus this store exports,
                # so the reverse edge would be a cycle (#202 §8).
                admitted = None
                if candidate is not None:
                    admitted = CandidateRegistry(
                        conn,
                        datasets=datasets,
                        ledger=ledger,
                        stable=stable,
                        catalog={item.candidate_sha: item for item in catalog},
                    ).admit_for_validation(candidate.candidate_sha)
                manifest = asyncio.run(
                    datasets.freeze_dataset(
                        DatasetSpec(
                            role=str(args.role),
                            window=ClosedWindow(from_ms=int(args.from_ms), to_ms=int(args.to_ms)),
                            observation_ref=candidate.candidate_sha if candidate is not None else None,
                        ),
                        admitted=admitted,
                    )
                )
                payload = manifest.model_dump(mode="json")
                _write_json(str(args.out), payload)
                return 0, {"ok": True, "data": {"path": args.out, **payload}}

            if candidate is None:
                raise ValueError("news_learning_candidate_required")
            observation_manifest = str(getattr(args, "observation_manifest", "") or "") or None
            if action == "shadow" and observation_manifest is None and not bool(args.live_program):
                raise ValueError("news_learning_shadow_live_program_confirmation_required")
            stage = str(args.stage) if action == "evaluate" else action
            request = EvaluationRequest(
                development_dataset_sha=str(args.development),
                validation_dataset_sha=str(args.validation) or None,
                candidate_sha=candidate.candidate_sha,
                stage=stage,
                observation_manifest_sha=observation_manifest,
            )
            judges = _learning_program_judges(
                conn,
                settings=settings,
                stable=stable,
                candidate=candidate,
                artifact_paths=artifact_paths,
                live=bool(getattr(args, "live_program", False)),
            )
            evaluator = CandidateEvaluator(
                conn,
                stable=stable,
                judges=judges,
                candidate_catalog=(candidate,),
            )
            report = asyncio.run(evaluator.evaluate(request))
            payload = report.model_dump(mode="json")
            _write_json(str(args.out), payload)
            code = 0 if report.gate_outcome == "pass" else 1
            return code, {"ok": report.gate_outcome == "pass", "data": {"path": args.out, **payload}}
    except (ValueError, PermissionError, RuntimeError) as exc:
        return 2, {"ok": False, "error": str(exc)}
