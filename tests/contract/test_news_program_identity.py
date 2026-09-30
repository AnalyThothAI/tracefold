"""Moving model adapters must not invalidate successful extraction or judgment caches.

The values are captured from main 3c429fc before #759's responsibility move, with a fixed model
identity. Changes to questions/schema need an intentional contract version change, not a path rename.
"""

from __future__ import annotations

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


def test_moving_adapters_preserves_existing_program_and_schema_identities() -> None:
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
        "extractor": "extractor:8aff9deb2553f79c9f59d715b59a6c479c0da3f00a468f7b3e8c13ef8c89997f",
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
        "extract": "b6230942371de3b0082546c8ede68ce3813c99ef3f3f5cc35e5ab8ede05f5ab8",
        "copy": "27c46b662ab3832abc75669acea346cc3a4dbecbed1a7675cfcddad287e5b64e",
        "judge": "a0878d7aef415b4f4dfe64a0ff99adc03a86bfb3aa3e0448b2efa5e9d038e42e",
        "native": "53d60e9e8714d736b5018db89f0ea762267e6dd814d00c513867474d951c341d",
        "reader": "8846b2f568693f554e058b0fae6479fbd57985b81aa7527a7a7a23c6ff94172f",
    }
