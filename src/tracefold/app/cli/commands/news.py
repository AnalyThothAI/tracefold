from __future__ import annotations

import asyncio
import json
import time
import uuid
from argparse import Namespace
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from tracefold.platform.config.settings import load_settings


def handle_news(args: Namespace) -> tuple[int, dict[str, Any]]:
    if args.news_command == "bus-check":
        return _handle_bus_check()
    if args.news_command == "instruments":
        return _handle_instruments(args)
    if args.news_command == "review":
        return _handle_review(args)
    if args.news_command == "learning":
        return _handle_learning(args)
    if args.news_command == "replay":
        return _handle_replay(args)
    if args.news_command == "dlq":
        return _handle_dlq(args)
    if args.news_command == "why":
        return _handle_why(args)
    return 2, {"ok": False, "error": f"unknown news command: {args.news_command}"}


def _bus(settings: Any) -> Any:
    from tracefold.integrations.rabbitmq import RabbitMQBus

    url = settings.news.broker.url
    if not url:
        raise ValueError("news_broker_url_missing")
    return RabbitMQBus(
        url=url,
        name_prefix=settings.news.broker.name_prefix,
        connect_timeout_seconds=settings.news.broker.connect_timeout_seconds,
    )


def _handle_bus_check() -> tuple[int, dict[str, Any]]:
    settings = load_settings(require_ws_token=False)

    async def _run() -> dict[str, Any]:
        bus = _bus(settings)
        try:
            await bus.connect()
            declared = await bus.declare_topology()
            depths = await bus.queue_depths()
        finally:
            await bus.close()
        return {"declared": declared, "queues": depths}

    try:
        result = asyncio.run(_run())
    except Exception as exc:
        return 1, {"ok": False, "error": type(exc).__name__, "detail": str(exc)[:200]}
    return 0, {"ok": True, "data": result}


def _handle_instruments(args: Namespace) -> tuple[int, dict[str, Any]]:
    """Tradeable instrument universe (#75). `snapshot` writes; the rest are read-only."""

    from tracefold.app.repositories import repositories

    settings = load_settings(require_ws_token=False)
    stamp = int(time.time() * 1000)
    action = str(getattr(args, "action", "summary") or "summary")

    if action == "snapshot":
        from tracefold.integrations.venues import (
            fetch_binance_instruments,
            fetch_hyperliquid_instruments,
            fetch_us_reference_instruments,
        )

        venues = settings.news.venues
        fetchers = []
        if venues.binance:
            fetchers.append(("binance", fetch_binance_instruments))
        if venues.hyperliquid:
            fetchers.append(("hyperliquid", fetch_hyperliquid_instruments))
        if venues.us_reference:
            fetchers.append(("us_reference", fetch_us_reference_instruments))
        if not fetchers:
            return 1, {"ok": False, "error": "news_venues_all_disabled"}
        instruments: list[Any] = []
        errors: list[str] = []
        for venue, fetch in fetchers:
            try:
                instruments.extend(asyncio.run(fetch()))
            except Exception as exc:
                errors.append(f"{venue}:{getattr(exc, 'code', None) or type(exc).__name__}")
        if not instruments:
            return 1, {"ok": False, "error": "news_venue_snapshot_empty", "venues": errors}
        with repositories(settings) as repos, repos.transaction():
            seeds = repos.instruments.reconcile_seed_aliases(now_ms=stamp)
            result = repos.instruments.apply_snapshot(instruments, now_ms=stamp)
            learned = repos.instruments.learn_aliases_from_universe(now_ms=stamp)
            dangling = repos.instruments.dangling_seed_aliases()
        return 0, {
            "ok": True,
            "data": {
                "total": result.total,
                "venues": list(result.venues),
                "delisted": result.delisted,
                "aliases_seeded": seeds,
                "aliases_learned": learned,
                "dangling_aliases": [f"{r['alias']}->{r['base_symbol']}" for r in dangling],
                "venue_errors": errors,
            },
        }

    # The workers role, like every other read-only News command: the CLI runs inside the workers container, which
    # is the only place the serve password file is absent.
    with repositories(settings) as repos:
        if action == "summary":
            return 0, {"ok": True, "data": repos.instruments.universe_summary()}
        if action == "unmatched":
            days = int(args.days)
            rows = repos.instruments.unmatched_provider_tags(since_ms=stamp - days * 86_400_000, limit=int(args.limit))
            dangling = list(repos.instruments.dangling_seed_aliases())
            return 0, {"ok": True, "data": {"days": days, "tags": rows, "dangling_aliases": dangling}}
        symbol = str(getattr(args, "symbol", "") or "").strip()
        if not symbol:
            return 1, {"ok": False, "error": "news_instruments_symbol_required"}
        base = repos.instruments.resolve(symbol)
        return 0, {
            "ok": True,
            "data": {
                "symbol": symbol,
                "base_symbol": base,
                "venues": list(repos.instruments.venues_for(base)),
                # `us.listed` is a reference row, not a venue: without this an operator reads
                # `{"venues": ["us.listed"]}` as "tradeable" (#91).
                "tradeable": repos.instruments.is_tradeable(base),
                "instrument_class": repos.instruments.instrument_classes().get(base),
            },
        }


def _handle_review(args: Namespace) -> tuple[int, dict[str, Any]]:
    from tracefold.app.repositories import postgres_connection
    from tracefold.news import (
        BlindPairwiseSubmission,
        DeskQuery,
        EventRubricSubmission,
        ExternalMissSubmission,
        Principal,
        ReviewDesk,
        TaskRef,
    )
    from tracefold.platform.postgres.postgres_client import transaction

    settings = load_settings(require_ws_token=False)
    principal = Principal(subject="operator")
    action = str(args.review_command)
    try:
        if action == "queue":
            query = DeskQuery(
                view=args.view,
                mode=args.mode,
                cohort=args.cohort,
                stratum=args.stratum,
                proposal=args.proposal,
                task=args.task,
                event=args.event,
                status=args.status,
                hours=int(args.hours),
                limit=min(100, int(args.limit)),
                cursor=args.cursor,
            )
            with postgres_connection(settings, role="serve") as conn:
                data = ReviewDesk(conn).open(query, principal=principal)
            return 0, {"ok": True, "data": data}
        if action == "evidence":
            task = TaskRef(task_id=str(args.task), task_version=str(args.version))
            with postgres_connection(settings, role="serve") as conn:
                data = ReviewDesk(conn).evidence(task, principal=principal)
            return 0, {"ok": True, "data": data}

        payload = _read_json_or_yaml(str(args.file))
        kind = str(payload.get("kind") or "")
        key = str(args.idempotency_key or uuid.uuid4())
        with postgres_connection(settings, role="review") as conn, transaction(conn):
            desk = ReviewDesk(conn)
            if action == "external-miss":
                submission = ExternalMissSubmission.model_validate(payload)
                data = desk.submit(None, submission, principal=principal, idempotency_key=key)
            else:
                submission = (
                    EventRubricSubmission.model_validate(payload)
                    if kind == "event_rubric"
                    else BlindPairwiseSubmission.model_validate(payload)
                )
                task = TaskRef(task_id=str(args.task), task_version=str(args.version))
                data = desk.submit(task, submission, principal=principal, idempotency_key=key)
        return 0, {"ok": True, "data": data}
    except (ValueError, PermissionError) as exc:
        return 2, {"ok": False, "error": str(exc)}


def _handle_learning(args: Namespace) -> tuple[int, dict[str, Any]]:
    from tracefold.app.repositories import postgres_connection
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
            from tracefold.app.repositories import repositories
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
            from tracefold.news.agents.program_compiler_launcher import ProgramCompilerLauncher
            from tracefold.news.agents.program_compiler_proxy import (
                CompilerModelProxyGrant,
                CompilerProviderEndpointSecret,
                CompilerProxySecretConfig,
            )
            from tracefold.news.agents.program_compiler_sandbox import CompilerSandboxPolicy
            from tracefold.news.agents.program_compiler_security import (
                CompileBudgetV2,
                CompileReceiptChain,
                CompilerEndpointIdentity,
                CompilerProxyTariff,
                CompilerRunnerReceiptsV2,
                ContentAddressedCompileReceipt,
                OptimizerCompileProvenanceV2,
                seal_compile_input,
            )
            from tracefold.news.agents.program_compiler_source import (
                compiler_source_sha256,
                proxy_source_sha256,
            )
            from tracefold.news.agents.program_compiler_trusted import (
                REFLECTION_MAX_TOKENS,
                REFLECTION_TIMEOUT_SECONDS,
                ProgramPatchV2,
                apply_trusted_program_patch,
                build_eligible_demo_bank,
                load_exact_stable_program,
                program_machine_diff,
                write_program_candidate_artifact,
            )
            from tracefold.platform.config.settings import news_model_availability

            availability = news_model_availability(settings)
            if not availability.program_configured or not availability.triage_model:
                raise ValueError("news_learning_compile_model_not_configured")
            parent = load_exact_stable_program()
            if (
                parent.program_sha256 != stable.program_sha256
                or parent.parent_program_sha256 is not None
                or parent.schema_version != "news_semantic_program_artifact_v2"
                or parent.factory_id != "tracefold.news.semantic_program.factory_v3"
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
            reflection_secret = CompilerProviderEndpointSecret(
                model=str(configured_reflection.model),
                api_key=str(configured_reflection.api_key),
                api_base=str(configured_reflection.base_url),
                timeout=REFLECTION_TIMEOUT_SECONDS,
                max_tokens=REFLECTION_MAX_TOKENS,
                model_kwargs={},
            )
            proxy_secrets = CompilerProxySecretConfig(
                task=endpoint_secret,
                reflection=reflection_secret,
                tariff=tariff,
            )
            task_identity = CompilerEndpointIdentity.issue(
                model=endpoint_secret.model,
                api_base=endpoint_secret.api_base,
            )
            reflection_identity = CompilerEndpointIdentity.issue(
                model=reflection_secret.model,
                api_base=reflection_secret.api_base,
            )
            source_sha = compiler_source_sha256()
            proxy_source_sha = proxy_source_sha256()
            sandbox_policy = CompilerSandboxPolicy.issue()
            proxy_grant = CompilerModelProxyGrant.issue(
                task_endpoint=task_identity,
                reflection_endpoint=reflection_identity,
                max_model_calls=int(args.max_task_model_calls),
                max_cost_microusd=int(args.max_cost_microusd),
                tariff=tariff,
                task_max_output_tokens=endpoint_secret.max_tokens,
                reflection_max_output_tokens=reflection_secret.max_tokens,
                task_timeout_seconds=endpoint_secret.timeout,
                reflection_timeout_seconds=reflection_secret.timeout,
                proxy_config_sha256=proxy_secrets.secret_free_config_sha256,
                proxy_source_sha256=proxy_source_sha,
            )
            budget = CompileBudgetV2(
                max_metric_calls=int(args.max_metric_calls),
                max_task_model_calls=int(args.max_task_model_calls),
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
                task_endpoint=task_identity,
                reflection_endpoint=reflection_identity,
                proxy_grant_sha256=proxy_grant.grant_sha256,
                proxy_config_sha256=proxy_secrets.secret_free_config_sha256,
                tariff_sha256=proxy_secrets.tariff_sha256,
                proxy_tariff=tariff,
                task_max_output_tokens=endpoint_secret.max_tokens,
                reflection_max_output_tokens=endpoint_secret.max_tokens,
                task_timeout_seconds=endpoint_secret.timeout,
                reflection_timeout_seconds=endpoint_secret.timeout,
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
                CompilerRunnerReceiptsV2,
                code="news_learning_compile_runner_receipt_invalid",
            )
            proxy_execution = launched.proxy_execution_receipt
            if (
                runner.input_bundle_sha256 != bundle.bundle_sha256
                or runner.parent_program_sha256 != parent.program_sha256
                or runner.parent_state_sha256 != parent.state_sha256
                or runner.proxy_grant_sha256 != proxy_grant.grant_sha256
                or runner.compiler_source_sha256 != source_sha
                or runner.proxy_source_sha256 != proxy_source_sha
                or runner.compiler_lock_sha256 != parent.quality_kernel.dependency_lock_sha256
                or runner.sandbox_policy_sha256 != sandbox_policy.policy_sha256
                or runner.task_endpoint_identity_sha256 != task_identity.binding_sha256
                or runner.reflection_endpoint_identity_sha256 != reflection_identity.binding_sha256
                or patch.parent_program_sha256 != parent.program_sha256
                or patch.parent_state_sha256 != parent.state_sha256
                or patch.eligible_demo_bank_root_sha256 != eligible_demo_bank.eligible_demo_bank_root_sha256
                or proxy_execution.grant_sha256 != proxy_grant.grant_sha256
                or proxy_execution.task_model_calls != runner.task_model_calls
                or proxy_execution.reflection_model_calls != runner.reflection_model_calls
                or proxy_execution.actual_cost_microusd != runner.actual_cost_microusd
                or proxy_execution.reserved_cost_microusd > budget.max_cost_microusd
                or proxy_execution.error_codes
                or len(proxy_execution.calls) != runner.task_model_calls + runner.reflection_model_calls
                or any(
                    not call.provider_invoked
                    or call.error_code is not None
                    or call.total_tokens <= 0
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
            provenance = OptimizerCompileProvenanceV2(
                development_dataset_sha=bundle.corpus.development_dataset_sha,
                learning_epoch="program_v5",
                learning_epoch_started_at_ms=bundle.corpus.learning_epoch_started_at_ms,
                projection_schema_id=bundle.corpus.projection_schema_id,
                metric_sha256=canonical_sha(runner.metric),
                optimizer_config_sha256=canonical_sha(optimizer_payload),
                seed=budget.seed,
                max_metric_calls=budget.max_metric_calls,
                max_task_model_calls=budget.max_task_model_calls,
                max_cost_microusd=budget.max_cost_microusd,
                max_call_cost_microusd=budget.max_call_cost_microusd,
                metric_calls=runner.metric_calls,
                task_model_calls=runner.task_model_calls,
                reflection_model_calls=runner.reflection_model_calls,
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
                task_endpoint_identity_sha256=task_identity.binding_sha256,
                reflection_endpoint_identity_sha256=reflection_identity.binding_sha256,
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
                "hypothesis": "Repair the accepted program_v5 failure clusters with bounded DSPy GEPA.",
                "target_dimensions": list(runner.target_dimensions),
                "failure_cluster_ids": list(runner.failure_cluster_ids),
                "guardrails": [
                    "fixed_factory_v2",
                    "development_only",
                    "holdout_unseen",
                    "no_dynamic_code",
                    "no_auto_promotion",
                ],
                "generator_kind": "model",
                "generator_prompt_sha": provenance.metric_sha256,
                "generator_model_sha": canonical_sha(
                    {
                        "task": task_identity.binding_sha256,
                        "reflection": reflection_identity.binding_sha256,
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
                OptimizerCompileProvenanceV2,
                ProgramMachineDiffV2,
                validate_compile_receipt_chain_v2,
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
                machine_diff = ProgramMachineDiffV2.model_validate(program_machine_diff(parent_artifact, artifact))
                provided_diff = ProgramMachineDiffV2.model_validate(spec.get("program_machine_diff"))
                if provided_diff.model_dump(mode="json") != machine_diff.model_dump(mode="json"):
                    raise ValueError("news_learning_program_machine_diff_mismatch")
                manifest_provenance = optimizer_provenance_from_artifact(artifact)
                compile_provenance = OptimizerCompileProvenanceV2.model_validate(spec.get("compile_provenance"))
                if compile_provenance.model_dump(mode="json") != manifest_provenance.model_dump(mode="json"):
                    raise ValueError("news_learning_program_compile_provenance_mismatch")
                if str(args.development) != compile_provenance.development_dataset_sha:
                    raise ValueError("news_learning_program_development_dataset_mismatch")
                patch = ProgramPatchV2.model_validate(spec.get("program_patch"))
                chain = CompileReceiptChain.model_validate(spec.get("compile_receipt_chain"))
                validate_compile_receipt_chain_v2(
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


def _handle_learning_baseline(args: Namespace, settings: Any, stable: Any) -> tuple[int, dict[str, Any]]:
    """Score the stable Program offline. Read-only: no sandbox, no tariff, no container, no writes.

    DSPy lives behind `program_baseline`; this layer only reads the corpus and prints the receipt, so the
    architecture boundary that keeps DSPy out of the CLI still holds.
    """

    from tracefold.app.learning_runtime import compose_news_program_runtime
    from tracefold.app.repositories import postgres_connection
    from tracefold.news import CandidateEvaluator, ClosedWindow
    from tracefold.news.agents.program_baseline import (
        build_baseline_cases,
        build_judge,
        build_runtime_lm,
        compile_program_factory,
        run_baseline,
    )
    from tracefold.news.agents.semantic_program import load_program_artifact

    mode = str(args.mode)
    action_source = str(args.action_source) or ("recorded" if mode == "recorded" else "policy")
    if mode == "recorded" and action_source != "recorded":
        # Scoring a retired arm's verdict against today's `decide()` compares two things that never coexisted.
        raise ValueError("news_program_baseline_recorded_mode_requires_recorded_action")
    window = ClosedWindow(from_ms=int(args.from_ms), to_ms=int(args.to_ms))
    with postgres_connection(settings, role="serve") as conn:
        evaluator = CandidateEvaluator(conn, stable=stable, judges={})
        episodes = evaluator.baseline_episodes(window, cohort=not bool(args.all_cohorts), limit=int(args.limit))
    if not episodes:
        return 2, {"ok": False, "error": {"code": "news_program_baseline_no_accepted_reviews_in_window"}}
    artifact = load_program_artifact(stable.program_sha256)
    lm = None
    if mode != "recorded":
        # The production task endpoint and route budget, so a live baseline measures the arm that actually
        # runs rather than some other binding.
        endpoint = compose_news_program_runtime(settings).event_semantics_primary
        lm = build_runtime_lm(
            model_name=endpoint.model_name,
            api_key=endpoint.api_key,
            api_base=endpoint.api_base,
            timeout=float(artifact.execution.route_deadline_seconds),
            max_tokens=max(artifact.route_spec.event_semantics_max_tokens, artifact.route_spec.reader_card_max_tokens),
            model_kwargs=endpoint.model_kwargs,
        )
    judge_model = str(args.semantic_judge).strip()
    judge = None
    if judge_model:
        # The judge belongs to the metric, not the Program, so it gets its own endpoint rather than the task
        # route: the compiler reflection endpoint when configured, otherwise the Triage fallback.
        reflection = getattr(settings.llm, "news_compiler_reflection", None)
        source = reflection if reflection is not None and reflection.configured else settings.llm.news_triage_fallback
        if not source.configured:
            raise ValueError("news_program_baseline_judge_endpoint_not_configured")
        judge = build_judge(model_name=judge_model, api_key=source.api_key, api_base=source.base_url)
    report = run_baseline(
        build_baseline_cases(episodes, action_source=action_source),
        mode=mode,
        artifact=artifact,
        program_factory=compile_program_factory if mode != "recorded" else None,
        lm=lm,
        judge=judge,
        runtime_identity={"model": getattr(lm, "model", None)} if lm else {},
    )
    payload = report.model_dump(mode="json")
    payload["report_sha256"] = report.report_sha256
    if str(args.out):
        _write_json(str(args.out), payload)
    summary = {key: value for key, value in payload.items() if key != "cases"}
    summary["cases_written_to"] = str(args.out) or None
    return 0, {"ok": True, "data": summary}


def _handle_learning_draft_reviews(args: Namespace, settings: Any, stable: Any) -> tuple[int, dict[str, Any]]:
    """Propose `news_review_v3` rubrics with gold. Read-only: the output is a file, never a review.

    `ReviewDesk.submit` appends an acceptance row unconditionally, so a draft written through that path would
    be accepted release evidence the instant it landed. The human stays the acceptance authority; this only
    turns "compose a judgment from scratch" into "confirm or reject one".
    """

    from tracefold.app.repositories import postgres_connection
    from tracefold.news import CandidateEvaluator, ClosedWindow, canonical_json
    from tracefold.news.agents.program_baseline import build_baseline_cases, build_judge
    from tracefold.news.agents.program_review_drafter import ReviewDrafter, build_draft_batch
    from tracefold.news.agents.semantic_program import render_model_evidence_json

    reflection = getattr(settings.llm, "news_compiler_reflection", None)
    source = reflection if reflection is not None and reflection.configured else settings.llm.news_triage_fallback
    if not source.configured:
        raise ValueError("news_review_drafter_endpoint_not_configured")

    window = ClosedWindow(from_ms=int(args.from_ms), to_ms=int(args.to_ms))
    with postgres_connection(settings, role="serve") as conn:
        evaluator = CandidateEvaluator(conn, stable=stable, judges={})
        episodes = evaluator.baseline_episodes(window, cohort=False, limit=int(args.limit))
    if not episodes:
        return 2, {"ok": False, "error": {"code": "news_review_drafter_no_events_in_window"}}

    cases = build_baseline_cases(episodes, action_source="recorded")
    tasks: list[dict[str, Any]] = []
    for case, raw in zip(cases, episodes, strict=True):
        episode = case.episode
        if bool(args.skip_reviewed) and episode.accepted_review.get("should_push"):
            continue
        verdict = dict(episode.production_verdict or {})
        tasks.append(
            {
                # The task identity a human needs for `review submit`; `evidence_version` comes from the
                # snapshot the episode was projected from.
                "task_id": f"evt.{raw.get('event_id') or ''}.{episode.context.event.evidence_version}",
                "task_version": str(episode.context.event.evidence_sha256 or ""),
                "event_id": str(raw.get("event_id") or ""),
                "headline_zh": str(verdict.get("headline_zh") or ""),
                "evidence_json": render_model_evidence_json(
                    episode.context.event_semantics_payload(), predictor="event_semantics"
                ),
                "card_json": canonical_json(verdict),
                "told_json": canonical_json(list(episode.policy_metric.get("told") or ())),
            }
        )
    if not tasks:
        return 2, {"ok": False, "error": {"code": "news_review_drafter_nothing_to_draft"}}

    # Same endpoint plumbing as the judge: a drafting model is a metric-side tool, not a Program route.
    batch = build_draft_batch(
        ReviewDrafter(
            build_judge(model_name=str(args.model), api_key=source.api_key, api_base=source.base_url).lm
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
            "drafted": len(drafted),
            "with_gold": len(with_gold),
            "failed": len(batch.drafts) - len(drafted),
            "note": "proposals only — a human must accept each one through `tracefold news review submit`",
        },
    }


def _load_candidate_bundle(path: str) -> tuple[Any | None, dict[str, str]]:
    if not path:
        return None, {}
    from tracefold.news import CandidateManifest

    document = _read_json_or_yaml(path)
    candidate = CandidateManifest.model_validate(document.get("candidate") or document)
    artifacts = {str(key): str(value) for key, value in dict(document.get("program_artifacts") or {}).items()}
    return candidate, artifacts


def _learning_program_judges(
    conn: Any,
    *,
    settings: Any,
    stable: Any,
    candidate: Any,
    artifact_paths: Mapping[str, str],
    live: bool,
) -> dict[tuple[str, str], Any]:
    from tracefold.news.agents.semantic_program import (
        DspyNewsSemanticProgram,
        RecordReplayPredictorAdapter,
    )

    arm_artifacts = _learning_program_arm_artifacts(
        stable=stable,
        candidate=candidate,
        artifact_paths=artifact_paths,
    )
    if live:
        return {
            (arm_name, arm.bundle_sha): _configured_program_judge(settings, artifact)
            for arm_name, arm, artifact in arm_artifacts
        }
    rows = conn.execute(
        "SELECT DISTINCT ON (request_sha256) request_sha256, request, response "
        "FROM news_model_recordings WHERE response IS NOT NULL "
        "AND request ? 'program_sha256' AND request ? 'runtime_binding_sha256' "
        "ORDER BY request_sha256, created_at_ms DESC"
    ).fetchall()
    recordings_by_program: dict[str, dict[str, Any]] = {}
    for row in rows:
        recorded_request = dict(row["request"] or {})
        program_sha = str(recorded_request.get("program_sha256") or "")
        if not program_sha:
            raise ValueError("news_learning_recording_program_identity_missing")
        recordings_by_program.setdefault(program_sha, {})[str(row["request_sha256"])] = {
            "request": recorded_request,
            "response": row["response"],
        }
    judges: dict[tuple[str, str], Any] = {}
    for arm_name, arm, artifact in arm_artifacts:
        replay = RecordReplayPredictorAdapter(recordings_by_program.get(arm.program_sha256, {}))
        judges[(arm_name, arm.bundle_sha)] = DspyNewsSemanticProgram(
            artifact,
            primary_adapter=replay,
            fallback_adapter=replay,
        )
    return judges


def _learning_recording_replay_capability(
    conn: Any,
    *,
    stable: Any,
    candidate: Any,
    artifact_paths: Mapping[str, str],
    run_sha: str,
) -> Any:
    from tracefold.news import ReplayArmSpec, load_recording_replay_capability

    arm_artifacts = _learning_program_arm_artifacts(
        stable=stable,
        candidate=candidate,
        artifact_paths=artifact_paths,
    )
    return load_recording_replay_capability(
        conn,
        run_sha=run_sha,
        arms=tuple(
            ReplayArmSpec(arm=arm_name, bundle_sha=arm.bundle_sha, artifact=artifact)
            for arm_name, arm, artifact in arm_artifacts
        ),
    )


def _learning_program_arm_artifacts(
    *,
    stable: Any,
    candidate: Any,
    artifact_paths: Mapping[str, str],
) -> tuple[tuple[Literal["stable", "candidate"], Any, Any], ...]:
    from tracefold.news.agents.semantic_program import (
        ProgramArtifactCodec,
        load_program_artifact,
        load_stable_program_artifact,
    )

    stable_artifact = load_stable_program_artifact()
    if stable_artifact.program_sha256 != stable.program_sha256:
        raise ValueError("news_learning_stable_program_mismatch")
    candidate_arm = candidate.candidate_arm
    candidate_sha = candidate_arm.program_sha256
    candidate_artifact = stable_artifact
    if candidate_sha != stable_artifact.program_sha256:
        path = artifact_paths.get(candidate_sha)
        candidate_artifact = ProgramArtifactCodec.load(path) if path else load_program_artifact(candidate_sha)
    if candidate_artifact.program_sha256 != candidate_sha:
        raise ValueError("news_learning_candidate_program_mismatch")
    return (
        ("stable", stable, stable_artifact),
        ("candidate", candidate_arm, candidate_artifact),
    )


def _configured_program_judge(settings: Any, artifact: Any) -> Any:
    from tracefold.app.learning_runtime import compose_news_program_runtime

    program = compose_news_program_runtime(settings).semantic_judge(artifact)
    if program is None:
        raise ValueError("news_learning_live_program_not_configured")
    return program


def _insert_learning_artifact(
    conn: Any,
    *,
    kind: str,
    payload: Mapping[str, Any],
    parent_sha: str | None,
    created_at_ms: int,
) -> str:
    public = json.loads(json.dumps(dict(payload), ensure_ascii=False, sort_keys=True, default=str))
    from tracefold.news import canonical_sha

    artifact_sha = canonical_sha({"kind": kind, "payload": public})
    conn.execute(
        "INSERT INTO news_learning_artifacts "
        "(artifact_sha, kind, parent_sha, payload, created_by, created_at_ms) "
        "VALUES (%s, %s, %s, %s::jsonb, 'learning_propose', %s) "
        "ON CONFLICT (artifact_sha) DO NOTHING",
        (artifact_sha, kind, parent_sha, json.dumps(public, ensure_ascii=False, sort_keys=True), created_at_ms),
    )
    row = conn.execute(
        "SELECT kind, payload FROM news_learning_artifacts WHERE artifact_sha = %s",
        (artifact_sha,),
    ).fetchone()
    if row is None or str(row["kind"]) != kind or dict(row["payload"] or {}) != public:
        raise ValueError("news_learning_artifact_collision")
    return artifact_sha


def _handle_why(args: Namespace) -> tuple[int, dict[str, Any]]:
    from tracefold.app.repositories import repositories
    from tracefold.news.eval.why import explain_event

    settings = load_settings(require_ws_token=False)
    with repositories(settings) as repos:
        report = explain_event(repos, str(args.event_id))
    if report is None:
        return 1, {"ok": False, "error": "news_event_not_found"}
    return 0, {"ok": True, "data": report}


def _read_json_or_yaml(path: str) -> dict[str, Any]:
    """JSON first, YAML second.

    A frozen corpus is one line of JSON and can be megabytes; PyYAML is orders of magnitude slower on it, and
    YAML 1.1 does not resolve exponent-form floats without a decimal point — `1e-05` comes back as the *string*
    `"1e-05"`, which then fails the corpus hash check for no visible reason. A hand-written candidate file is
    still allowed to be YAML.
    """

    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    try:
        document = json.loads(text)
    except ValueError:
        import yaml

        document = yaml.safe_load(text)
    if not isinstance(document, dict):
        raise ValueError(f"news_document_not_a_mapping:{path}")
    return document


def _canonical_model_document(document: str, model_type: Any, *, code: str) -> Any:
    from tracefold.news import canonical_json

    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        for key, value in pairs:
            if key in payload:
                raise ValueError(f"duplicate_key:{key}")
            payload[key] = value
        return payload

    try:
        payload = json.loads(
            document,
            object_pairs_hook=reject_duplicate_keys,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
        )
        parsed = model_type.model_validate(payload)
    except (TypeError, ValueError) as exc:
        raise ValueError(code) from exc
    if document != canonical_json(parsed.model_dump(mode="json")):
        raise ValueError(code)
    return parsed


def _write_json(path: str, payload: Mapping[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _handle_replay(args: Namespace) -> tuple[int, dict[str, Any]]:
    from tracefold.app.repositories import repositories
    from tracefold.news.eval.replay import replay_hits

    settings = load_settings(require_ws_token=False)
    # The Gate reads the instrument universe (#89), so a replay without it measures the fallback, not the deployed
    # behaviour. The database stays optional — this command is also the offline tuning tool — but never silently:
    # `instruments_error` says why the map is missing.
    classes: Mapping[str, str] | None = None
    instruments_error: str | None = None
    if not args.no_instruments:
        try:
            with repositories(settings) as repos:
                classes = repos.instruments.instrument_classes() or None
        except Exception as exc:  # a replay must not need a database to run
            instruments_error = type(exc).__name__
    with open(args.path, encoding="utf-8") as fh:
        raw = json.load(fh)
    hits: list[Mapping[str, Any]] = []
    if isinstance(raw, Mapping):
        for value in raw.values():
            hits.extend(h for h in value if isinstance(h, Mapping))
    elif isinstance(raw, list):
        hits.extend(h for h in raw if isinstance(h, Mapping))
    report = replay_hits(
        hits,
        watchlist_symbols=settings.news.watchlist_symbols,
        suppress_low_signal=(
            settings.news.gate.suppress_low_signal if args.gate_policy == "config" else args.gate_policy == "strict"
        ),
        instrument_classes=classes,
    )
    if instruments_error:
        report["instruments_error"] = instruments_error
    return 0, {"ok": True, "data": report}


def _handle_dlq(args: Namespace) -> tuple[int, dict[str, Any]]:
    settings = load_settings(require_ws_token=False)

    async def _run() -> dict[str, Any]:
        bus = _bus(settings)
        try:
            await bus.connect()
            if args.dlq_action == "inspect":
                return {"messages": await bus.dead_letters(limit=int(args.limit))}
            if args.dlq_action == "replay":
                return {"replayed": await bus.replay_dead_letters(limit=int(args.limit))}
            return {"purged": await bus.purge_dead_letters()}
        finally:
            await bus.close()

    try:
        result = asyncio.run(_run())
    except Exception as exc:
        return 1, {"ok": False, "error": type(exc).__name__, "detail": str(exc)[:200]}
    return 0, {"ok": True, "data": result}


__all__ = ["handle_news"]
