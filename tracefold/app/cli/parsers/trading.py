from __future__ import annotations

import argparse

from tracefold.app.cli.parsers.common import _nonnegative_int, _positive_int


def add_trading_commands(
    subcommands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    trading = subcommands.add_parser("trading", help="Trading Case -> Intent -> Outcome and safety controls")
    trading_subcommands = trading.add_subparsers(dest="trading_command", required=True)
    trading_subcommands.add_parser("status", help="Decision, Capital, binding facts, and durable outcomes")
    trading_cases = trading_subcommands.add_parser("cases", help="list Trading cases newest first")
    trading_cases.add_argument(
        "--state",
        choices=("PENDING", "RUNNING", "NO_TRADE", "POLICY_REJECTED", "INTENT_EMITTED", "BLOCKED"),
        default=None,
    )
    trading_cases.add_argument("--limit", type=_positive_int, default=20)
    trading_replay = trading_subcommands.add_parser(
        "replay-oi",
        help="source-native BAR replay with an audited artifact and immutable receipt (#286)",
    )
    trading_replay.add_argument(
        "--days",
        type=_positive_int,
        default=7,
        help="how far back to replay; the OI ledger holds 30 days of parsed frames",
    )
    trading_replay.add_argument(
        "--strategy",
        choices=("source_native_oi_smart_money_long_v3",),
        default="source_native_oi_smart_money_long_v3",
        help="the one production capital policy; a replay may only run the identity the lane runs",
    )
    trading_replay.add_argument(
        "--venues",
        default="binance.perp,hl.perp",
        help="comma-separated exact source-native venue scenarios",
    )
    trading_replay.add_argument("--fidelity", choices=("bar_v1",), default="bar_v1")
    trading_replay.add_argument("--out", default="artifacts/trading-replay", help="artifact root")
    trading_evidence = trading_subcommands.add_parser(
        "evidence",
        help="capture, seal, preregister, unblind, and verify the Production V3 evidence clock",
    )
    evidence_subcommands = trading_evidence.add_subparsers(dest="evidence_command", required=True)
    evidence_capture = evidence_subcommands.add_parser(
        "capture", help="freeze a point-in-time discovery or protocol-locked future source partition"
    )
    evidence_capture.add_argument("--partition", choices=("discovery", "future"), required=True)
    evidence_capture.add_argument("--start-ms", type=_nonnegative_int, required=True)
    evidence_capture.add_argument("--end-ms", type=_positive_int, required=True)
    evidence_capture.add_argument("--candidate", default="")
    evidence_capture.add_argument("--candidate-receipt", default="")
    evidence_capture.add_argument("--out", required=True, help="content-addressed evidence artifact root")
    evidence_drain = evidence_subcommands.add_parser(
        "drain", help="freeze bars and funding only after the capture horizon can be finalized"
    )
    evidence_drain.add_argument("--capture", required=True)
    evidence_drain.add_argument("--candidate", default="")
    evidence_drain.add_argument("--candidate-receipt", default="")
    evidence_drain.add_argument("--max-horizon-ms", type=_positive_int, default=None)
    evidence_drain.add_argument("--finalization-lag-ms", type=_nonnegative_int, default=None)
    evidence_drain.add_argument("--cost-model", default="")
    evidence_drain.add_argument("--out", required=True, help="content-addressed evidence artifact root")
    evidence_seal = evidence_subcommands.add_parser(
        "corpus-seal", help="deterministically seal a discovery capture and drain; zero provider I/O"
    )
    evidence_seal.add_argument("--capture", required=True)
    evidence_seal.add_argument("--drain", required=True)
    evidence_seal.add_argument("--out", required=True, help="content-addressed evidence artifact root")
    evidence_register = evidence_subcommands.add_parser(
        "candidate-register", help="durably register one candidate or NO_CANDIDATE before a future window"
    )
    evidence_register.add_argument("--file", required=True)
    evidence_register.add_argument("--out", required=True, help="content-addressed evidence artifact root")
    evidence_release_register = evidence_subcommands.add_parser(
        "release-register",
        help="bind an approved exact release and fixed window to the current Workers/Serve generations",
    )
    evidence_release_register.add_argument("--file", required=True, help="approved release candidate YAML/JSON")
    evidence_unblind = evidence_subcommands.add_parser(
        "future-unblind", help="evaluate one protocol-locked future partition after its fixed drain cutoff"
    )
    evidence_unblind.add_argument("--capture", required=True)
    evidence_unblind.add_argument("--drain", required=True)
    evidence_unblind.add_argument("--candidate", required=True)
    evidence_unblind.add_argument("--candidate-receipt", required=True)
    evidence_unblind.add_argument("--out", required=True, help="content-addressed evidence artifact root")
    evidence_verify = evidence_subcommands.add_parser(
        "verify", help="credential-free verification of one evidence chain, lifecycle, release, window, or rollback"
    )
    verification_subject = evidence_verify.add_mutually_exclusive_group(required=True)
    verification_subject.add_argument("--receipt", default="", help="durable evidence receipt SHA")
    verification_subject.add_argument("--case-id", default="", help="single durable Case/Intent lifecycle")
    verification_subject.add_argument("--window", default="", help="fixed seven-day acceptance YAML/JSON")
    verification_subject.add_argument("--release", default="", help="exact approved release candidate YAML/JSON")
    verification_subject.add_argument("--rollback", default="", help="rollback receipt YAML/JSON")
    trading_show = trading_subcommands.add_parser("show", help="one case with its intent and current outcome")
    trading_show.add_argument("case_id")
    trading_blacklist = trading_subcommands.add_parser(
        "blacklist",
        help="the canonical deny-list; one row blocks every provider spelling of that underlying",
    )
    trading_blacklist.add_argument("blacklist_action", choices=("list", "add", "remove"))
    trading_blacklist.add_argument("symbol", nargs="?", default="")
    trading_blacklist.add_argument("--reason", default="operator")
    trading_control = trading_subcommands.add_parser("control", help="set the runtime control state")
    trading_control.add_argument("state", choices=("close-only", "paused"))
    trading_authority = trading_subcommands.add_parser(
        "authority", help="install immutable human authority artifacts and explicitly re-arm"
    )
    authority_subcommands = trading_authority.add_subparsers(dest="authority_command", required=True)
    for action, help_text in (
        ("risk-policy-install", "install one DailyRiskPolicyV1 JSON/YAML artifact"),
        ("grant-install", "install one ProductionPromotionGrantV1 JSON/YAML artifact"),
        ("grant-revoke", "append one ProductionPromotionGrantRevocationV1 JSON/YAML artifact"),
        ("arm-install", "install one OperatorArmReceiptV1 JSON/YAML artifact while paused"),
    ):
        authority_file = authority_subcommands.add_parser(action, help=help_text)
        authority_file.add_argument("--file", required=True)
    authority_activate = authority_subcommands.add_parser(
        "activate", help="atomically activate the exact arm set for every configured binding"
    )
    authority_activate.add_argument(
        "--arm", action="append", required=True, help="arm receipt SHA; repeat once per configured binding"
    )
