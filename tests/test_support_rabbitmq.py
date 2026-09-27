"""The broker-test harness never guesses a management API for a non-default AMQP port."""

from __future__ import annotations

import pytest

from tests.support.rabbitmq import declared_amqp_url, declared_management_url, rabbitmq_management_url


def test_an_explicit_management_url_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRACEFOLD_TEST_RABBITMQ_MANAGEMENT_URL", "http://172.17.0.2:15672/")
    assert rabbitmq_management_url("amqp://u:p@127.0.0.1:45672/") == "http://172.17.0.2:15672"


def test_the_default_amqp_port_pairs_with_its_default_management_port(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRACEFOLD_TEST_RABBITMQ_MANAGEMENT_URL", raising=False)
    assert rabbitmq_management_url("amqp://u:p@rabbit:5672/") == "http://rabbit:15672"
    assert rabbitmq_management_url("amqp://u:p@rabbit/") == "http://rabbit:15672"


def test_a_disposable_broker_on_another_port_must_name_its_management_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRACEFOLD_TEST_RABBITMQ_MANAGEMENT_URL", raising=False)
    with pytest.raises(RuntimeError, match="TRACEFOLD_TEST_RABBITMQ_MANAGEMENT_URL is required"):
        rabbitmq_management_url("amqp://u:p@127.0.0.1:45672/")


def test_no_broker_is_declared_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRACEFOLD_TEST_AMQP_URL", raising=False)
    monkeypatch.delenv("TRACEFOLD_TEST_RABBITMQ_MANAGEMENT_URL", raising=False)
    # Undeclared means no broker at all, never the local 5672 / 15672 of the operator's deployment.
    assert declared_amqp_url() == ""
    assert declared_management_url(declared_amqp_url()) == ""
