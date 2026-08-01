from tracefold.market.capture.entity_extractor import TextSurface, extract_entities_from_surfaces


def test_malformed_url_like_provider_text_does_not_abort_entity_extraction() -> None:
    text = (
        "https://AVE.ai,他们最近在搞Stable的交易大赛，8888USDT的奖池："
        "0x0000000000000000000000000000000000000001"
    )

    entities = extract_entities_from_surfaces([TextSurface(surface="text", text=text)])

    assert any(entity.entity_type == "ca" for entity in entities)
    assert any(entity.entity_type == "url" for entity in entities)
