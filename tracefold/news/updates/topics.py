"""The existing finite IPTC navigation vocabulary, without the retired four axes."""
from __future__ import annotations

from .judgment import Answer

CODEBOOK: tuple[tuple[str, str], ...] = (
    ("medtop:04000000", "economy, business and finance"),
    ("medtop:20000174", "bankruptcy"), ("medtop:20000175", "stock buyback"),
    ("medtop:20000177", "corporate dividends"), ("medtop:20000178", "corporate earnings"),
    ("medtop:20000180", "financial statement"), ("medtop:20000183", "business financing"),
    ("medtop:20000186", "stock activity"), ("medtop:20000187", "stock flotation"),
    ("medtop:20000189", "layoffs and downsizing"), ("medtop:20000190", "executive officer"),
    ("medtop:20000192", "business strategy and marketing"), ("medtop:20000195", "board of directors"),
    ("medtop:20000196", "commercial contract"), ("medtop:20000197", "spin-off"),
    ("medtop:20000199", "business governance"), ("medtop:20000200", "joint venture"),
    ("medtop:20000204", "merger and acquisition"), ("medtop:20000205", "new product or service"),
    ("medtop:20000207", "product recall"), ("medtop:20000208", "research and development"),
    ("medtop:20000344", "economy"), ("medtop:20000346", "economic trends and indicators"),
    ("medtop:20000350", "central bank"), ("medtop:20000359", "gross domestic product"),
    ("medtop:20000365", "employment statistics"), ("medtop:20000370", "inflation"),
    ("medtop:20000371", "interest rates"), ("medtop:20000373", "international trade"),
    ("medtop:20000379", "monetary policy"), ("medtop:20000384", "tariff"),
    ("medtop:20000385", "market and exchange"), ("medtop:20001164", "payment service"),
    ("medtop:20001279", "cryptocurrency"), ("medtop:16000000", "conflict, war and peace"),
)


def project_topics(answers: tuple[Answer, ...], codebook: tuple[tuple[str, str], ...] = CODEBOOK) -> tuple[str, ...]:
    order = {code: index for index, (code, _) in enumerate(codebook)}
    chosen = {a.item_id: a for a in answers if a.status == "available" and a.value is True and a.item_id in order}
    if chosen.keys() - {"medtop:04000000", "medtop:16000000"}:
        chosen.pop("medtop:04000000", None)
    if chosen.keys() & {"medtop:20000346", "medtop:20000350", "medtop:20000359", "medtop:20000365", "medtop:20000370", "medtop:20000371", "medtop:20000373", "medtop:20000379", "medtop:20000384"}:
        chosen.pop("medtop:20000344", None)
    # Raw true probability orders topic projection only. Generated Booleans have
    # no provider probability; ties use the original codebook order, not a fake 1.0.
    ranked = sorted(chosen, key=lambda code: (-(chosen[code].probabilities or {}).get("true", 0.0), order[code]))[:3]
    return tuple(sorted(ranked, key=order.__getitem__))
