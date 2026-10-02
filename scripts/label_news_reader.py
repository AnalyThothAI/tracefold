"""Versioned owner-rule blind annotation and proxy agreement/review reports.

Claude receives source text and randomly ordered sent messages, with tools disabled.
Its labels are fitting proxies, never certified truth. Reporting is local and read-only.
Provider annotation must be explicitly requested with the ``annotate`` subcommand.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from scripts.news_reader_io import dataset_sha256, read_jsonl, write_jsonl
from scripts.news_reader_labeling import ANNOTATION_IDENTITY, GUIDE_VERSION, OWNER_GUIDE
from tracefold.news.notifications.reader import REPORT_KIND_OPTIONS, ReaderInput
from tracefold.news.updates.identity import digest

_ALLOWED = {
    "case_id",
    "claim_ref",
    "statement",
    "quotes",
    "as_of",
    "messages",
    "reader_input",
    "first_available_at_ms",
    "story_id",
    "stratum",
    "inclusion_probability",
    "sampling_design",
    "sampling_frame",
    "sampling_unit",
    "event_id",
    "decision_ref",
    "update_ref",
    "content_revision",
    "decided_at_ms",
    "reader_input_sha256",
    "recorded_input_sha256",
    "reader_input_provenance",
    "source_document_sha256",
    "message_intents",
    "message_payload_sha256",
    "links",
    "receipts",
    "novelty",
    "reader_revision",
    "reader_identity",
    "original_reason",
    "original_decision",
    "reader_applicable",
    "pre_reader_reason",
    "deterministic_decision",
    "decision_band",
    "report_kind_stratum",
    "type_stratum_source",
}
_KINDS = {kind for kind, _ in REPORT_KIND_OPTIONS}
_LABEL_METADATA = (
    "claim_ref",
    "stratum",
    "inclusion_probability",
    "sampling_design",
    "sampling_frame",
    "sampling_unit",
    "reader_applicable",
    "pre_reader_reason",
    "deterministic_decision",
    "original_reason",
)
OWNER_IMPORT_PROTOCOL = "news_reader_owner_blind_import_v1"


def blind_case(
    row: Mapping[str, Any],
    shuffle: Callable[[list[int]], None] | None = None,
) -> tuple[dict[str, Any], dict[str, str]]:
    """Drop extraction fields and metadata; randomize anchors without changing the stored input."""
    if set(row) - _ALLOWED:
        raise ValueError("news_blind_label_input_has_scores_or_readings")
    if "reader_input" in row:
        source = ReaderInput.model_validate(row["reader_input"])
        statement, quotes, as_of, messages = (
            source.claim.statement,
            [item.quote for item in source.sources],
            source.as_of.isoformat(),
            list(source.messages),
        )
        sources = [
            {key: value for key, value in item.model_dump(mode="json").items() if key != "quote" and value is not None}
            for item in source.sources
        ]
    else:
        statement, quotes, as_of, messages = (
            row["statement"],
            row["quotes"],
            row["as_of"],
            list(row.get("messages", [])),
        )
        sources = []
    order = list(range(len(messages)))
    (shuffle or random.SystemRandom().shuffle)(order)
    mapping = {f"m{i + 1}": f"m{original + 1}" for i, original in enumerate(order)}
    payload = {
        "case_id": row["case_id"],
        "statement": statement,
        "quotes": quotes,
        "sources": sources,
        "as_of": as_of,
        "messages": [{"id": f"m{i + 1}", "body": messages[original]} for i, original in enumerate(order)],
    }
    return payload, mapping


def normalize_label(value: Mapping[str, Any], mapping: Mapping[str, str]) -> dict[str, Any]:
    label = dict(value["label"])
    if (
        label.get("kind") not in _KINDS
        or label.get("push") not in {"push", "feed", "borderline"}
        or not isinstance(label.get("key"), bool)
        or label.get("anchor") not in {"none", *mapping}
        or not label.get("note")
        or not value.get("story_id")
    ):
        raise ValueError("news_blind_label_invalid")
    if label["key"] and label["push"] != "push":
        raise ValueError("news_blind_label_key_requires_push")
    label["anchor"] = "none" if label["anchor"] == "none" else mapping[label["anchor"]]
    return {"case_id": value["case_id"], "story_id": value["story_id"], "label": label}


def prepare_owner(
    rows: Sequence[Mapping[str, Any]],
    *,
    shuffle: Callable[[list[int]], None] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Prepare human text only; preserve original inputs and selection privately."""
    cases = [row["case_id"] for row in rows]
    frames = [row.get("sampling_frame") for row in rows]
    if (
        not rows
        or len(set(cases)) != len(cases)
        or not isinstance(frames[0], Mapping)
        or any(frame != frames[0] for frame in frames)
        or set(frames[0].get("selected_case_ids", ())) != set(cases)
        or frames[0].get("selection_frozen_before_labels") is not True
        or frames[0].get("unit") != "independent_story_representative"
        or any(row.get("sampling_unit") != frames[0]["unit"] or "reader_input" not in row for row in rows)
    ):
        raise ValueError("news_owner_blind_frozen_selection_required")
    public, entries = [], []
    for row in rows:
        payload, mapping = blind_case(row, shuffle)
        blind_sha = digest(payload)
        public.append({**payload, "blind_input_sha256": blind_sha})
        entries.append(
            {
                "case_id": row["case_id"],
                "input_sha256": digest(row),
                "reader_input_sha256": digest(row["reader_input"]),
                "blind_input_sha256": blind_sha,
                "anchor_mapping": mapping,
                "metadata": {key: row[key] for key in _LABEL_METADATA if key in row},
            }
        )
    manifest = {
        "protocol": OWNER_IMPORT_PROTOCOL,
        "guide_version": GUIDE_VERSION,
        "annotation_identity": ANNOTATION_IDENTITY,
        "source_dataset_sha256": dataset_sha256(rows),
        "blind_dataset_sha256": dataset_sha256(public),
        "selected_case_ids": cases,
        "cases": entries,
    }
    manifest["manifest_sha256"] = digest(manifest)
    return public, manifest


def import_owner(
    source: Sequence[Mapping[str, Any]],
    human: Sequence[Mapping[str, Any]],
    manifest: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Bind complete human labels to frozen blind material and restore anchors."""
    if (
        manifest.get("protocol") != OWNER_IMPORT_PROTOCOL
        or manifest.get("guide_version") != GUIDE_VERSION
        or manifest.get("annotation_identity") != ANNOTATION_IDENTITY
        or manifest.get("manifest_sha256")
        != digest({key: value for key, value in manifest.items() if key != "manifest_sha256"})
        or manifest.get("source_dataset_sha256") != dataset_sha256(source)
    ):
        raise ValueError("news_owner_blind_manifest_or_source_changed")
    source_ids = [row["case_id"] for row in source]
    human_ids = [row["case_id"] for row in human]
    entries = manifest["cases"]
    entry_ids = [row["case_id"] for row in entries]
    if (
        not source
        or len(set(source_ids)) != len(source_ids)
        or len(set(human_ids)) != len(human_ids)
        or len(set(entry_ids)) != len(entry_ids)
        or source_ids != manifest["selected_case_ids"]
        or source_ids != entry_ids
        or set(source_ids) != set(human_ids)
    ):
        raise ValueError("news_owner_blind_selected_labels_incomplete")
    by_id = {row["case_id"]: row for row in human}
    records, public = [], []
    for original, entry in zip(source, entries, strict=True):
        metadata = {key: original[key] for key in _LABEL_METADATA if key in original}
        count = len(original["reader_input"].get("messages", []))
        mapping = entry["anchor_mapping"]
        options = {f"m{i + 1}" for i in range(count)}
        if (
            entry["input_sha256"] != digest(original)
            or entry["reader_input_sha256"] != digest(original["reader_input"])
            or entry["metadata"] != metadata
            or set(mapping) != options
            or set(mapping.values()) != options
        ):
            raise ValueError("news_owner_blind_original_input_or_mapping_changed")
        order = [int(mapping[f"m{i + 1}"][1:]) - 1 for i in range(count)]

        def restore_order(indices: list[int], original_order: Sequence[int] = order) -> None:
            indices[:] = original_order

        payload, _ = blind_case(original, restore_order)
        blind_sha = digest(payload)
        raw = by_id[original["case_id"]]
        if (
            set(raw) != {"case_id", "blind_input_sha256", "story_id", "label"}
            or raw["blind_input_sha256"] != blind_sha
            or entry["blind_input_sha256"] != blind_sha
        ):
            raise ValueError("news_owner_blind_material_sha256_mismatch")
        public.append({**payload, "blind_input_sha256": blind_sha})
        records.append(
            normalize_label(raw, mapping)
            | {
                "labeler": "owner",
                "guide_version": GUIDE_VERSION,
                "annotation_identity": ANNOTATION_IDENTITY,
                "input_sha256": entry["input_sha256"],
                "reader_input_sha256": entry["reader_input_sha256"],
                "blind_input_sha256": blind_sha,
                "protocol": OWNER_IMPORT_PROTOCOL,
                "proxy": False,
                **metadata,
            }
        )
    if manifest["blind_dataset_sha256"] != dataset_sha256(public):
        raise ValueError("news_owner_blind_material_sha256_mismatch")
    return records


async def label(args: argparse.Namespace) -> None:
    if args.output.suffix == ".gz":
        raise ValueError("news_blind_label_append_journal_requires_plain_jsonl")
    rows = read_jsonl(args.input)
    by_id = {row["case_id"]: row for row in rows}
    if not rows or len(by_id) != len(rows):
        raise ValueError("news_blind_label_empty_or_duplicate_case")
    blinded = {row["case_id"]: blind_case(row) for row in rows}
    done = {}
    if args.output.exists():
        journal = read_labels(args.output)
        done = {row["case_id"]: row for row in journal}
        if len(done) != len(journal) or set(done) - set(by_id):
            raise ValueError("news_blind_label_resume_cases_changed")
        if any(row.get("annotation_identity") != ANNOTATION_IDENTITY for row in journal):
            raise ValueError("news_blind_label_resume_instruction_changed")
        if any(row["input_sha256"] != digest(by_id[row["case_id"]]) for row in journal):
            raise ValueError("news_blind_label_resume_input_changed")
    pending = [row for row in rows if row["case_id"] not in done]
    sem = asyncio.Semaphore(args.concurrency)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.touch(mode=0o600, exist_ok=True)
    args.output.chmod(0o600)

    async def batch(group: list[dict[str, Any]]) -> None:
        async with sem:
            prompt = (
                OWNER_GUIDE
                + "\nCases:\n"
                + json.dumps([blinded[row["case_id"]][0] for row in group], ensure_ascii=False)
            )
            process = await asyncio.create_subprocess_exec(
                "claude",
                "-p",
                "--effort",
                "low",
                "--tools",
                "",
                "--no-session-persistence",
                "--output-format",
                "json",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=tempfile.gettempdir(),
            )
            try:
                stdout, _ = await asyncio.wait_for(process.communicate(prompt.encode()), timeout=args.timeout)
                if process.returncode:
                    raise ValueError("news_blind_label_provider_failed")
                envelope = json.loads(stdout)
                text = str(envelope["result"]).strip()
                if text.startswith("```"):
                    text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
                labels = json.loads(text)
                if len(labels) != len(group) or {row["case_id"] for row in labels} != {row["case_id"] for row in group}:
                    raise ValueError("news_blind_label_batch_incomplete")
                records = []
                for raw in labels:
                    case = raw["case_id"]
                    record = normalize_label(raw, blinded[case][1])
                    original = by_id[case]
                    record.update(
                        labeler=f"claude:{ANNOTATION_IDENTITY}",
                        guide_version=GUIDE_VERSION,
                        annotation_identity=ANNOTATION_IDENTITY,
                        input_sha256=digest(original),
                        reader_input_sha256=digest(original["reader_input"]) if "reader_input" in original else None,
                        annotator_models=sorted(envelope.get("modelUsage", {})),
                        annotator_effort="low",
                        protocol="news_reader_blind_labels_v4",
                        proxy=True,
                        **{key: original[key] for key in _LABEL_METADATA if key in original},
                    )
                    records.append(record)
                with args.output.open("a", encoding="utf-8") as stream:
                    for record in records:
                        stream.write(json.dumps(record, ensure_ascii=False) + "\n")
                        done[record["case_id"]] = record
                print(f"Claude blind proxy labels: {len(done)}/{len(rows)}", flush=True)
            finally:
                if process.returncode is None:
                    process.kill()
                    await process.wait()

    await asyncio.gather(
        *(batch(pending[start : start + args.batch_size]) for start in range(0, len(pending), args.batch_size))
    )


def read_labels(path: Path) -> list[dict[str, Any]]:
    rows = read_jsonl(path)
    if len({row["case_id"] for row in rows}) != len(rows):
        raise ValueError("news_blind_label_report_duplicate_case")
    return rows


def _agreement(a: list[Any], b: list[Any], categories: Sequence[Any], *, quadratic: bool = False) -> dict[str, Any]:
    n = len(a)
    index = {value: i for i, value in enumerate(categories)}
    matrix = [[0 for _ in categories] for _ in categories]
    for owner, proxy in zip(a, b, strict=True):
        matrix[index[owner]][index[proxy]] += 1
    if not n:
        return {
            "cases": 0,
            "kappa": None,
            "disagreement_rate": None,
            "categories": list(categories),
            "confusion": matrix,
        }
    weights = [
        [((i - j) / (len(categories) - 1)) ** 2 if quadratic else float(i != j) for j in range(len(categories))]
        for i in range(len(categories))
    ]
    observed = sum(weights[i][j] * matrix[i][j] for i in range(len(categories)) for j in range(len(categories))) / n
    marginal_a, marginal_b = (
        [sum(row) for row in matrix],
        [sum(row[j] for row in matrix) for j in range(len(categories))],
    )
    expected = (
        sum(
            weights[i][j] * marginal_a[i] * marginal_b[j]
            for i in range(len(categories))
            for j in range(len(categories))
        )
        / n**2
    )
    return {
        "cases": n,
        "kappa": 1 - observed / expected if expected else None,
        "disagreement_rate": sum(x != y for x, y in zip(a, b, strict=True)) / n,
        "categories": list(categories),
        "confusion": matrix,
        "rows": "owner",
        "columns": "proxy",
    }


def agreement_report(owner: Sequence[Mapping[str, Any]], proxy: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    if any(row.get("labeler") != "owner" for row in owner):
        raise ValueError("news_blind_label_gold_owner_required")
    if any(not row.get("labeler", "").startswith("claude:") for row in proxy):
        raise ValueError("news_blind_label_claude_proxy_required")
    gold, labels = {row["case_id"]: row for row in owner}, {row["case_id"]: row for row in proxy}
    if len(gold) != len(owner) or len(labels) != len(proxy):
        raise ValueError("news_blind_label_report_duplicate_case")
    overlapping = sorted(gold.keys() & labels.keys())
    if any(gold[case].get("guide_version") != labels[case].get("guide_version") for case in overlapping):
        raise ValueError("news_blind_label_agreement_guide_mismatch")
    if any(row.get("guide_version") != GUIDE_VERSION for row in [*owner, *proxy]):
        raise ValueError("news_blind_label_owner_guide_changed")
    if any(
        not gold[case].get("reader_input_sha256")
        or gold[case].get("reader_input_sha256") != labels[case].get("reader_input_sha256")
        for case in overlapping
    ):
        raise ValueError("news_blind_label_agreement_input_changed")
    fields: dict[str, Sequence[Any]] = {
        "kind": [kind for kind, _ in REPORT_KIND_OPTIONS],
        "push": ["feed", "borderline", "push"],
        "key": [False, True],
    }
    reports = {
        field: _agreement(
            [gold[case]["label"][field] for case in overlapping],
            [labels[case]["label"][field] for case in overlapping],
            categories,
            quadratic=field == "push",
        )
        for field, categories in fields.items()
    }
    anchor = (
        _agreement(
            [gold[case]["label"]["anchor"] for case in overlapping],
            [labels[case]["label"]["anchor"] for case in overlapping],
            sorted({row["label"]["anchor"] for row in [*owner, *proxy]}),
        )
        if overlapping
        else {"cases": 0}
    )
    return {
        "protocol": "news_reader_proxy_agreement_v1",
        "overlapping_owner_cases": len(overlapping),
        "fields": {**reports, "anchor": anchor},
        "proxy_is_truth": False,
        "key_certification_labels": "owner only",
        "key_disagreement_above_20_percent": reports["key"]["disagreement_rate"] is not None
        and reports["key"]["disagreement_rate"] > 0.2,
        "accuracy_claim": "agreement measures consistency with owner, not model accuracy",
    }


def review_queue(proxy: Sequence[Mapping[str, Any]], fitted: Mapping[str, Any]) -> list[dict[str, Any]]:
    predictions = {row["case_id"]: row for row in fitted["oof_predictions"]}
    if len(predictions) != len(fitted["oof_predictions"]):
        raise ValueError("news_blind_label_oof_duplicate_case")
    queue = []
    for row in proxy:
        case = row["case_id"]
        if case not in predictions or row["label"]["push"] == "borderline":
            continue
        prediction = predictions[case]
        if row.get("guide_version") != GUIDE_VERSION or prediction.get("guide_version") != GUIDE_VERSION:
            raise ValueError("news_blind_label_review_guide_changed")
        if not row.get("reader_input_sha256") or row.get("reader_input_sha256") != prediction.get("input_sha256"):
            raise ValueError("news_blind_label_review_input_changed")
        if case not in fitted["split"]["fit"] or case in fitted["split"]["certification"]:
            raise ValueError("news_blind_label_review_prediction_not_out_of_fold_fit")
        disagreement = max(
            abs(prediction["p_push"] - int(row["label"]["push"] == "push")),
            abs(prediction["p_key"] - int(row["label"]["key"])),
        )
        queue.append(
            {
                "case_id": case,
                "disagreement": disagreement,
                "proxy_label": row["label"],
                "oof_prediction": prediction,
                "action": "owner review; never automatically relabel",
            }
        )
    return sorted(queue, key=lambda row: (-row["disagreement"], row["case_id"]))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    annotation = commands.add_parser("annotate")
    annotation.add_argument("--input", type=Path, required=True)
    annotation.add_argument("--output", type=Path, required=True)
    annotation.add_argument("--batch-size", type=int, default=10)
    annotation.add_argument("--concurrency", type=int, default=2)
    annotation.add_argument("--timeout", type=float, default=120)
    preparation = commands.add_parser("prepare-owner", help="Prepare human blind text and a private import manifest.")
    preparation.add_argument("--input", type=Path, required=True, help="Frozen owner-sample JSONL[.gz].")
    preparation.add_argument("--output", type=Path, required=True, help="Public blind material JSONL[.gz].")
    preparation.add_argument("--manifest", type=Path, required=True, help="Private SHA, selection and anchor mapping.")
    owner_import = commands.add_parser("import-owner", help="Import complete human labels without a provider call.")
    owner_import.add_argument("--input", type=Path, required=True, help="The original frozen owner-sample file.")
    owner_import.add_argument("--manifest", type=Path, required=True, help="Private prepare-owner manifest.")
    owner_import.add_argument(
        "--labels", type=Path, required=True, help="Human case_id/blind SHA/story_id/label JSONL[.gz]."
    )
    owner_import.add_argument("--output", type=Path, required=True, help="Owner label journal JSONL[.gz].")
    report = commands.add_parser("report")
    report.add_argument("--owner", type=Path, required=True)
    report.add_argument("--proxy", type=Path, required=True)
    report.add_argument("--candidate", type=Path, help="Fitted artifact containing story-disjoint OOF predictions.")
    report.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "annotate":
        if not 1 <= args.batch_size <= 100 or not 1 <= args.concurrency <= 4 or args.timeout <= 0:
            parser.error("invalid bounded annotation batch/concurrency/timeout")
        asyncio.run(label(args))
    elif args.command == "prepare-owner":
        if len({path.resolve() for path in (args.input, args.output, args.manifest)}) != 3:
            parser.error("source, public output and private manifest must be separate paths")
        public, manifest = prepare_owner(read_jsonl(args.input))
        write_jsonl(args.output, public)
        args.manifest.parent.mkdir(parents=True, exist_ok=True)
        with args.manifest.open("w", encoding="utf-8") as stream:
            args.manifest.chmod(0o600)
            stream.write(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    elif args.command == "import-owner":
        if args.output.resolve() in {path.resolve() for path in (args.input, args.manifest, args.labels)}:
            parser.error("owner output must preserve source, private manifest and human labels")
        records = import_owner(
            read_jsonl(args.input), read_jsonl(args.labels), json.loads(args.manifest.read_text("utf-8"))
        )
        write_jsonl(args.output, records)
    else:
        owner, proxy = read_labels(args.owner), read_labels(args.proxy)
        result = agreement_report(owner, proxy)
        if args.candidate:
            result["review_queue"] = review_queue(proxy, json.loads(args.candidate.read_text("utf-8")))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
