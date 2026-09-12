#!/usr/bin/env python3
"""Offline #641 config hard cut. Never invoked by the runtime loader."""

from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from tracefold.platform.config.models import Settings

RETIRED_RULES = frozenset(
    {
        "exit_notifications_enabled",
        "buy_min_usd",
        "buy_window_s",
        "exit_ratio_bps",
        "exit_min_position_usd",
        "exit_cascade_window_s",
        "exit_cascade_min_usd",
        "crowding_n",
        "crowding_window_s",
        "crowding_min_usd",
        "crowding_premium_late_bps",
    }
)
DEFAULTS = {"net_buy_fast_n": 3, "net_buy_slow_n": 5, "min_net_buy_usd": 1000, "trigger_max_age_s": 60}


def migrate(data: dict[str, Any], *, new_rules: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    result = copy.deepcopy(data)
    news = result.setdefault("news", {})
    if not isinstance(news, dict):
        raise ValueError("news_not_mapping")
    tape = news.setdefault("chain_tape", {})
    if not isinstance(tape, dict):
        raise ValueError("chain_tape_not_mapping")
    rules = tape.setdefault("rules", {})
    if not isinstance(rules, dict):
        raise ValueError("rules_not_mapping")
    retired = sorted(RETIRED_RULES.intersection(rules))
    is_old = bool(retired) or "digest" in tape
    for key in retired:
        del rules[key]
    removed = ["news.chain_tape.rules." + key for key in retired]
    if "digest" in tape:
        del tape["digest"]
        removed.append("news.chain_tape.digest")
    # The old age default was 600s. This is an explicit offline reset, never a runtime fallback.
    if is_old and "trigger_max_age_s" in rules:
        del rules["trigger_max_age_s"]
        removed.append("news.chain_tape.rules.trigger_max_age_s (reset)")
    for key, value in DEFAULTS.items():
        rules.setdefault(key, value)
    rules.update(new_rules)
    Settings.model_validate(result)
    return result, removed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--output", type=Path, help="Write a separate file; omission is a redacted dry run.")
    parser.add_argument("--fast-n", type=int)
    parser.add_argument("--slow-n", type=int)
    parser.add_argument("--min-net-buy-usd", type=str)
    parser.add_argument("--trigger-max-age-s", type=int)
    args = parser.parse_args()
    try:
        if args.config.is_symlink() or not args.config.is_file():
            raise ValueError("config_not_regular_file")
        data = yaml.safe_load(args.config.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("config_not_mapping")
        overrides = {
            key: value
            for key, value in {
                "net_buy_fast_n": args.fast_n,
                "net_buy_slow_n": args.slow_n,
                "min_net_buy_usd": args.min_net_buy_usd,
                "trigger_max_age_s": args.trigger_max_age_s,
            }.items()
            if value is not None
        }
        migrated, removed = migrate(data, new_rules=overrides)
        output_state = "dry_run"
        if args.output is not None:
            destination = args.output.resolve()
            if destination == args.config.resolve() or args.output.is_symlink():
                raise ValueError("output_must_be_separate_regular_file")
            encoded = yaml.safe_dump(migrated, allow_unicode=True, sort_keys=False).encode("utf-8")
            if destination.exists():
                if destination.read_bytes() != encoded:
                    raise ValueError("output_exists_with_different_contents")
                output_state = "unchanged"
            else:
                descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, "wb") as target:
                    target.write(encoded)
                    target.flush()
                    os.fsync(target.fileno())
                output_state = "written"
        print(
            json.dumps(
                {
                    "state": output_state,
                    "removed_keys": removed,
                    "new_rules": migrated["news"]["chain_tape"]["rules"],
                    "switches": "preserved; no activation or deployment performed",
                },
                ensure_ascii=False,
            )
        )
        return 0
    except ValidationError as error:
        # Pydantic's default exception includes input values, potentially credentials.
        print(
            json.dumps(
                {
                    "error": "config_validation_failed",
                    "fields": [
                        ".".join(map(str, item["loc"]))
                        for item in error.errors(include_input=False, include_context=False)
                    ],
                }
            )
        )
        return 2
    except (ValueError, OSError, yaml.YAMLError) as error:
        print(json.dumps({"error": "config_migration_failed", "type": type(error).__name__}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
