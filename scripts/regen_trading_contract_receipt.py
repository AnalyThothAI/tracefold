"""Generate or verify the sealed Production V3 execution-policy receipt."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from tracefold.trading.contract_receipt import build_execution_policy_contract_receipt

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "docs/generated/execution-policy-contract-v4.json"


def rendered_receipt() -> str:
    receipt = build_execution_policy_contract_receipt()
    return json.dumps(receipt.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    expected = rendered_receipt()
    if args.check:
        try:
            observed = TARGET.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise SystemExit("execution_policy_contract_receipt_missing") from None
        if observed != expected:
            raise SystemExit("execution_policy_contract_receipt_drift")
        return 0
    TARGET.write_text(expected, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
