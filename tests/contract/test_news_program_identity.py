"""Program identities, with a fixed model identity, and the signature schemas they include.

#791 moves extraction and speech questions to English v5, adds actor_role without
changing claim material identity, and moves reader inputs to v3 with a fixed as_of.
The card composer and relation signature shape remain stable.
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
        "extractor": "extractor:a84f781b6e3994b1b94c9a5d3472a64461b16f510eed231041c865e2d13069ef",
        "copy": "news_card_copy:410f061c5d2ead4ecb1011ed64d4895d831266aceb836a446cf5225fc8125876",
        "generated": "generated_judgment:b08c0cd790661c2b8bb34f9a5bebac773ebca1c5c5b00ceec9fc7a8d183d50b8",
        "native": "native_judgment:5277aa0905c66fa2c31096e8faa91cbd7de33e3de74022cb100d8f78dd1e4281",
        "reader": "news_reader_judge:10dad0d2f838c2ed03c0c55dc0c5bdedd0d2baf348b86d2c1857290989ab4919",
    }
    assert {
        "extract": digest(ExtractSignature.model_json_schema()),
        "copy": digest(CopySignature.model_json_schema()),
        "judge": digest(GeneratedJudgmentSignature.model_json_schema()),
        "native": digest(native_signature("relation", 2, True, QUESTION_VERSION).model_json_schema()),
        "reader": digest(reader_signature(2).model_json_schema()),
    } == {
        "extract": "4e4844d2bcabcb60186152f86157f297c02b58d4a1f98d6f27fb008f6960b1d5",
        "copy": "27c46b662ab3832abc75669acea346cc3a4dbecbed1a7675cfcddad287e5b64e",
        "judge": "a0878d7aef415b4f4dfe64a0ff99adc03a86bfb3aa3e0448b2efa5e9d038e42e",
        "native": "f3a475e9ce2ee597e078d877cdbefa79acc41550caaf7180cd48534e6fc6abd6",
        "reader": "64d2c6e80e612c9f4f770c1c49b372988acd3d47e2a1aafd42f978fa710c566e",
    }
