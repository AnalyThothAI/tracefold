from tracefold.market.capture.entity_extractor import TextSurface, extract_entities_from_surfaces


def test_malformed_url_like_provider_text_does_not_abort_entity_extraction() -> None:
    text = "https://AVE.ai,他们最近在搞Stable的交易大赛，8888USDT的奖池：0x0000000000000000000000000000000000000001"

    entities = extract_entities_from_surfaces([TextSurface(surface="text", text=text)])

    assert any(entity.entity_type == "ca" for entity in entities)
    assert any(entity.entity_type == "url" for entity in entities)


def test_robinhood_chain_hint_resolves_an_evm_contract_to_the_canonical_chain() -> None:
    address = "0x020bfc650a365f8bb26819deaabf3e21291018b4"

    entities = extract_entities_from_surfaces(
        [TextSurface(surface="text", text=f"Robinhood Chain contract: {address}")]
    )

    contract = next(entity for entity in entities if entity.entity_type == "ca")
    assert contract.chain == "robinhood"
    assert contract.token_resolution_status == "resolved_ca"
