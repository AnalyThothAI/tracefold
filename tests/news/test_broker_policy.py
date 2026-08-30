"""The frozen RabbitMQ retry contract (#400), checked without a broker.

These are the statements the real-broker tests cannot make on their own: that the checked-in definitions
document is exactly what the code generates, that the byte bounds are the documented formula applied to
the recorded measurements rather than hand-picked numbers, and that no source file can quietly bring the
deleted TTL retry lane back.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tracefold.news import broker_policy
from tracefold.news.bus import Q_DEAD, Q_DELIVER, Q_RAW, Q_TRIAGE

REPO = Path(__file__).resolve().parents[2]
DEFINITIONS = REPO / "docker" / "rabbitmq" / "definitions.json"


def test_checked_in_definitions_match_the_generated_document() -> None:
    assert DEFINITIONS.read_text(encoding="utf-8") == broker_policy.definitions_json()


def test_three_total_transient_attempts_is_the_delivery_limit_plus_one() -> None:
    """RabbitMQ 4.3.5 delivers `delivery-limit + 1` times: the first delivery has no failure counter.

    The relationship is measured, not assumed, in `tests/integration/test_news_bus_rabbitmq.py`; keeping
    it as one expression here means an edit to the frozen attempt budget cannot silently change the
    limit by one in either direction.
    """

    assert broker_policy.TOTAL_TRANSIENT_ATTEMPTS == 3
    assert broker_policy.TRANSIENT_DELIVERY_LIMIT == broker_policy.TOTAL_TRANSIENT_ATTEMPTS - 1


def test_every_business_queue_carries_the_whole_retry_contract() -> None:
    definitions = broker_policy.expected_effective_definitions()
    for queue in (Q_RAW, Q_TRIAGE, Q_DELIVER):
        assert definitions[queue] == {
            "delayed-retry-type": "all",
            "delayed-retry-min": 30_000,
            "delayed-retry-max": 30_000,
            "delivery-limit": 2,
            "dead-letter-strategy": "at-least-once",
            "dead-letter-exchange": "news.dlx",
            "overflow": "reject-publish",
            "max-length-bytes": broker_policy.MAX_LENGTH_BYTES[queue],
        }
    # The dead-letter queue is terminal: it must not dead-letter onward or delay anything, but it keeps
    # reject-publish so a full DLQ holds the source message instead of dropping evidence.
    assert definitions[Q_DEAD] == {
        "overflow": "reject-publish",
        "max-length-bytes": broker_policy.MAX_LENGTH_BYTES[Q_DEAD],
    }


def test_byte_bounds_are_the_documented_formula_over_the_recorded_measurements() -> None:
    for queue in (Q_RAW, Q_TRIAGE, Q_DELIVER):
        computed = (
            broker_policy.P99_ENVELOPE_BYTES[queue]
            * broker_policy.PEAK_MESSAGES_PER_MINUTE[queue]
            * broker_policy.BACKLOG_MINUTES
        )
        bound = broker_policy.MAX_LENGTH_BYTES[queue]
        assert bound >= computed
        assert bound >= broker_policy.MIN_QUEUE_BYTES
        assert bound % broker_policy.MIB == 0
        assert bound == broker_policy.max_length_bytes(queue)
    assert broker_policy.MAX_LENGTH_BYTES[Q_DEAD] >= (
        broker_policy.P99_ENVELOPE_BYTES[Q_DEAD] * broker_policy.DEAD_LETTER_EVIDENCE_MESSAGES
    )


def test_the_total_byte_bound_stays_under_the_broker_budget() -> None:
    """The per-queue bound must reject first, before the node-wide memory alarm blocks every publisher."""

    assert sum(broker_policy.MAX_LENGTH_BYTES.values()) <= broker_policy.BROKER_BYTE_BUDGET


def test_one_policy_per_queue_and_no_pattern_overlap() -> None:
    for prefix in ("", "tf_test_abcd1234"):
        policies = broker_policy.policies(name_prefix=prefix)
        assert len(policies) == 4
        for policy in policies:
            matched = [other for other in policies if re.fullmatch(policy.pattern, other.queue)]
            assert matched == [policy], f"{policy.pattern} matched {[m.queue for m in matched]}"


def test_a_prefixed_topology_never_matches_the_production_policies() -> None:
    """Integration tests run their own prefixed topology; RabbitMQ applies exactly one policy per queue."""

    production = broker_policy.policies()
    for policy in broker_policy.policies(name_prefix="tf_test_abcd1234"):
        assert not any(re.fullmatch(other.pattern, policy.queue) for other in production)
        assert not any(other.name == policy.name for other in production)


def test_the_definitions_document_carries_only_policies() -> None:
    """Importing it must not create, replace or drop users, vhosts, permissions, exchanges or queues."""

    document = json.loads(DEFINITIONS.read_text(encoding="utf-8"))
    assert set(document) == {"rabbit_version", "policies"}
    assert document["rabbit_version"] == broker_policy.RABBIT_VERSION


_CUT_SYMBOLS = (
    "Q_RETRY",
    "RETRY_EXCHANGE",
    "RETRY_TTL_MS",
    "MAX_TRANSIENT_ATTEMPTS",
    "_publish_retry_lane",
    "publish_retry",
    "publish_defer",
    "x-news-attempt",
    # Workstream B: the process-memory copy of durable incident state.
    "_circuit_incident_open",
)


@pytest.mark.parametrize("symbol", _CUT_SYMBOLS)
def test_no_source_file_can_rebuild_the_deleted_retry_lane(symbol: str) -> None:
    """The hard cut is only real if nothing in `tracefold/` still names the lane it deleted.

    `news.retry` itself is exempt from a blanket ban: the adapter has to know the name it must never
    declare, and the cutover runbook has to tell an operator which queue to delete by hand.
    """

    pattern = re.compile(rf"\b{re.escape(symbol)}\b")
    offenders = [
        path.relative_to(REPO).as_posix()
        for path in (REPO / "tracefold").rglob("*.py")
        if pattern.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []


def test_the_application_never_publishes_to_the_cut_retry_lane() -> None:
    from tracefold.integrations import rabbitmq

    source = (REPO / "tracefold" / "integrations" / "rabbitmq.py").read_text(encoding="utf-8")
    # The cut lane appears exactly once in the adapter: the constant that names what must never be
    # declared. Everything else refers to that constant.
    assert source.count('"news.retry"') == 1
    assert rabbitmq.REMOVED_RETRY_LANE == "news.retry"
    declared = {spec.name for spec in rabbitmq.topology().queues}
    assert declared == {Q_RAW, Q_TRIAGE, Q_DELIVER, Q_DEAD}
    assert rabbitmq.topology().exchange_names == ("news", "news.dlx")


def test_the_triage_circuit_has_no_process_memory_branch_over_durable_incident_state() -> None:
    """#400 Workstream B: nothing may decide a PostgreSQL incident transition from a remembered edge.

    The two writes have to be reachable from state that a retried transaction can re-derive, and neither
    may be wrapped in a suppression that turns a failed durable write into a silent success.
    """

    import inspect

    from tracefold.news.pipeline import triage

    # Startup reconciliation must fail the Workers root, so it cannot sit inside a suppression.
    run_source = inspect.getsource(triage.TriageConsumer.run)
    assert "news_triage_circuit_reconcile" in run_source
    assert "suppress" not in run_source

    # Exactly one place writes the incident, and the persist transaction is what calls it.
    source = inspect.getsource(triage)
    assert source.count("open_incident(") == 1
    assert source.count("close_open_incidents(") == 2  # the persist transition and startup reconciliation
    assert "_apply_circuit_incident(repos, s.circuit_incident" in source
    # The transition is derived, never remembered: no attribute on the consumer holds incident state.
    assert not [name for name in vars(triage.TriageConsumer) if "incident" in name]


def test_declared_queue_arguments_carry_nothing_the_policy_owns() -> None:
    """A stale queue argument must not be able to mask a missing policy.

    RabbitMQ 4.3 lets a policy override these arguments, so leaving them on the queue would not change
    today's behaviour — but it would silently restore the old delivery limit, at-most-once dead lettering
    and message-count bound the moment the policy were removed.
    """

    from tracefold.integrations.rabbitmq import topology

    policy_owned = {
        "x-dead-letter-exchange",
        "x-dead-letter-strategy",
        "x-delivery-limit",
        "x-max-length",
        "x-max-length-bytes",
        "x-message-ttl",
        "x-overflow",
    }
    for spec in topology().queues:
        overlap = policy_owned & set(spec.arguments)
        if spec.name == Q_DEAD:
            # The dead-letter queue's own delivery limit preserves evidence, it does not configure
            # retry: `news.dead` is terminal and has nowhere left to dead-letter to.
            assert overlap == {"x-delivery-limit"}
            assert spec.arguments["x-delivery-limit"] == broker_policy.DEAD_LETTER_DELIVERY_LIMIT
            continue
        assert overlap == set(), f"{spec.name} declares {sorted(overlap)}"
