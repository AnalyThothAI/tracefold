"""Adapter responsibility moves preserve identities; changed extraction contracts change its cache.

The values are captured from main 3c429fc before #759's responsibility move, with a fixed model
identity. Extraction intentionally adds source candidates and uses the shared MarketType vocabulary;
the unrelated judgment and copy programs retain their original identities.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from tracefold.news.adapters.card_copy import CopySignature, DspyCardComposer
from tracefold.news.adapters.extraction import DspyExtractor, ExtractSignature
from tracefold.news.adapters.reader_judge import DspyReaderJudge, reader_signature
from tracefold.news.adapters.semantic_judgments import (
    GeneratedJudgments,
    GeneratedJudgmentSignature,
    NativeJudgments,
    native_signature,
)
from tracefold.news.updates.identity import digest
from tracefold.news.updates.judgment import QUESTION_VERSION
from tracefold.news.updates.topics import CODEBOOK


def test_image_news_identity_probe_executes_its_actual_build_call() -> None:
    root = Path(__file__).resolve().parents[2]
    dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
    probe = re.search(r"^RUN /app/\.venv/bin/python -c \\\n\s+'([^']+)'", dockerfile, flags=re.MULTILINE)
    assert probe is not None, "the image must validate its News program identity"
    result = subprocess.run(
        [sys.executable, "-c", probe.group(1)],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_extraction_contract_change_preserves_unrelated_program_and_schema_identities() -> None:
    model = "identity-fixture"
    actual = {
        "extractor": DspyExtractor(lambda: None, model_identity=model, topics=dict(CODEBOOK)).identity,
        "copy": DspyCardComposer(lambda: None, model_identity=model).identity,
        "generated": GeneratedJudgments(lambda: None, model_identity=model).identity,
        "native": NativeJudgments(lambda: None, model_identity=model).identity,
        "reader": DspyReaderJudge(
            lambda: None,
            generated_model_identity=model,
            native_lm_factory=lambda: None,
            native_model_identity="native-fixture",
        ).identity,
    }
    assert actual == {
        "extractor": "extractor:5b24af81420fef3a7127b722d4e911a6e01d0bc0314d87f88f449b0934faf3ba",
        "copy": "news_card_copy:d0a6746bae6657d750165ab78db10ffd4ef4838b69ed2f8c191acb90c0037261",
        "generated": "generated_judgment:0ff4b04ea2eb0f1f01890b7d21d1705b86284f1801c367ace51677c7da4be460",
        "native": "native_judgment:763a95518060a756b572c0163fcb4f35b5fbc556daf46dd9e295cfa71bc08fca",
        "reader": "news_reader_judge:879f7202524d09293138986aa32640f9a73b01201181785d89e4897449164b1e",
    }
    assert {
        "extract": digest(ExtractSignature.model_json_schema()),
        "copy": digest(CopySignature.model_json_schema()),
        "judge": digest(GeneratedJudgmentSignature.model_json_schema()),
        "native": digest(native_signature("relation", 2, True, QUESTION_VERSION).model_json_schema()),
        "reader": digest(reader_signature(2).model_json_schema()),
    } == {
        "extract": "16ba4f38c98237cb65f6c3087d3da7956021d8bb9ab7edef11e433d96f23e7e1",
        "copy": "27c46b662ab3832abc75669acea346cc3a4dbecbed1a7675cfcddad287e5b64e",
        "judge": "a0878d7aef415b4f4dfe64a0ff99adc03a86bfb3aa3e0448b2efa5e9d038e42e",
        "native": "53d60e9e8714d736b5018db89f0ea762267e6dd814d00c513867474d951c341d",
        "reader": "8846b2f568693f554e058b0fae6479fbd57985b81aa7527a7a7a23c6ff94172f",
    }
