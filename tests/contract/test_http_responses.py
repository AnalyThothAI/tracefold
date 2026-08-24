import json
import math
from decimal import Decimal

from tracefold.app.http.responses import _json


def test_api_json_response_encodes_decimal_payloads() -> None:
    response = _json({"ok": True, "data": {"price": Decimal("1.23")}})
    assert json.loads(response.body) == {"ok": True, "data": {"price": 1.23}}


def test_api_json_response_replaces_non_finite_float_payloads_with_null() -> None:
    response = _json(
        {
            "ok": True,
            "data": {
                "score": math.nan,
                "nested": [{"value": math.inf}, {"value": -math.inf}, {"value": 1.0}],
            },
        }
    )
    assert json.loads(response.body) == {
        "ok": True,
        "data": {"score": None, "nested": [{"value": None}, {"value": None}, {"value": 1.0}]},
    }
