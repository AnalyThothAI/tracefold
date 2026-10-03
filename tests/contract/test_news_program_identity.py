"""Program identities, with a fixed model identity, and the signature schemas they include.

#791 moves extraction and speech questions to English v5, adds actor_role without
changing claim material identity, and moves reader inputs to v3 with a fixed as_of.
#809 changes the extraction instruction, transport descriptions and field order.
The card composer, relation and reader signatures remain stable.
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
        "extractor": "extractor:845ea0eaa1bb8294f61f41bde8c8f4ec6043a0fae295bf927eb24928be87f981",
        "copy": "news_card_copy:410f061c5d2ead4ecb1011ed64d4895d831266aceb836a446cf5225fc8125876",
        "generated": "generated_judgment:59a8be20d63b8ba3c8b29aff590486774ce2c56bff75de22b8fd1f47f4cf4c7f",
        "native": "native_judgment:0b0c7a11510f5d0f22095b42810311a0fa5f30b23e17ce7cf433395c7b1627c8",
        "reader": "news_reader_judge:15b10baf2cd1f33e54ede8017542a9cbc76c8adf427bcdc91392e71b0b242e51",
    }
    assert {
        "extract": digest(ExtractSignature.model_json_schema()),
        "copy": digest(CopySignature.model_json_schema()),
        "judge": digest(GeneratedJudgmentSignature.model_json_schema()),
        "native": digest(native_signature("relation", 2, True, QUESTION_VERSION).model_json_schema()),
        "reader": digest(reader_signature(2).model_json_schema()),
    } == {
        "extract": "6793edc2cbdc949af282a6dfda7b80aedb86330dda5443abaf13db6e48e01cc5",
        "copy": "27c46b662ab3832abc75669acea346cc3a4dbecbed1a7675cfcddad287e5b64e",
        "judge": "a0878d7aef415b4f4dfe64a0ff99adc03a86bfb3aa3e0448b2efa5e9d038e42e",
        "native": "f3a475e9ce2ee597e078d877cdbefa79acc41550caaf7180cd48534e6fc6abd6",
        "reader": "3e7d290ab9e94c127ca067e2a0f34ae6de8dd256969078c8dd23b41464cf70cd",
    }
