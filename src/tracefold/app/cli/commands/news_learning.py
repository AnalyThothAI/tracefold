from __future__ import annotations

import asyncio
import time
from argparse import Namespace
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from tracefold.platform.config.loader import load_settings

from .news_learning_baseline import (
    _handle_learning_baseline,
    _handle_learning_draft_reviews,
)
from .news_learning_documents import (
    _canonical_model_document,
    _read_json_or_yaml,
    _write_json,
)

if TYPE_CHECKING:
    from tracefold.news.program.contracts import SemanticJudge

from .news_learning_runtime import (
    _insert_learning_artifact,
    _learning_program_judges,
    _learning_recording_replay_capability,
    _load_candidate_bundle,
)


def _handle_learning(args: Namespace) -> tuple[int, dict[str, Any]]:
    from tracefold.app.repository_session import postgres_connection
    from tracefold.news.artifact_identity import canonical_json, canonical_sha
    from tracefold.news.learning.evaluator import (
        LEARNING_EPOCH,
        CandidateEvaluator,
        CandidateManifest,
        ClosedWindow,
        DatasetSpec,
        EvaluationRequest,
        ProposalReceipt,
    )
    from tracefold.news.learning.review import REVIEW_RUBRIC_VERSION

    settings = load_settings(require_ws_token=False)
    action = str(args.learning_command)
    from tracefold.app.learning_runtime import active_arm_manifest

    try:
        if action == "canary":
            from tracefold.app.learning_runtime import artifact_valid_candidate_bundles
            from tracefold.app.repository_session import repositories
            from tracefold.news.learning.canary import apply_canary_control, parse_canary_control
            from tracefold.news.program.resources.candidates import compiled_canary_candidates

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

        stable = active_arm_manifest(settings)
        if action == "baseline":
            return _handle_learning_baseline(args, settings, stable)
        if action == "draft-reviews":
            return _handle_learning_draft_reviews(args, settings, stable)
        if action == "experiment":
            from .news_learning_experiment import handle_experiment

            return handle_experiment(args, settings, stable)
        if action == "compile":
            from tracefold.app.learning_runtime import compose_news_program_runtime
            from tracefold.app.llm import configured_lm_endpoint
            from tracefold.news.learning.compiler.launcher import ProgramCompilerLauncher
            from tracefold.news.learning.compiler.proxy import (
                CompilerModelProxyGrant,
                CompilerProviderEndpointSecret,
                CompilerProxySecretConfig,
            )
            from tracefold.news.learning.compiler.sandbox import CompilerSandboxPolicy
            from tracefold.news.learning.compiler.security import (
                CompileBudgetV3,
                CompilerBuildAttestation,
                CompileRecordV1,
                CompilerProxyTariff,
                CompilerRunnerReceipts,
                seal_compile_input,
            )
            from tracefold.news.learning.compiler.source_identity import (
                COMPILER_DEPENDENCY_LOCK_SHA256,
                compiler_source_sha256,
                proxy_source_sha256,
            )
            from tracefold.news.learning.compiler.trusted import (
                METRIC_JUDGE_MAX_TOKENS,
                METRIC_JUDGE_TIMEOUT_SECONDS,
                REFLECTION_MAX_TOKENS,
                REFLECTION_TIMEOUT_SECONDS,
                apply_trusted_program_patch,
                changed_predictors,
                load_exact_stable_program,
                write_program_candidate_artifact,
            )
            from tracefold.news.program.runtime import (
                PROGRAM_EVENT_SEMANTICS_MAX_TOKENS,
                PROGRAM_READER_CARD_MAX_TOKENS,
                PROGRAM_ROUTE_DEADLINE_SECONDS,
            )
            from tracefold.platform.config.models import news_model_availability

            availability = news_model_availability(settings)
            if not availability.program_configured or not availability.triage_model:
                raise ValueError("news_learning_compile_model_not_configured")
            parent = load_exact_stable_program()
            if parent.program_sha256 != stable.program_sha256:
                raise ValueError("news_learning_compile_stable_program_mismatch")
            # Both DB reads in this path happen here, before any container starts, and the runner and
            # sidecar receive no DSN.
            with postgres_connection(settings, role="serve") as conn:
                evaluator = CandidateEvaluator(conn, stable=stable, judges={})
                compile_export = evaluator.development_compile_export(str(args.development))
            composition = compose_news_program_runtime(settings)
            endpoint = composition.event_semantics_primary
            compiler_image = str(args.compiler_image).strip()
            if not (
                compiler_image.startswith("sha256:")
                and len(compiler_image) == 71
                and all(character in "0123456789abcdef" for character in compiler_image[7:])
            ):
                raise ValueError("news_learning_compile_image_must_be_local_sha256")
            endpoint_secret = CompilerProviderEndpointSecret(
                model=endpoint.model_name,
                api_key=endpoint.api_key,
                api_base=endpoint.api_base,
                timeout=float(PROGRAM_ROUTE_DEADLINE_SECONDS),
                max_tokens=max(PROGRAM_EVENT_SEMANTICS_MAX_TOKENS, PROGRAM_READER_CARD_MAX_TOKENS),
                temperature=0,
                model_kwargs=endpoint.model_kwargs,
            )
            configured_tariff = getattr(settings.llm, "news_compiler_tariff", None)
            if configured_tariff is None or not bool(getattr(configured_tariff, "configured", False)):
                raise ValueError("news_learning_compile_tariff_not_configured")
            tariff = CompilerProxyTariff.model_validate(configured_tariff.model_dump(mode="json"))
            # #143: the reflection endpoint is its own configuration. Passing the task endpoint for both made
            # the student its own teacher (DSPy's guidance is to reflect with a *larger* model when optimizing a
            # small one), gave the reflection call the task route's token ceiling and deadline, and pointed a
            # multi-hour optimization at the same single-slot GPU that serves production Triage.
            configured_reflection = getattr(settings.llm, "news_compiler_reflection", None)
            if configured_reflection is None or not bool(getattr(configured_reflection, "configured", False)):
                raise ValueError("news_learning_compile_reflection_not_configured")
            compiler_endpoint = configured_lm_endpoint(
                settings,
                model_name=str(configured_reflection.model),
                api_key=str(configured_reflection.api_key),
                base_url=str(configured_reflection.base_url),
            )
            reflection_secret = CompilerProviderEndpointSecret(
                model=compiler_endpoint.model_name,
                api_key=compiler_endpoint.api_key,
                api_base=compiler_endpoint.api_base,
                timeout=REFLECTION_TIMEOUT_SECONDS,
                max_tokens=REFLECTION_MAX_TOKENS,
                temperature=1,
                model_kwargs=compiler_endpoint.model_kwargs,
            )
            metric_judge_secret = CompilerProviderEndpointSecret(
                model=compiler_endpoint.model_name,
                api_key=compiler_endpoint.api_key,
                api_base=compiler_endpoint.api_base,
                timeout=METRIC_JUDGE_TIMEOUT_SECONDS,
                max_tokens=METRIC_JUDGE_MAX_TOKENS,
                temperature=0,
                model_kwargs=compiler_endpoint.model_kwargs,
            )
            proxy_secrets = CompilerProxySecretConfig(
                task=endpoint_secret,
                reflection=reflection_secret,
                metric_judge=metric_judge_secret,
                tariff=tariff,
            )
            task_binding = endpoint_secret.binding("task")
            reflection_binding = reflection_secret.binding("reflection")
            metric_judge_binding = metric_judge_secret.binding("metric_judge")
            source_sha = compiler_source_sha256()
            proxy_source_sha = proxy_source_sha256()
            sandbox_policy = CompilerSandboxPolicy.issue()
            proxy_grant = CompilerModelProxyGrant.issue(
                task=task_binding,
                reflection=reflection_binding,
                metric_judge=metric_judge_binding,
                max_task_model_calls=int(args.max_task_model_calls),
                max_reflection_model_calls=int(args.max_reflection_model_calls),
                max_metric_judge_model_calls=int(args.max_metric_judge_model_calls),
                max_cost_microusd=int(args.max_cost_microusd),
                tariff=tariff,
                proxy_config_sha256=proxy_secrets.secret_free_config_sha256,
                proxy_source_sha256=proxy_source_sha,
            )
            budget = CompileBudgetV3(
                max_metric_calls=int(args.max_metric_calls),
                max_task_model_calls=int(args.max_task_model_calls),
                max_reflection_model_calls=int(args.max_reflection_model_calls),
                max_metric_judge_model_calls=int(args.max_metric_judge_model_calls),
                max_cost_microusd=int(args.max_cost_microusd),
                max_call_cost_microusd=proxy_grant.max_call_cost_microusd,
                seed=int(args.seed),
            )
            bundle = seal_compile_input(
                dataset_sha=compile_export.dataset_sha,
                dataset_payload=compile_export.dataset_payload,
                episodes=compile_export.episodes,
                # Declared here, on the trusted host. The compiler records the rubric its corpus was accepted
                # under; it never looks one up, so the review plane stays out of its import graph.
                review_rubric_version=REVIEW_RUBRIC_VERSION,
                parent_program_sha256=parent.program_sha256,
                stable_bundle_sha256=stable.bundle_sha,
                target_runtime_manifest_sha256=stable.runtime_model_bindings_sha256,
                task=task_binding,
                reflection=reflection_binding,
                metric_judge=metric_judge_binding,
                proxy_grant_sha256=proxy_grant.grant_sha256,
                proxy_config_sha256=proxy_secrets.secret_free_config_sha256,
                proxy_tariff=tariff,
                compiler_source_sha256=source_sha,
                proxy_source_sha256=proxy_source_sha,
                compiler_lock_sha256=COMPILER_DEPENDENCY_LOCK_SHA256,
                sandbox_policy_sha256=sandbox_policy.policy_sha256,
                compiler_image_digest=compiler_image,
                budget=budget,
            )
            with postgres_connection(settings, role="serve") as clock_conn:
                compiled_at_ms = int(
                    clock_conn.execute(
                        "SELECT floor(extract(epoch FROM clock_timestamp()) * 1000)::bigint AS now_ms"
                    ).fetchone()["now_ms"]
                )
            launched = ProgramCompilerLauncher(
                policy=sandbox_policy,
                compiler_source_sha256=source_sha,
                compiler_lock_sha256=COMPILER_DEPENDENCY_LOCK_SHA256,
                compiler_image=compiler_image,
                proxy_source_sha256=proxy_source_sha,
            ).launch(
                input_document=canonical_json(bundle.model_dump(mode="json")),
                input_bundle_sha256=bundle.bundle_sha256,
                proxy_secret_config=proxy_secrets,
            )
            runner = _canonical_model_document(
                launched.runner_receipts_document,
                CompilerRunnerReceipts,
                code="news_learning_compile_runner_receipt_invalid",
            )
            # The one patch this compile produced, typed by the receipt that carries it. The runner used
            # to write it out a second time as `patch.json` and the host parsed both, which is two
            # documents making one claim across one trust boundary.
            patch = runner.run.patch
            proxy_execution = launched.proxy_execution_receipt
            # What only this point can check: the untrusted runner's own counters against the trusted
            # sidecar's ledger. Two parties, two independent counts. Everything else the compile has to
            # prove — the per-call reservation arithmetic, the budget ceilings, the write-set shape —
            # is a property of the record, checked by the record, once.
            if (
                runner.input_bundle_sha256 != bundle.bundle_sha256
                or runner.container_source_sha256 != source_sha
                or runner.container_proxy_source_sha256 != proxy_source_sha
                or patch.parent_program_sha256 != parent.program_sha256
                or proxy_execution.grant_sha256 != proxy_grant.grant_sha256
                or proxy_execution.task_model_calls != runner.spend.task_model_calls
                or proxy_execution.reflection_model_calls != runner.spend.reflection_model_calls
                or proxy_execution.metric_judge_model_calls != runner.spend.metric_judge_model_calls
                or proxy_execution.task_cost_microusd != runner.spend.task_cost_microusd
                or proxy_execution.reflection_cost_microusd != runner.spend.reflection_cost_microusd
                or proxy_execution.metric_judge_cost_microusd != runner.spend.metric_judge_cost_microusd
                or proxy_execution.actual_cost_microusd != runner.spend.actual_cost_microusd
                or proxy_execution.metric_judge_failures > runner.spend.metric_judge_failures
                or len(proxy_execution.calls)
                < runner.spend.task_model_calls
                + runner.spend.reflection_model_calls
                + runner.spend.metric_judge_model_calls
            ):
                raise ValueError("news_learning_compile_receipt_cross_binding_mismatch")
            candidate_artifact = apply_trusted_program_patch(parent, patch)
            record = CompileRecordV1.issue(
                parent_program_sha256=parent.program_sha256,
                program_sha256=candidate_artifact.program_sha256,
                development_dataset_sha256=bundle.corpus.development_dataset_sha,
                learning_epoch_started_at_ms=bundle.corpus.learning_epoch_started_at_ms,
                review_rubric_version=bundle.corpus.review_rubric_version,
                episode_count=bundle.corpus.episode_count,
                episode_projection_root_sha256=bundle.corpus.episode_projection_root_sha256,
                target_runtime_manifest_sha256=stable.runtime_model_bindings_sha256,
                task_model=task_binding,
                reflection_model=reflection_binding,
                metric_judge_model=metric_judge_binding,
                # Carried whole, not re-listed: the optimization the container produced and the spend it
                # counted are the same two objects the runner receipt already holds.
                run=runner.run,
                budget=budget,
                tariff=tariff,
                usage=proxy_execution,
                spend=runner.spend,
                sandbox=launched.launch_receipt,
                compiler_build=CompilerBuildAttestation(
                    compiler_image_digest=compiler_image,
                    proxy_image_digest=compiler_image,
                    host_source_sha256=source_sha,
                    host_proxy_source_sha256=proxy_source_sha,
                    host_lock_sha256=COMPILER_DEPENDENCY_LOCK_SHA256,
                    image_source_sha256=launched.launch_receipt.image_preflight["compiler_source_sha256"],
                    image_proxy_source_sha256=launched.launch_receipt.image_preflight["proxy_source_sha256"],
                    image_lock_sha256=launched.launch_receipt.image_preflight["compiler_lock_sha256"],
                    container_source_sha256=runner.container_source_sha256,
                    container_proxy_source_sha256=runner.container_proxy_source_sha256,
                ),
                created_at_ms=compiled_at_ms,
            )
            artifact_directory = write_program_candidate_artifact(
                candidate_artifact,
                artifact_root=Path(str(args.artifact_root)),
            )
            payload = {
                "target": "program",
                "hypothesis": "Repair the accepted program_v7 failure clusters with bounded DSPy GEPA.",
                "target_dimensions": list(runner.run.target_dimensions),
                "failure_cluster_ids": list(runner.run.failure_cluster_ids),
                "guardrails": [
                    "fixed_factory_v6",
                    "development_only",
                    "holdout_unseen",
                    "no_dynamic_code",
                    "no_auto_promotion",
                ],
                "generator_kind": "model",
                # One record, one identity. The receipt used to carry a prompt digest, a model digest and
                # an execution digest that between them re-hashed the same compile three times.
                "generator_execution_sha": record.compile_record_sha256,
                "program_parent_sha256": parent.program_sha256,
                "program_sha256": candidate_artifact.program_sha256,
                "program_artifact_path": artifact_directory,
                "compile_record_sha256": record.compile_record_sha256,
                "compile_record": record.model_dump(mode="json"),
                # Operator-facing only, derived from the two artifacts rather than trusted: which advisory
                # this compile actually rewrote.
                "changed_predictors": list(changed_predictors(parent, candidate_artifact)),
            }
            _write_json(str(args.out), payload)
            return 0, {
                "ok": True,
                "data": {
                    "path": args.out,
                    "program_sha256": candidate_artifact.program_sha256,
                    "artifact_directory": artifact_directory,
                    "compile_record_sha256": record.compile_record_sha256,
                },
            }

        if action == "propose":
            from tracefold.news.learning.compiler.security import CompileRecordV1, validate_compile_record
            from tracefold.news.learning.compiler.trusted import (
                load_program_artifact,
                reapply_exact_candidate,
            )

            spec = _read_json_or_yaml(str(args.file))
            target = str(spec.get("target") or "")
            candidate_arm = stable
            program_artifact_path: str | None = None
            program_fields: dict[str, Any] = {}
            compile_record_payload: dict[str, Any] | None = None
            if target == "program":
                program_artifact_path = str(spec.get("program_artifact_path") or "")
                artifact = load_program_artifact(program_artifact_path)
                if str(spec.get("program_sha256") or artifact.program_sha256) != artifact.program_sha256:
                    raise ValueError("news_learning_program_artifact_identity_mismatch")
                parent_artifact = load_program_artifact()
                if parent_artifact.program_sha256 != stable.program_sha256:
                    raise ValueError("news_learning_program_parent_mismatch")
                record = validate_compile_record(
                    CompileRecordV1.model_validate(spec.get("compile_record")),
                    parent_program_sha256=parent_artifact.program_sha256,
                    program_sha256=artifact.program_sha256,
                    development_dataset_sha256=str(args.development),
                    target_runtime_manifest_sha256=stable.runtime_model_bindings_sha256,
                )
                with postgres_connection(settings, role="serve") as export_conn:
                    compile_export = CandidateEvaluator(
                        export_conn,
                        stable=stable,
                        judges={},
                    ).development_compile_export(str(args.development))
                if compile_export.dataset_sha != record.development_dataset_sha256:
                    raise ValueError("news_learning_program_development_reexport_mismatch")
                # The record carries the write-set; rebuilding the candidate from it is what proves the
                # artifact on disk is the one that compile produced.
                patch = record.run.patch
                reapply_exact_candidate(parent_artifact, patch, artifact)
                arm_payload = stable.model_dump(mode="json")
                arm_payload.update(program_sha256=artifact.program_sha256)
                candidate_arm = type(stable).model_validate(arm_payload)
                program_fields = {
                    "program_parent_sha256": stable.program_sha256,
                    "program_candidate_sha256": artifact.program_sha256,
                    "compile_record_sha256": record.compile_record_sha256,
                }
                compile_record_payload = record.model_dump(mode="json")
            elif target == "policy":
                if str(spec.get("parent_program_sha256") or "") != stable.program_sha256:
                    raise ValueError("news_learning_policy_parent_program_mismatch")
                policy = dict(stable.policy)
                policy.update(dict(spec.get("policy") or {}))
                arm_payload = stable.model_dump(mode="json")
                arm_payload.update(policy=policy, policy_sha256=canonical_sha(policy))
                candidate_arm = type(stable).model_validate(arm_payload)
            else:
                raise ValueError("candidate_kind_unsupported")
            dimensions = tuple(str(value) for value in spec.get("target_dimensions") or ())
            generator_kind = str(spec.get("generator_kind") or ("model" if target == "program" else "human"))
            if target == "program" and generator_kind != "model":
                raise ValueError("news_learning_program_generator_must_be_model")
            with postgres_connection(settings, role="workers") as conn, conn.transaction():
                development = conn.execute(
                    "SELECT artifact_sha FROM news_learning_artifacts "
                    "WHERE artifact_sha = %s AND kind = 'dataset' "
                    "AND payload->>'role' = 'development' AND payload->>'learning_epoch' = %s",
                    (str(args.development), LEARNING_EPOCH),
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
                    failure_cluster_ids=tuple(str(value) for value in spec.get("failure_cluster_ids") or ()),
                    generator_kind=generator_kind,
                    generator_execution_sha=spec.get("generator_execution_sha"),
                    registered_at_ms=registered_at_ms,
                    declared_target_dimensions=dimensions,
                    guardrails=tuple(str(value) for value in spec.get("guardrails") or ()),
                    **program_fields,
                )
                registered = CandidateManifest(
                    target=target,
                    parent_stable_sha=stable.bundle_sha,
                    candidate_arm=candidate_arm,
                    hypothesis=str(spec.get("hypothesis") or ""),
                    target_dimensions=dimensions,
                    development_dataset_sha=str(args.development),
                    proposal_receipt=receipt,
                )
                proposal_payload = receipt.model_dump(mode="json")
                if compile_record_payload is not None:
                    _insert_learning_artifact(
                        conn,
                        kind="compile_record",
                        payload=compile_record_payload,
                        parent_sha=str(args.development),
                        created_at_ms=registered_at_ms,
                    )
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
                    payload=proposal_payload,
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
                "program_artifacts": (
                    {registered.candidate_arm.program_sha256: str(Path(program_artifact_path).resolve())}
                    if program_artifact_path is not None
                    else {}
                ),
            }
            _write_json(str(args.out), payload)
            return 0, {"ok": True, "data": {"path": args.out, **payload}}

        candidate, artifact_paths = _load_candidate_bundle(str(getattr(args, "candidate", "") or ""))
        catalog = () if candidate is None else (candidate,)
        with postgres_connection(settings, role="workers") as conn:
            if action == "freeze":
                if args.role == "validation" and candidate is None:
                    raise ValueError("news_learning_validation_candidate_required")
                evaluator = CandidateEvaluator(
                    conn,
                    stable=stable,
                    judges={},
                    candidate_catalog=catalog,
                )
                manifest = asyncio.run(
                    evaluator.freeze_dataset(
                        DatasetSpec(
                            role=str(args.role),
                            window=ClosedWindow(from_ms=int(args.from_ms), to_ms=int(args.to_ms)),
                            observation_ref=candidate.candidate_sha if candidate is not None else None,
                        )
                    )
                )
                payload = manifest.model_dump(mode="json")
                _write_json(str(args.out), payload)
                return 0, {"ok": True, "data": {"path": args.out, **payload}}

            if candidate is None:
                raise ValueError("news_learning_candidate_required")
            observation_manifest = str(getattr(args, "observation_manifest", "") or "") or None
            verify_recordings = bool(getattr(args, "verify_recordings", False))
            if action == "shadow" and observation_manifest is None and not bool(args.live_program):
                raise ValueError("news_learning_shadow_live_program_confirmation_required")
            stage = str(args.stage) if action == "evaluate" else action
            if verify_recordings and stage not in {"offline", "holdout"}:
                raise ValueError(f"news_learning_recording_verification_stage_unsupported:{stage}")
            request = EvaluationRequest(
                development_dataset_sha=str(args.development),
                validation_dataset_sha=str(args.validation) or None,
                candidate_sha=candidate.candidate_sha,
                stage=stage,
                observation_manifest_sha=observation_manifest,
            )
            recording_replay = None
            if verify_recordings:
                from tracefold.news.learning.evaluator import evaluation_run_sha

                run_sha = evaluation_run_sha(
                    request,
                    stable_bundle_sha=stable.bundle_sha,
                    candidate_sha=candidate.candidate_sha,
                )
                recording_replay = _learning_recording_replay_capability(
                    conn,
                    stable=stable,
                    candidate=candidate,
                    artifact_paths=artifact_paths,
                    run_sha=run_sha,
                )
                judges: Mapping[tuple[Literal["stable", "candidate"], str], SemanticJudge] = {}
            else:
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
            report = asyncio.run(
                evaluator.evaluate(
                    request,
                    recording_replay=recording_replay,
                )
            )
            payload = report.model_dump(mode="json")
            _write_json(str(args.out), payload)
            code = 0 if report.gate_outcome == "pass" else 1
            return code, {"ok": report.gate_outcome == "pass", "data": {"path": args.out, **payload}}
    except (ValueError, PermissionError, RuntimeError) as exc:
        return 2, {"ok": False, "error": str(exc)}
