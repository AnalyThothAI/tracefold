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
)
from .news_learning_documents import (
    _canonical_model_document,
    _read_json_or_yaml,
    _write_json,
)
from .news_learning_runtime import (
    _insert_learning_artifact,
    _learning_program_judges,
    _learning_recording_replay_capability,
    _load_candidate_bundle,
)


def _handle_learning(args: Namespace) -> tuple[int, dict[str, Any]]:
    from tracefold.app.repository_session import postgres_connection
    from tracefold.news import (
        LEARNING_EPOCH,
        REVIEW_RUBRIC_VERSION,
        CandidateEvaluator,
        CandidateManifest,
        ClosedWindow,
        DatasetSpec,
        EvaluationRequest,
        ProposalReceipt,
        canonical_json,
        canonical_sha,
    )

    settings = load_settings(require_ws_token=False)
    action = str(args.learning_command)
    from tracefold.app.learning_runtime import active_arm_manifest

    try:
        if action == "canary":
            from tracefold.app.learning_runtime import artifact_valid_candidate_bundles
            from tracefold.app.repository_session import repositories
            from tracefold.news import apply_canary_control, parse_canary_control
            from tracefold.news.agents.programs.candidates import compiled_canary_candidates

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
        if action == "compile":
            from tracefold.app.learning_runtime import compose_news_program_runtime
            from tracefold.app.llm import configured_lm_endpoint
            from tracefold.news.agents.program_compiler_launcher import ProgramCompilerLauncher
            from tracefold.news.agents.program_compiler_proxy import (
                CompilerModelProxyGrant,
                CompilerProviderEndpointSecret,
                CompilerProxySecretConfig,
            )
            from tracefold.news.agents.program_compiler_sandbox import CompilerSandboxPolicy
            from tracefold.news.agents.program_compiler_security import (
                CompileBudgetV3,
                CompileReceiptChain,
                CompilerProxyTariff,
                CompilerRunnerReceiptsV3,
                ContentAddressedCompileReceipt,
                OptimizerCompileProvenanceV3,
                gepa_metric_call_ceiling,
                seal_compile_input,
            )
            from tracefold.news.agents.program_compiler_source import (
                compiler_source_sha256,
                proxy_source_sha256,
            )
            from tracefold.news.agents.program_compiler_trusted import (
                METRIC_JUDGE_MAX_TOKENS,
                METRIC_JUDGE_TIMEOUT_SECONDS,
                REFLECTION_MAX_TOKENS,
                REFLECTION_TIMEOUT_SECONDS,
                ProgramPatchV2,
                apply_trusted_program_patch,
                build_eligible_demo_bank,
                load_exact_stable_program,
                program_machine_diff,
                write_program_candidate_artifact,
            )
            from tracefold.platform.config.models import news_model_availability

            availability = news_model_availability(settings)
            if not availability.program_configured or not availability.triage_model:
                raise ValueError("news_learning_compile_model_not_configured")
            parent = load_exact_stable_program()
            if (
                parent.program_sha256 != stable.program_sha256
                or parent.parent_program_sha256 is not None
                or parent.schema_version != "news_semantic_program_artifact_v2"
                or parent.factory_id != "tracefold.news.semantic_program.factory_v4"
            ):
                raise ValueError("news_learning_compile_stable_program_mismatch")
            # This is the only DB contact in the compile path.  It ends before
            # any container starts, and the runner/sidecar receive no DSN.
            with postgres_connection(settings, role="serve") as conn:
                evaluator = CandidateEvaluator(conn, stable=stable, judges={})
                compile_export = evaluator.development_compile_export(str(args.development))
            eligible_demo_bank = build_eligible_demo_bank(
                dataset_sha=compile_export.dataset_sha,
                dataset_payload=compile_export.dataset_payload,
                episodes=compile_export.episodes,
            )
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
                timeout=float(parent.execution.route_deadline_seconds),
                max_tokens=max(
                    parent.route_spec.event_semantics_max_tokens,
                    parent.route_spec.reader_card_max_tokens,
                ),
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
                parent_state_sha256=parent.state_sha256,
                stable_bundle_sha256=stable.bundle_sha,
                target_runtime_manifest_sha256=stable.runtime_model_bindings_sha256,
                eligible_demo_bank_root_sha256=eligible_demo_bank.eligible_demo_bank_root_sha256,
                task=task_binding,
                reflection=reflection_binding,
                metric_judge=metric_judge_binding,
                proxy_grant_sha256=proxy_grant.grant_sha256,
                proxy_config_sha256=proxy_secrets.secret_free_config_sha256,
                tariff_sha256=proxy_secrets.tariff_sha256,
                proxy_tariff=tariff,
                compiler_source_sha256=source_sha,
                proxy_source_sha256=proxy_source_sha,
                compiler_lock_sha256=parent.quality_kernel.dependency_lock_sha256,
                sandbox_policy_sha256=sandbox_policy.policy_sha256,
                compiler_image_digest=compiler_image,
                budget=budget,
            )
            launched = ProgramCompilerLauncher(
                policy=sandbox_policy,
                compiler_source_sha256=source_sha,
                compiler_lock_sha256=parent.quality_kernel.dependency_lock_sha256,
                compiler_image=compiler_image,
                proxy_source_sha256=proxy_source_sha,
            ).launch(
                input_document=canonical_json(bundle.model_dump(mode="json")),
                input_bundle_sha256=bundle.bundle_sha256,
                proxy_secret_config=proxy_secrets,
            )
            patch = _canonical_model_document(
                launched.patch_document,
                ProgramPatchV2,
                code="news_learning_compile_patch_invalid",
            )
            runner = _canonical_model_document(
                launched.runner_receipts_document,
                CompilerRunnerReceiptsV3,
                code="news_learning_compile_runner_receipt_invalid",
            )
            proxy_execution = launched.proxy_execution_receipt
            try:
                metric_call_ceiling = gepa_metric_call_ceiling(
                    max_metric_calls=budget.max_metric_calls,
                    optimizer_config=runner.optimizer_config,
                    expected_example_count=bundle.corpus.episode_count,
                )
            except ValueError as exc:
                raise ValueError("news_learning_compile_receipt_cross_binding_mismatch") from exc
            if (
                runner.input_bundle_sha256 != bundle.bundle_sha256
                or runner.parent_program_sha256 != parent.program_sha256
                or runner.parent_state_sha256 != parent.state_sha256
                or runner.proxy_grant_sha256 != proxy_grant.grant_sha256
                or runner.compiler_source_sha256 != source_sha
                or runner.proxy_source_sha256 != proxy_source_sha
                or runner.compiler_lock_sha256 != parent.quality_kernel.dependency_lock_sha256
                or runner.sandbox_policy_sha256 != sandbox_policy.policy_sha256
                or runner.task_endpoint_identity_sha256 != task_binding.endpoint.binding_sha256
                or runner.reflection_endpoint_identity_sha256 != reflection_binding.endpoint.binding_sha256
                or runner.metric_judge_endpoint_identity_sha256 != metric_judge_binding.endpoint.binding_sha256
                or patch.parent_program_sha256 != parent.program_sha256
                or patch.parent_state_sha256 != parent.state_sha256
                or patch.eligible_demo_bank_root_sha256 != eligible_demo_bank.eligible_demo_bank_root_sha256
                or proxy_execution.grant_sha256 != proxy_grant.grant_sha256
                or proxy_execution.task_model_calls != runner.task_model_calls
                or proxy_execution.reflection_model_calls != runner.reflection_model_calls
                or proxy_execution.metric_judge_model_calls != runner.metric_judge_model_calls
                or runner.metric_judge_model_calls > runner.metric_judge_attempts
                or runner.metric_judge_failures > runner.metric_judge_attempts
                or runner.metric_calls > metric_call_ceiling
                or proxy_execution.task_cost_microusd != runner.task_cost_microusd
                or proxy_execution.reflection_cost_microusd != runner.reflection_cost_microusd
                or proxy_execution.metric_judge_cost_microusd != runner.metric_judge_cost_microusd
                or proxy_execution.actual_cost_microusd != runner.actual_cost_microusd
                or proxy_execution.reserved_cost_microusd > budget.max_cost_microusd
                or proxy_execution.task_failures
                or proxy_execution.reflection_failures
                or proxy_execution.metric_judge_failures > runner.metric_judge_failures
                or len(proxy_execution.calls)
                < runner.task_model_calls + runner.reflection_model_calls + runner.metric_judge_model_calls
                or any(
                    (call.role != "metric_judge" and (not call.provider_invoked or call.error_code is not None))
                    or (call.provider_invoked and call.total_tokens <= 0)
                    or call.reserved_cost_microusd > budget.max_call_cost_microusd
                    or call.provider_cost_microusd > call.reserved_cost_microusd
                    for call in proxy_execution.calls
                )
            ):
                raise ValueError("news_learning_compile_receipt_cross_binding_mismatch")
            optimizer_payload = {
                "runner_optimizer_config": runner.optimizer_config,
                "proxy_grant": proxy_grant.model_dump(mode="json"),
                "proxy_execution": proxy_execution.model_dump(mode="json"),
                "input_bundle_sha256": bundle.bundle_sha256,
            }
            receipts = CompileReceiptChain.issue(
                (
                    ContentAddressedCompileReceipt.issue("corpus", bundle.corpus),
                    ContentAddressedCompileReceipt.issue("metric", runner.metric),
                    ContentAddressedCompileReceipt.issue("optimizer_config", optimizer_payload),
                    ContentAddressedCompileReceipt.issue("trajectory", runner.trajectory),
                    ContentAddressedCompileReceipt.issue("checkpoint", runner.checkpoint),
                    ContentAddressedCompileReceipt.issue("sandbox_launch", launched.launch_receipt),
                    ContentAddressedCompileReceipt.issue("patch", patch),
                )
            )
            provenance = OptimizerCompileProvenanceV3(
                development_dataset_sha=bundle.corpus.development_dataset_sha,
                learning_epoch="program_v6",
                learning_epoch_started_at_ms=bundle.corpus.learning_epoch_started_at_ms,
                projection_schema_id=bundle.corpus.projection_schema_id,
                metric_sha256=canonical_sha(runner.metric),
                optimizer_config_sha256=canonical_sha(optimizer_payload),
                seed=budget.seed,
                max_metric_calls=budget.max_metric_calls,
                max_task_model_calls=budget.max_task_model_calls,
                max_reflection_model_calls=budget.max_reflection_model_calls,
                max_metric_judge_model_calls=budget.max_metric_judge_model_calls,
                max_cost_microusd=budget.max_cost_microusd,
                max_call_cost_microusd=budget.max_call_cost_microusd,
                metric_calls=runner.metric_calls,
                task_model_calls=runner.task_model_calls,
                reflection_model_calls=runner.reflection_model_calls,
                metric_judge_attempts=runner.metric_judge_attempts,
                metric_judge_model_calls=runner.metric_judge_model_calls,
                metric_judge_failures=runner.metric_judge_failures,
                task_cost_microusd=runner.task_cost_microusd,
                reflection_cost_microusd=runner.reflection_cost_microusd,
                metric_judge_cost_microusd=runner.metric_judge_cost_microusd,
                actual_cost_microusd=runner.actual_cost_microusd,
                trajectory_sha256=canonical_sha(runner.trajectory),
                checkpoint_sha256=canonical_sha(runner.checkpoint),
                parent_program_sha256=parent.program_sha256,
                parent_state_sha256=parent.state_sha256,
                quality_kernel_sha256=parent.quality_kernel.sha256,
                rule_pack_root_sha256=parent.rule_pack_root_sha256,
                development_dataset_payload_sha256=bundle.corpus.development_dataset_payload_sha256,
                case_root_sha256=bundle.corpus.case_root_sha256,
                cluster_root_sha256=bundle.corpus.cluster_root_sha256,
                episode_projection_root_sha256=bundle.corpus.episode_projection_root_sha256,
                episode_count=bundle.corpus.episode_count,
                eligible_demo_bank_root_sha256=eligible_demo_bank.eligible_demo_bank_root_sha256,
                patch_sha256=patch.patch_sha256,
                receipt_payload_root_sha256=receipts.receipt_payload_root_sha256,
                sandbox_launch_receipt_sha256=launched.launch_receipt.launch_receipt_sha256,
                target_runtime_manifest_sha256=stable.runtime_model_bindings_sha256,
                task_endpoint_identity_sha256=task_binding.endpoint.binding_sha256,
                reflection_endpoint_identity_sha256=reflection_binding.endpoint.binding_sha256,
                metric_judge_endpoint_identity_sha256=metric_judge_binding.endpoint.binding_sha256,
                compiler_source_sha256=source_sha,
                compiler_lock_sha256=parent.quality_kernel.dependency_lock_sha256,
                sandbox_policy_sha256=sandbox_policy.policy_sha256,
            )
            candidate_artifact = apply_trusted_program_patch(
                parent,
                patch,
                eligible_demo_bank,
                provenance,
            )
            artifact_directory = write_program_candidate_artifact(
                candidate_artifact,
                artifact_root=Path(str(args.artifact_root)),
            )
            machine_diff = program_machine_diff(parent, candidate_artifact)
            payload = {
                "target": "program",
                "hypothesis": "Repair the accepted program_v6 failure clusters with bounded DSPy GEPA.",
                "target_dimensions": list(runner.target_dimensions),
                "failure_cluster_ids": list(runner.failure_cluster_ids),
                "guardrails": [
                    "fixed_factory_v4",
                    "development_only",
                    "holdout_unseen",
                    "no_dynamic_code",
                    "no_auto_promotion",
                ],
                "generator_kind": "model",
                "generator_prompt_sha": provenance.metric_sha256,
                "generator_model_sha": canonical_sha(
                    {
                        "task": task_binding.binding_sha256,
                        "reflection": reflection_binding.binding_sha256,
                        "metric_judge": metric_judge_binding.binding_sha256,
                        "proxy_config": proxy_secrets.secret_free_config_sha256,
                        "tariff": proxy_secrets.tariff_sha256,
                    }
                ),
                "generator_execution_sha": canonical_sha(provenance.model_dump(mode="json")),
                "candidate_patch_sha": patch.patch_sha256,
                "program_parent_sha256": parent.program_sha256,
                "program_sha256": candidate_artifact.program_sha256,
                "program_state_sha256": candidate_artifact.state_sha256,
                "program_artifact_path": artifact_directory,
                "program_machine_diff": machine_diff,
                "program_patch": patch.model_dump(mode="json"),
                "compile_provenance": provenance.model_dump(mode="json"),
                "compile_receipt_chain": receipts.model_dump(mode="json"),
            }
            _write_json(str(args.out), payload)
            return 0, {
                "ok": True,
                "data": {
                    "path": args.out,
                    "program_sha256": candidate_artifact.program_sha256,
                    "program_state_sha256": candidate_artifact.state_sha256,
                    "candidate_patch_sha": patch.patch_sha256,
                    "artifact_directory": artifact_directory,
                    "compile_receipt_root_sha256": receipts.receipt_payload_root_sha256,
                },
            }

        if action == "propose":
            from tracefold.news.agents.program_compiler_security import (
                CompileReceiptChain,
                OptimizerCompileProvenanceV3,
                ProgramMachineDiffV3,
                validate_compile_receipt_chain_v3,
            )
            from tracefold.news.agents.program_compiler_trusted import (
                ProgramPatchV2,
                build_eligible_demo_bank,
                load_program_artifact,
                optimizer_provenance_from_artifact,
                program_machine_diff,
                reapply_exact_candidate,
            )

            spec = _read_json_or_yaml(str(args.file))
            target = str(spec.get("target") or "")
            candidate_arm = stable
            program_artifact_path: str | None = None
            program_fields: dict[str, Any] = {}
            compile_receipt_chain_payload: dict[str, Any] | None = None
            if target == "program":
                program_artifact_path = str(spec.get("program_artifact_path") or "")
                artifact = load_program_artifact(program_artifact_path)
                if artifact.parent_program_sha256 != stable.program_sha256:
                    raise ValueError("news_learning_program_parent_mismatch")
                if str(spec.get("program_sha256") or artifact.program_sha256) != artifact.program_sha256:
                    raise ValueError("news_learning_program_artifact_identity_mismatch")
                if str(spec.get("program_state_sha256") or "") != artifact.state_sha256:
                    raise ValueError("news_learning_program_state_identity_mismatch")
                parent_artifact = load_program_artifact()
                machine_diff = ProgramMachineDiffV3.model_validate(program_machine_diff(parent_artifact, artifact))
                provided_diff = ProgramMachineDiffV3.model_validate(spec.get("program_machine_diff"))
                if provided_diff.model_dump(mode="json") != machine_diff.model_dump(mode="json"):
                    raise ValueError("news_learning_program_machine_diff_mismatch")
                manifest_provenance = optimizer_provenance_from_artifact(artifact)
                compile_provenance = OptimizerCompileProvenanceV3.model_validate(spec.get("compile_provenance"))
                if compile_provenance.model_dump(mode="json") != manifest_provenance.model_dump(mode="json"):
                    raise ValueError("news_learning_program_compile_provenance_mismatch")
                if str(args.development) != compile_provenance.development_dataset_sha:
                    raise ValueError("news_learning_program_development_dataset_mismatch")
                patch = ProgramPatchV2.model_validate(spec.get("program_patch"))
                chain = CompileReceiptChain.model_validate(spec.get("compile_receipt_chain"))
                validate_compile_receipt_chain_v3(
                    chain,
                    provenance=compile_provenance,
                    patch_sha256=patch.patch_sha256,
                    parent_program_sha256=parent_artifact.program_sha256,
                    parent_state_sha256=parent_artifact.state_sha256,
                    eligible_demo_bank_root_sha256=patch.eligible_demo_bank_root_sha256,
                    target_runtime_manifest_sha256=stable.runtime_model_bindings_sha256,
                )
                with postgres_connection(settings, role="serve") as export_conn:
                    compile_export = CandidateEvaluator(
                        export_conn,
                        stable=stable,
                        judges={},
                    ).development_compile_export(str(args.development))
                if compile_export.dataset_sha != compile_provenance.development_dataset_sha:
                    raise ValueError("news_learning_program_development_reexport_mismatch")
                eligible_demo_bank = build_eligible_demo_bank(
                    dataset_sha=compile_export.dataset_sha,
                    dataset_payload=compile_export.dataset_payload,
                    episodes=compile_export.episodes,
                )
                reapply_exact_candidate(
                    parent_artifact,
                    patch,
                    eligible_demo_bank,
                    artifact,
                )
                arm_payload = stable.model_dump(mode="json")
                arm_payload.update(
                    program_version=artifact.program_version,
                    program_sha256=artifact.program_sha256,
                )
                candidate_arm = type(stable).model_validate(arm_payload)
                candidate_patch_sha = patch.patch_sha256
                if str(spec.get("candidate_patch_sha") or "") != candidate_patch_sha:
                    raise ValueError("news_learning_program_patch_sha_mismatch")
                program_fields = {
                    "program_parent_sha256": stable.program_sha256,
                    "program_candidate_sha256": artifact.program_sha256,
                    "program_state_sha256": artifact.state_sha256,
                    "program_machine_diff": machine_diff.model_dump(mode="json"),
                    "compile_provenance": compile_provenance.model_dump(mode="json"),
                }
                compile_receipt_chain_payload = chain.model_dump(mode="json")
            elif target == "policy":
                if str(spec.get("parent_program_sha256") or "") != stable.program_sha256:
                    raise ValueError("news_learning_policy_parent_program_mismatch")
                policy = dict(stable.policy)
                policy.update(dict(spec.get("policy") or {}))
                arm_payload = stable.model_dump(mode="json")
                arm_payload.update(policy=policy, policy_sha256=canonical_sha(policy))
                candidate_arm = type(stable).model_validate(arm_payload)
                patch_payload: Mapping[str, Any] = {"policy": dict(spec.get("policy") or {})}
                candidate_patch_sha = canonical_sha(patch_payload)
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
                    generator_prompt_sha=spec.get("generator_prompt_sha"),
                    generator_model_sha=spec.get("generator_model_sha"),
                    generator_execution_sha=spec.get("generator_execution_sha"),
                    registered_at_ms=registered_at_ms,
                    candidate_patch_sha=candidate_patch_sha,
                    declared_target_dimensions=dimensions,
                    guardrails=tuple(str(value) for value in spec.get("guardrails") or ()),
                    **program_fields,
                )
                candidate = CandidateManifest(
                    target=target,
                    parent_stable_sha=stable.bundle_sha,
                    candidate_arm=candidate_arm,
                    hypothesis=str(spec.get("hypothesis") or ""),
                    target_dimensions=dimensions,
                    development_dataset_sha=str(args.development),
                    proposal_receipt=receipt,
                )
                proposal_payload = receipt.model_dump(mode="json")
                if compile_receipt_chain_payload is not None:
                    _insert_learning_artifact(
                        conn,
                        kind="compile_receipt",
                        payload=compile_receipt_chain_payload,
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
                        "candidate_sha": candidate.candidate_sha,
                        "proposal_sha": sealed_proposal_sha,
                        "manifest": candidate.model_dump(mode="json"),
                    },
                    parent_sha=stable.bundle_sha,
                    created_at_ms=registered_at_ms,
                )
            payload = {
                "candidate_sha": candidate.candidate_sha,
                "candidate": candidate.model_dump(mode="json"),
                "program_artifacts": (
                    {candidate.candidate_arm.program_sha256: str(Path(program_artifact_path).resolve())}
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
                from tracefold.news import evaluation_run_sha

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
                judges = {}
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
