"""Program identities, with a fixed model identity, and the signature schemas they include.

#770 bound `adds_information` to the same core fact and moved the questions to `news_questions_v4`, so the
generated and native judgment identities and the native relation signature schema changed together. The
card copy and reader programs carry no relation option and keep their identities; the generated judgment
signature schema is unchanged. #788 changes the extractor instruction to read listed markets and omit
uninformative source markets, intentionally changing its program identity without changing read refs.
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


def test_program_and_signature_schema_identities_are_pinned() -> None:
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
        "extractor": "extractor:5d475c2f70b1c9bd4d7dfab192067d634c8b3110819c4e609de043c2700b5e36",
        "copy": "news_card_copy:410f061c5d2ead4ecb1011ed64d4895d831266aceb836a446cf5225fc8125876",
        "generated": "generated_judgment:a28f6ab99c87f0a20a5e7572179bc7226ac2f16399628e52b6babf710f01c27c",
        "native": "native_judgment:84fe75cb1b310f683dae0d73a5bda7e670ce5ecd787959ac5acc356e40cf75de",
        "reader": "news_reader_judge:3af8592d934a884e6fcc685fcf090dd6a050b2675fffc58c3195d6f6fd7f388a",
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
        "native": "f3a475e9ce2ee597e078d877cdbefa79acc41550caaf7180cd48534e6fc6abd6",
        "reader": "8846b2f568693f554e058b0fae6479fbd57985b81aa7527a7a7a23c6ff94172f",
    }
