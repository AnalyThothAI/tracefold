"""The ordinary model policy and its code-owned degraded fallback.

``TRIAGE_POLICY_VERSION`` is ``news_triage_policy_v17``. v16 (#675 §1) deleted the branch that read the
model's own answer to "should the reader get this": the model observes (`fact_kind`, `novelty`, typed
assets, direction, scope), the code decides, and :func:`decide` is the whole of the decision -- one ordered
table whose every row has a name, reads only facts the code produced and stored, and can be replayed
against a recording.

v17 deletes the #504 D2 per-storyline budget, which withheld an ordinary push as `storyline:<key>:budget`
once the reader had received two cards on its storyline key inside an hour, whatever the card said. The
owner withdrew it on 2026-09-23, reversing #675 §6's "no storyline budget changes". The restatement drop,
the deterministic listing and watchlist guards, the decision table, the single-name instrument rule, the
stale-source rule and the similarity check keep their position and their behaviour, and the bigram
threshold is untouched.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field, fields
from typing import Any, Final

from .artifact_identity import canonical_sha
from .events.storyline import NO_STORYLINE_KEY
from .models import (
    DROP_FACT_KINDS,
    FACT_KINDS,
    MATERIAL_FACT_KINDS,
    PUSH_FACT_KINDS,
    STALE_SOURCE_KEY,
    Decision,
    FactKind,
    TriageVerdict,
    base_symbol,
)
from .program.contracts import JUDGMENT_CONTRACT_VERSION, ScoredJudgment
from .similarity import max_similarity

_DIRECTIONAL = frozenset({"bullish", "bearish"})


@dataclass(frozen=True, slots=True)
class DecidePolicy:
    """The four safety/duplicate knobs exposed through ``news.policy``.

    Trade relevance and objective guards are a code-owned ordered policy, not
    operator-tunable thresholds.
    """

    restatement_drop: bool = True
    # Duplicate protection is content-based, never a reader quota. A value of
    # zero disables the deterministic similarity check; it does not restore a
    # count cap. Escalations remain exempt because a false-positive similarity
    # match is least affordable for the most important cards.
    similarity_max: float = 0.25
    # An artifact older than this when the provider pushed it is a replay, not news (#154). The artifact ledger
    # catches a re-send of something we already delivered; this catches the case it cannot see — a stale artifact
    # arriving for the first time, such as the 16-day-old tweet that shipped as "Take-Two 股票 $TTWO 周四在
    # Solana 上线". Measured over 3174 x/twitter frames in 30 days the distribution is bimodal: 2491 within 10 s
    # of the push, 7 beyond 16 h, nothing between, and never negative — so any threshold in [10 min, 16 h] picks
    # exactly the same frames. 12 h is the `general` family window: older than that and our own dedup could no
    # longer have seen the first delivery. Zero disables the rule.
    stale_source_max_age_s: int = 12 * 60 * 60
    # Exchange listing/delisting frames are independent facts wearing one template: "Coinbase adds
    # ALIGN" and "Upbit adds BICO" name different instruments but share almost every character
    # bigram, so both the model's restatement judgment and the deterministic similarity check read
    # the second one as a repeat. #72 admits these frames deterministically; this stops that
    # admission from being undone one step later. The trade is explicit: a genuine re-send of the
    # same notice is no longer withheld by content.
    listing_exempt_from_duplicate: bool = True

    def as_dict(self) -> dict[str, Any]:
        """Every tunable, by name. A stored decision has to carry the numbers that produced it: without this the
        trace said which rule fired but not against which thresholds, so a historical verdict could not be
        replayed or compared with a candidate (#81). Also the policy half of the release-gate evidence."""

        out: dict[str, Any] = {}
        for spec in fields(self):
            value = getattr(self, spec.name)
            out[spec.name] = list(value) if isinstance(value, tuple) else value
        return out


DEFAULT_POLICY = DecidePolicy()


@dataclass(frozen=True, slots=True)
class GateFacts:
    grounded_assets: tuple[str, ...]
    watchlist_symbols: frozenset[str]
    admission: str
    # Seconds between the source artifact's own publication and the provider's push (#154). `None` whenever the
    # artifact does not carry its own timestamp, which is every non-x/twitter frame.
    source_age_s: int | None = None
    # #675 §3: how many *distinct member texts* the Deduper merged, counted over `evidence_text_sha256`.
    #
    # It replaced `member_count` outright in v16, and `member_count` is gone from this contract with it.
    # #504 D3 gave the escalate corroboration rule the Deduper's arrival count on the theory that a second
    # arrival is a second party; the #675 Tencent card is why that is false. It had two members and both
    # were the same jin10 line arriving twice, so `member_count` read 2 and called one wire two parties. A
    # digest over the normalized provider body is the only thing that can tell a second arrival from a
    # second source, so the corroboration rule now reads this and nothing else counts arrivals.
    independent_text_count: int = 1
    # The Event's own wire title. The decision table checks the text the fact actually arrived in as well as
    # the reader headline the model wrote from it: a basis stated in the English source and dropped from the
    # 60-character Chinese headline is still a basis, and the reverse is not true.
    title: str = ""


@dataclass(frozen=True, slots=True)
class StorylineStatus:
    """Content evidence from cards proven to have reached the reader.

    The small ``told`` subset grounds the model's restatement citation.  The
    wider ``seen`` subset supports deterministic same-fact comparison.  It
    intentionally carries no delivery counts or capacity state.
    """

    key: str
    told_directions: tuple[str, ...] = ()
    # Same order as ``told_directions``: the instruments each shown ledger entry was about, so a
    # restatement claim can be checked against the asset it cites rather than its rendered prose.
    told_assets: tuple[frozenset[str], ...] = ()
    # #675 §3: the storyline key and settle stamp of each shown ledger entry, same order. The two conflict
    # rows ask "how much of this storyline has the reader already been handed", and the honest evidence for
    # that is the ledger the model was judging against, not the wider `seen_*` window: the model saw these
    # rows, so a row it still called new is a fact about the judgment, and a row it never saw is not.
    told_keys: tuple[str, ...] = ()
    told_at_ms: tuple[int, ...] = ()
    # Every card the reader actually received in the comparison window, newest first — not the <= 16 entries the
    # status bar showed the model. The two differ by design: the model gets a readable ledger, ``decide()`` gets
    # the whole window, and the wider set measurably catches more repeats (#81).
    seen_headlines: tuple[str, ...] = ()
    seen_event_ids: tuple[str, ...] = ()
    # The direction of each ``seen_headlines`` entry, same order. A reversal of a fact the reader just received
    # shares almost every character bigram with it ("SEC 批准…" vs "SEC 拒绝…" scores 0.60), so the duplicate
    # defence has to be able to tell the two apart.
    seen_directions: tuple[str, ...] = ()
    # The instruments each remembered card was about, same order. `headline_zh` is Chinese reader prose and the
    # reader contract strips parenthesised tickers, so the rendered text cannot answer "is this the same asset?";
    # only a structured field can. Empty for a caller that did not supply assets, which never grants an exemption.
    seen_assets: tuple[frozenset[str], ...] = ()

    @property
    def told_count(self) -> int:
        return len(self.told_directions)

    def told_on_key_within(self, *, now_ms: int | None, window_ms: int) -> int:
        """Shown ledger entries on exactly this storyline key inside the window.

        Exact string equality on the key, deliberately: this counts what the reader was handed on *this*
        key, and the market-aware comparison retrieval uses is deliberately inclusive. A caller with no clock
        (a pure replay that kept no stamp) gets the age-blind count, which is what the ledger's own 4 h
        recency bound already is.
        """

        cutoff = None if now_ms is None else int(now_ms) - int(window_ms)
        return sum(
            1
            for index, key in enumerate(self.told_keys)
            if key == self.key
            and key != NO_STORYLINE_KEY
            and (cutoff is None or (index < len(self.told_at_ms) and int(self.told_at_ms[index]) >= cutoff))
        )


@dataclass(frozen=True, slots=True)
class DecisionResult:
    final: Decision
    override_rule: str | None
    throttled_by: str | None
    rule_baseline: Decision
    watchlist_hits: tuple[str, ...] = field(default_factory=tuple)
    # Only set when the card was measured against the reader's window: how close it came, and which of
    # ``status.seen_*`` it came closest to (-1 = nothing to compare against).
    seen_similarity: float | None = None
    seen_against: int = -1
    # ``all`` means the push or escalate path was compared with the sent ledger (only a push can be withheld by
    # it); empty means no comparison was made.
    seen_scope: str = ""


@dataclass(frozen=True, slots=True)
class DegradedJudgment:
    """One unavailable-model presentation and its only action authority."""

    verdict: TriageVerdict
    decision: DecisionResult
    error_code: str
    judgment_contract_version: str = field(default=JUDGMENT_CONTRACT_VERSION, init=False)

    @property
    def judgment_atom(self) -> dict[str, Any]:
        return {
            "judgment_contract_version": self.judgment_contract_version,
            "origin": "degraded",
            "verdict": self.verdict.model_dump(mode="json"),
            "decision": asdict(self.decision),
            "error_code": self.error_code,
        }

    @property
    def judgment_sha256(self) -> str:
        return canonical_sha(self.judgment_atom)


_base = base_symbol


def grounded_watchlist_hits(facts: GateFacts) -> tuple[str, ...]:
    """Objective watchlist facts, independent of any model-selected primary."""

    return tuple(sorted({_base(s) for s in facts.grounded_assets} & facts.watchlist_symbols))


def rule_baseline(facts: GateFacts) -> Decision:
    """The degraded baseline: only objective guards fail open."""

    if facts.admission == "listing_deterministic":
        return "push"
    return "push" if grounded_watchlist_hits(facts) else "drop"


# ---------------------------------------------------------------- #675 §3: the decision table's own facts
#
# What makes a quote worth interrupting a reader for, stated as text the card itself has to carry. The
# 2026-09-22 audit read all 417 delivered cards of one day: 39 were pure price broadcasts a reviewer would
# not have been interrupted for, and every one of the ones a reviewer *did* want shared one of these five
# shapes. The vocabulary is bilingual because the first cut of it was not: 11 of the 14 cards the audit's
# simulation wrongly dropped were English or a Chinese variant the Chinese-only list did not spell
# ("seven-month high", "rises above $82,000", "创 7 月 30 日以来最大盘中涨幅", "失守 100 美元关口").
#
# This is admissibility, not importance. It decides whether the text states a fact beyond "the number moved",
# and it is code-owned precisely because the model kept answering that question with the seed's own
# five-point calibration, which #675 §1 deletes.
_PRICE_LEVEL_CROSSED: Final = (
    r"站上|跌破|突破|收复|失守|关口|首次突破"
    # "回落至 X 下方" and "跌回每桶 100 美元下方" are one shape with two verbs; both name the level crossed.
    r"|(回落|跌回|回落至|跌至|下探至)[^，。,.]{0,14}?下方|(升至|涨回|回升至|反弹至)[^，。,.]{0,14}?上方"
    r"|rises?\s+above|rose\s+above|falls?\s+below|fell\s+below|drops?\s+below"
    r"|reclaims?|reclaimed|back\s+above|first\s+time\s+since"
)
_PRICE_PERIOD_RECORD: Final = (
    r"创[^，。,.]{0,12}?(新高|新低|高位|低位|纪录)"
    r"|(历史|创纪录|阶段性)?(新高|新低)"
    r"|(年内|月内|周内)(新高|新低|高点|低点)"
    r"|record\s+(high|low)"
    r"|(highest|lowest)\s+(since|level|price)"
    # "seven-month high" is how the wires write it; a digits-only pattern missed every English one.
    r"|(\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|multi)[\s-]+"
    r"(month|week|year|day|session)[\s-]+(high|low)"
    r"|最大(单日|日内|盘中|单周|单月)?(涨|跌)幅"
    r"|(largest|biggest)\s+(single-day\s+|daily\s+|intraday\s+)?(gain|drop|loss|rise|fall|decline)"
)
# A flow only counts when the text quantifies it. "Outflows continue" is a mood; "$648M withdrawn" is a fact.
_PRICE_FLOW_WORDS: Final = (
    r"清算|爆仓|净流入|净流出|增持|减持|提币|提取|转出|存入|持仓"
    r"|liquidat\w*|inflows?|outflows?|withdraw\w*|deposit\w*"
)
_PRICE_DIGIT: Final = r"[\d一二三四五六七八九十百千万亿]"
_PRICE_BASIS_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(_PRICE_LEVEL_CROSSED, re.IGNORECASE),
    re.compile(_PRICE_PERIOD_RECORD, re.IGNORECASE),
    # A stablecoin off its peg is a credit event wearing a price, whichever side of the number the word is on.
    re.compile(r"脱锚|depeg\w*|跌至\s*0\.9\d", re.IGNORECASE),
    # Freight is a physical-supply price; the Hormuz VLCC day rate is the fact, not the percentage.
    re.compile(r"租金|运费|freight", re.IGNORECASE),
    re.compile(rf"{_PRICE_DIGIT}[^\n]{{0,24}}?({_PRICE_FLOW_WORDS})", re.IGNORECASE),
    re.compile(rf"({_PRICE_FLOW_WORDS})[^\n]{{0,24}}?{_PRICE_DIGIT}", re.IGNORECASE),
)
_PRICE_PERCENT: Final = re.compile(r"(\d+(?:\.\d+)?)\s*%")
# The owner's one exception (#675 §7): a same-day move of this size is itself the fact, but only where a
# whole market moved. A single stock is excluded by name -- the Tencent card that opened #675 is +7 % and
# stays a background card -- so the exception is carried by the primary asset's market, never by its size.
PRICE_MOVE_EXCEPTION_PERCENT: Final = 5.0
PRICE_MOVE_EXCEPTION_MARKETS: Final[frozenset[str]] = frozenset({"commodity", "index"})
# The three kinds the >= 5% exception may not rescue. Each one says the text is about something other than
# the move it mentions -- told again, scheduled, or sold -- so admitting it on the size of the number would
# push the class the 2026-09-22 audit counted as its largest demote bucket. `statement` is deliberately not
# here: the move is usually the thing being stated (#679 review 3).
PRICE_MOVE_EXCEPTION_EXCLUDED_KINDS: Final[frozenset[str]] = frozenset({"recap", "schedule", "promotion"})


def price_move_basis(text: str) -> bool:
    """True when the text states something about the move beyond the number itself."""

    value = str(text or "")
    return bool(value) and any(pattern.search(value) for pattern in _PRICE_BASIS_PATTERNS)


def _states_large_daily_move(text: str) -> bool:
    return any(float(value) >= PRICE_MOVE_EXCEPTION_PERCENT for value in _PRICE_PERCENT.findall(str(text or "")))


# Exchange listing notices: one wire template carrying different instruments. "Coinbase adds ALIGN"
# and "Upbit adds BICO" share almost every character bigram while naming different tradable things,
# so the duplicate check needs the instrument, not the prose (#72).
_TEMPLATE_ADMISSIONS: Final = frozenset({"listing_deterministic"})


def _template_fact(facts: GateFacts) -> bool:
    """True for a Gate-admitted frame whose text is a template carrying an instrument.

    Only the Gate's admission counts. Model taxonomy is not admission evidence, so trusting it would
    let a recurring "X will support Y" tease escape duplicate evidence on every repeat with no
    corroboration. The admission is derived upstream from provider metadata.
    """

    return facts.admission in _TEMPLATE_ADMISSIONS


def _names_another_instrument(
    template_fact: bool,
    symbols: set[str],
    seen_assets: Sequence[frozenset[str]],
    index: int,
) -> bool:
    """True when a template frame's closest match is a card about a *different* instrument.

    Exchange notices share one wire template, so "Coinbase 将新增对 ALIGN 的支持" and "Upbit 将新增对
    BICO 的交易支持" score well above ``similarity_max`` on character bigrams while naming different
    tradable things. Exempting the whole class would stop protecting the reader from a genuinely
    re-issued notice, so the exemption is narrowed to the case it exists for.

    The comparison is between symbol sets, never between a ticker and rendered headline text: the
    reader contract tells the model to strip parenthesised tickers and write Chinese, so a substring
    test would both miss the common case and fire by accident (``BASE`` inside "Coinbase"). A ledger
    row that carries no assets is not evidence of a different instrument and never exempts.
    """

    if not template_fact or not symbols or not 0 <= index < len(seen_assets):
        return False
    matched = seen_assets[index]
    return bool(matched) and matched.isdisjoint(symbols)


def _seen_flip(direction: str, seen_directions: Sequence[str], index: int) -> bool:
    """True when the card resembles a ledger entry it *contradicts* — the reader was told bullish and this is
    bearish, or the other way round. Only a directional pair counts: neutral/unclear on either side is not a
    reversal, and a ledger without directions (a pure caller, an old replay) never exempts anything."""

    if direction not in _DIRECTIONAL or not 0 <= index < len(seen_directions):
        return False
    told = seen_directions[index]
    return told in _DIRECTIONAL and told != direction


def grounded_restatement(verdict: TriageVerdict, status: StorylineStatus | None) -> bool:
    """True when the model called this a restatement *of a ledger entry it was actually shown*.

    An out-of-range ``restates`` (or an empty ledger) is ignored: novelty then counts as new_fact, so a
    hallucinated restatement can never drop a card.

    The label decides, and the direction does not (#651 §6.3). This used to exempt a `restatement` whose
    `direction` had flipped against the cited told entry, on the theory that a reversal cannot be a repeat.
    But `direction` is the model's own reading of a fact, not a fact about the world. `ec2e5a29` and
    `727ffc0b` are one Visa onchain-credit release carried by two outlets and the reader received both
    cards; the model called the second a progression, so the drop it earned it never reached this guard --
    and had it called it a restatement while reading the direction the other way, the exemption would have
    delivered it anyway. Correcting the instruction alone could not have stopped that card.

    A real world reversal does not arrive wearing this label at all -- it is a new action, so it arrives as
    `progression` or `new_fact`, and it is those two that ``_seen_flip`` still protects against the
    similarity check.
    """

    if verdict.novelty != "restatement" or status is None or status.told_count == 0:
        return False
    return 0 <= verdict.restates < status.told_count


_CONFLICT_KEY_PREFIX: Final[str] = "conflict:"
# The window the two conflict rows read the ledger over. It is the same 4 h the bounded recent history is
# already cut at, so the rows can never ask for evidence the retrieval does not carry.
CONFLICT_TOLD_WINDOW_MS: Final[int] = 4 * 60 * 60_000

# ------------------------------------------------------------------- #675 §1: the decision table's rows
#
# Every name below is a constant, because `override_rule` is the stored answer to "why did the reader get
# this", is folded into a top-10 count map on `status.pipeline`, and is rendered in Chinese by
# `outcome.OVERRIDE_RULE_ZH`. A rule whose name is built by string formatting at the call site cannot be
# any of those things.
RULE_RESTATEMENT: Final[str] = "restatement"
RULE_LISTING_DETERMINISTIC: Final[str] = "listing_deterministic"
RULE_WATCHLIST_OBJECTIVE_GUARD: Final[str] = "watchlist_objective_guard"
RULE_ESCALATE_CORROBORATED: Final[str] = "escalate_corroborated"
RULE_ESCALATE_UNCORROBORATED: Final[str] = "escalate_uncorroborated"
RULE_PRICE_REPORT_WITHOUT_BASIS: Final[str] = "price_report_without_basis"
RULE_CONFLICT_CLAIM_UNCORROBORATED: Final[str] = "conflict_claim_uncorroborated"
RULE_CONFLICT_RUNNING_STORYLINE: Final[str] = "conflict_running_storyline"
RULE_SINGLE_NAME_WITHOUT_INSTRUMENT: Final[str] = "single_name_without_instrument"
RULE_STALE_SOURCE_ARTIFACT: Final[str] = "stale_source_artifact"
# A judgment that carries no `fact_kind` at all. It is not a `statement` -- a statement is an observation
# the model made -- and the ledger may not record an observation nobody made, so the withholding gets its
# own name. Only a replay of an archived `news_judgment_v2` verdict can reach it: `module._assemble`
# builds every model verdict from the typed EventSemantics, and the v3 CHECK refuses a model row without
# a kind.
RULE_FACT_KIND_UNAVAILABLE: Final[str] = "fact_kind_unavailable"
FACT_KIND_RULES: Final[dict[str, str]] = {kind: f"fact_kind_{kind}" for kind in FACT_KINDS}

# `PUSH_FACT_KINDS`, `DROP_FACT_KINDS` and `MATERIAL_FACT_KINDS` are imported from `models` beside the
# kinds they partition, because `review.desk` reads the drop half too and may not import this module.
#
# Where a material change is loud enough to interrupt a reader twice over. Four families, chosen because
# they are the ones whose state changes are about access, safety or the price of money rather than about
# one issuer's own product: a closure, a rate decision, a breach, a venue admitting or delisting an
# instrument. A `financial_results` state change is a push, not an escalate.
ESCALATE_FAMILIES: Final[frozenset[str]] = frozenset(
    {"geopolitical_conflict", "macro_policy_data", "security_operational_incident", "market_access"}
)
# The kinds whose whole claim is a number the text must actually contain. A model that answers
# `level_crossed` for "BTC is up 3%" is not observing the text, and the code can check it.
CONFIRMED_FACT_KINDS: Final[frozenset[str]] = frozenset({"level_crossed", "period_record", "quantified_flow"})

DECISION_TABLE_RULES: Final[tuple[str, ...]] = (
    RULE_FACT_KIND_UNAVAILABLE,
    RULE_ESCALATE_CORROBORATED,
    RULE_ESCALATE_UNCORROBORATED,
    RULE_PRICE_REPORT_WITHOUT_BASIS,
    RULE_CONFLICT_CLAIM_UNCORROBORATED,
    RULE_CONFLICT_RUNNING_STORYLINE,
    *(FACT_KIND_RULES[kind] for kind in FACT_KINDS),
)
# The rows that produce an ordinary push from the text alone. `single_name_without_instrument` applies to
# exactly these: the two objective guards answer a question about the frame rather than about the fact,
# and an escalate is the one class a missing ticker may not silence.
_FACT_KIND_PUSH_RULES: Final[frozenset[str]] = frozenset(FACT_KIND_RULES[kind] for kind in PUSH_FACT_KINDS)


def confirmed_fact_kind(kind: FactKind | None, text: str, markets: frozenset[str]) -> tuple[FactKind | None, bool]:
    """The `fact_kind` a `market_flow_price` report's own text supports, and whether the code moved it.

    Two rules, in this order, and both only on the one family whose codebook definition is "the number
    was printed" (#675 §1.5).

    The owner's exception comes first (#675 §7), because it is an independent admission rather than a
    confirmation of something the model claimed: a same-day move of >= 5% carried by a `commodity` or
    `index` primary is itself the fact the reader wants. A single stock is excluded by name -- the Tencent
    card that opened #675 is +7% and stays withheld -- so the exception is carried by the primary asset's
    market and never by the size of the move.

    It is not, however, carried past the three kinds that say the text is not reporting the move at all
    (#679 review 3). A `recap` is the move told again, a `schedule` is a calendar entry that happens to
    quote one, and a `promotion` is somebody selling something next to one; admitting those on the size
    of a number mentioned anywhere in the text would push exactly the class the audit counted as its
    largest demote bucket. A `statement` stays eligible, because the move is often the thing being
    stated -- "oil settles 6% higher" read as a quote is the mistake the exception exists to correct.

    Then the confirmation. If the model says the number crossed a level, set a period record or moved a
    quantified flow, the text has to say so in either language, or the card is a quote and the answer is
    `statement`. The vocabulary is bilingual because the first cut of it was not: 11 of the 14 cards the
    audit's simulation wrongly dropped were English or a Chinese variant the Chinese-only list did not
    spell.
    """

    if (
        kind not in PRICE_MOVE_EXCEPTION_EXCLUDED_KINDS
        and _states_large_daily_move(text)
        and bool(markets & PRICE_MOVE_EXCEPTION_MARKETS)
    ):
        return "new_quantity", False
    if kind not in CONFIRMED_FACT_KINDS:
        return kind, False
    if price_move_basis(text):
        return kind, False
    return "statement", True


def decision_table_row(
    judgment: ScoredJudgment,
    facts: GateFacts,
    status: StorylineStatus | None,
    *,
    now_ms: int | None = None,
) -> tuple[Decision, str]:
    """The whole model-judgment decision, as one ordered table (#675 §1).

    Every input is a fact the code produced and stored, or an observation of the text the model made and
    the code can check: `fact_kind`, the taxonomy Predictor's four axes, the `source_authority` the
    registry issued from the evidence, the Deduper's count of distinct member texts, the told ledger, and
    the Event's own title. None of them is the model's opinion of the reader, which is the thing #675
    found had no discriminating power at all.

    Rows that need a classification are silent when the taxonomy call failed, and the `fact_kind` rows
    still apply. A classification that does not exist is not evidence of anything -- treating
    `taxonomy_status=unavailable` as "not a price report, not a conflict" would quietly make a Predictor
    outage the loudest card's ally -- but the kind of fact the text states does not depend on it, so a
    taxonomy outage costs precision, never the whole table.
    """

    verdict = judgment.verdict
    editorial = judgment.editorial
    taxonomy = editorial.taxonomy if editorial.taxonomy_status == "available" else None
    text = f"{facts.title}\n{verdict.headline_zh}"
    primary_markets = frozenset(asset.market_type for asset in verdict.assets if asset.role == "primary")

    # The code's own reading of the claim, for the one claim it can read. Outside `market_flow_price` +
    # `reported` the model's answer stands: the check exists because a price quote wearing a level is the
    # single most common way a card reaches a reader with nothing in it, not because the model is
    # generally unreliable about its own vocabulary.
    kind: FactKind | None = verdict.fact_kind
    unconfirmed = False
    if taxonomy is not None and taxonomy.event_family == "market_flow_price" and taxonomy.change_state == "reported":
        kind, unconfirmed = confirmed_fact_kind(kind, text, primary_markets)

    # Row 1. Four kinds of text that are not a new fact about the world. No taxonomy needed and none read:
    # a statement is a statement whether or not the classifier answered.
    if kind in DROP_FACT_KINDS:
        return "drop", RULE_PRICE_REPORT_WITHOUT_BASIS if unconfirmed else FACT_KIND_RULES[kind]

    if kind is None:
        # A model judgment always carries a kind (`module._assemble` builds the verdict from the typed
        # EventSemantics, and the CHECK refuses a v3 model row without one). Nothing but a replay of an
        # archived v2 judgment can arrive here, and it has no observation to decide from. The row is named
        # for the absence rather than folded into `fact_kind_statement`, because the ledger would otherwise
        # record an observation the model never made (#679 review 9).
        return "drop", RULE_FACT_KIND_UNAVAILABLE

    if taxonomy is not None:
        family = taxonomy.event_family
        told_on_key = (
            0 if status is None else status.told_on_key_within(now_ms=now_ms, window_ms=CONFLICT_TOLD_WINDOW_MS)
        )
        material = kind in MATERIAL_FACT_KINDS

        # Row 2. One unverified party's word, carried by one text, from a source the registry cannot name.
        # The #675 refinery card is exactly this: `claimed`, `unknown`, a single member text, 97 minutes
        # after the reader had already been handed the same storyline. A material change is honoured only
        # while the reader is not already two cards into the storyline -- during an escalation the model
        # marks nearly every strike a new state, which is how that card escaped this row in the first place.
        if (
            family == "geopolitical_conflict"
            and editorial.source_authority == "unknown"
            and taxonomy.assertion_status in {"claimed", "rumor"}
            and facts.independent_text_count <= 1
            and (not material or told_on_key >= 2)
        ):
            return "drop", RULE_CONFLICT_CLAIM_UNCORROBORATED

        # Row 3. One more item on a conflict the reader is already reading. A material change still gets
        # through: a ceasefire, a closure, a sanction in effect or a measure an authority took is why the
        # storyline is being followed at all.
        if (
            family == "geopolitical_conflict"
            and str(status.key if status else "").startswith(_CONFLICT_KEY_PREFIX)
            and told_on_key >= 1
            and not material
        ):
            return "drop", RULE_CONFLICT_RUNNING_STORYLINE

        # Row 4. A new state of the world in one of the four families where a state change is about access,
        # safety or the price of money. Corroboration is the code's, not the model's: a second independent
        # member text, or a source the registry can name. 92 of the 126 escalates on 2026-09-02 were one
        # unknown account's single line, which is what this condition exists to keep out of the loudest
        # class (#504 D3). An uncorroborated one is still a push -- it keeps every other right of one.
        if material and family in ESCALATE_FAMILIES:
            corroborated = editorial.source_authority != "unknown" or facts.independent_text_count >= 2
            if corroborated:
                return "escalate", RULE_ESCALATE_CORROBORATED
            return "push", RULE_ESCALATE_UNCORROBORATED

    # Row 5. Everything the text states as a new fact and no row took away.
    return "push", FACT_KIND_RULES[kind]


def decide(
    judgment: ScoredJudgment,
    facts: GateFacts,
    status: StorylineStatus | None,
    *,
    policy: DecidePolicy = DEFAULT_POLICY,
    now_ms: int | None = None,
) -> DecisionResult:
    """Deterministic policy over one current model judgment.

    Runtime policy has no hourly, 2-hour, or 4-hour *reader* quota, no per-storyline delivery budget and no
    operator mute. Once the semantic conditions resolve to push/escalate, a card is withheld only by
    evidence about content the reader already received: a grounded restatement, a stale artifact or a
    same-fact similarity match. v12-v16 also withheld an ordinary push once the reader had received two
    cards on its storyline key inside an hour (#504 D2); v17 deletes that budget. ``now_ms`` is the settle
    stamp the conflict rows of :func:`decision_table_row` measure the told window from; a caller that passes
    none gets the age-blind count. Structured and degraded lanes carry their own ``DecisionResult`` and
    cannot enter this function.

    Order is fixed (#504, #675 §1): restatement drop -> deterministic listing -> watchlist objective guard
    -> :func:`decision_table_row` -> ``single_name_without_instrument`` -> stale source -> similarity. v16
    replaced one step of that order and left the rest of it alone: where v11-v15 read the model's
    `reader_value` and then appended three rows that could downgrade the result, the table now *is* the
    step, and it produces the action and its rule name together.
    """

    if facts.admission in {"telemetry_deterministic", "liquidation_deterministic"}:
        raise ValueError("news_model_decide_structured_admission")
    verdict = judgment.verdict
    baseline = rule_baseline(facts)
    primaries = {_base(a.symbol) for a in verdict.assets if a.role == "primary"}
    grounded = {_base(s) for s in facts.grounded_assets}
    watch_hits = grounded_watchlist_hits(facts)

    template_fact = policy.listing_exempt_from_duplicate and _template_fact(facts)
    # The restatement bypass is narrowed the same way the similarity one is. Exempting the class
    # outright left a listing frame with no duplicate defence at all whenever the similarity check
    # never ran — an `escalate`, or a deployment with `similarity_max: 0`.
    template_restates_other = template_fact and _names_another_instrument(
        template_fact, primaries | grounded, status.told_assets if status else (), verdict.restates
    )
    if policy.restatement_drop and not template_restates_other and grounded_restatement(verdict, status):
        return DecisionResult("drop", RULE_RESTATEMENT, None, baseline, watch_hits)

    final: Decision
    rule: str
    # #523 D1: `listing_deterministic` is the provider's `engine_type=listing` tag, not a content judgment, so
    # the admission alone let marketing, trading-competition and operations notices ride the objective guard.
    # Of 56 listing frames in a 24 h window the model scored 17 `reader_value=none`, and 13 of those reached
    # the reader anyway: a Binance trading competition, a "Rug Pulls explained" explainer, a 35% APR
    # promotion. v16 states that condition as what it always was about: a listing frame whose text is a
    # statement, a recap, a calendar entry or a pitch is not a listing the reader can act on, and it falls
    # through to the table's own drop row. The branch keeps its position, so a real listing notice still wins
    # over the watchlist guard and over every table row.
    if facts.admission == "listing_deterministic" and verdict.fact_kind not in DROP_FACT_KINDS:
        final, rule = "push", RULE_LISTING_DETERMINISTIC
    elif watch_hits:
        final, rule = "push", RULE_WATCHLIST_OBJECTIVE_GUARD
    else:
        final, rule = decision_table_row(judgment, facts, status, now_ms=now_ms)

    # #504 PR-A: a single-name fact with no primary instrument names nothing the reader can trade. This checks
    # only that the verdict names *a* primary — never the instrument universe, which has no Hong Kong venue —
    # so an `02015.HK` primary passes and only influences storyline grouping. The seed (PR-B) is the other
    # half of this rule: it asks the model for the listed ticker whenever the company has one. It applies to
    # the table's own pushes, never to the two objective guards or to an escalate, exactly as under v15.
    if rule in _FACT_KIND_PUSH_RULES and final == "push" and verdict.scope == "single_name" and not primaries:
        final, rule = "drop", RULE_SINGLE_NAME_WITHOUT_INSTRUMENT

    # #154: a replay is not a push, whatever the verdict says about it. `escalate` is exempt for the same reason
    # it is exempt from the similarity check — a false positive is least affordable on the loudest cards.
    if (
        final == "push"
        and policy.stale_source_max_age_s > 0
        and facts.source_age_s is not None
        and facts.source_age_s > policy.stale_source_max_age_s
    ):
        # A constant key on purpose: `throttled_by` is folded into a top-10 count map, so embedding the age
        # would give every withhold its own count-1 bucket and hide the rule from `status.pipeline`. The age
        # itself is in the trace.
        return DecisionResult("throttled", RULE_STALE_SOURCE_ARTIFACT, STALE_SOURCE_KEY, baseline, watch_hits)

    seen_similarity: float | None = None
    seen_against = -1
    seen_scope = ""
    if final in {"push", "escalate"} and status is not None and policy.similarity_max > 0.0:
        seen_scope = "all"
        seen_similarity, seen_against = max_similarity(verdict.headline_zh, status.seen_headlines)
        # An `escalate` is measured but never withheld: a false-positive match is least affordable on the
        # loudest cards. Measuring it is what makes the exemption observable — the 2026-09-01 audit found 11
        # duplicates a day leaving through it with `seen_similarity` unrecorded, so nobody could count them (#491).
        if (
            final == "push"
            and seen_against >= 0
            and seen_similarity >= policy.similarity_max
            and not _seen_flip(verdict.direction, status.seen_directions, seen_against)
            and not _names_another_instrument(template_fact, primaries | grounded, status.seen_assets, seen_against)
        ):
            return DecisionResult(
                "throttled",
                rule,
                f"storyline:{status.key}:seen",
                baseline,
                watch_hits,
                seen_similarity,
                seen_against,
                seen_scope,
            )
    return DecisionResult(final, rule, None, baseline, watch_hits, seen_similarity, seen_against, seen_scope)


def fallback_verdict(facts: GateFacts, *, error_code: str, title: str = "") -> DegradedJudgment:
    """Issue the one degraded presentation and its code-owned objective action."""

    baseline = rule_baseline(facts)
    watch_hits = grounded_watchlist_hits(facts)
    wire_headline = " ".join(str(title or "").split())[:60] or "模型不可用（规则兜底）"
    # No `fact_kind`: the model is what reads the text, and this is the branch where it did not answer.
    # A kind invented here would be a code-owned observation of a text nothing observed (#675 §1).
    verdict = TriageVerdict(
        novelty="new_fact",
        assets=[],
        direction="neutral",
        scope="macro",
        confidence=0.0,
        headline_zh=wire_headline,
        why_zh="",
    )
    if facts.admission == "listing_deterministic":
        rule = "degraded_listing_objective"
    elif watch_hits:
        rule = "degraded_watchlist_objective"
    else:
        rule = "degraded_no_objective_guard"
    decision = DecisionResult(
        final=baseline,
        override_rule=rule,
        throttled_by=None,
        rule_baseline=baseline,
        watchlist_hits=watch_hits,
    )
    return DegradedJudgment(verdict=verdict, decision=decision, error_code=error_code)


def _row_symbols(row: Mapping[str, Any]) -> frozenset[str]:
    """Every symbol a full sent-ledger row was about."""

    symbols = {
        _base(str(value)) for key in ("canonical_assets", "grounded_assets") for value in row.get(key) or () if value
    }
    for asset in row.get("assets") or ():
        symbol = asset.get("symbol") if isinstance(asset, Mapping) else asset
        if symbol:
            symbols.add(_base(str(symbol)))
    return frozenset(symbol for symbol in symbols if symbol)


def storyline_status(
    key: str,
    *,
    told: Sequence[Mapping[str, Any]] = (),
    seen: Sequence[Mapping[str, Any]] | None = None,
) -> StorylineStatus:
    """``told`` is the ledger the model saw (status-bar order); only its directions matter to decide().

    ``seen`` is every card the reader received in the window — the wider set decide() measures a duplicate candidate
    against. It defaults to ``told`` so pure callers and replays that only kept the status bar still work, at the
    cost of a narrower comparison than the worker performs.
    """

    told_directions = tuple(str(t.get("direction") or "") for t in told)
    told_assets = tuple(frozenset(_base(str(value)) for value in t.get("symbols") or () if value) for t in told)
    told_keys = tuple(str(t.get("storyline_key") or "") for t in told)
    told_at_ms = tuple(int(t.get("at_ms") or 0) for t in told)
    rows = list(told if seen is None else seen)
    seen_headlines = tuple(str(r.get("headline_zh") or "") for r in rows)
    seen_event_ids = tuple(str(r.get("event_id") or "") for r in rows)
    seen_directions = tuple(str(r.get("direction") or "") for r in rows)
    seen_assets = told_assets if seen is None else tuple(_row_symbols(r) for r in rows)
    return StorylineStatus(
        key=key,
        told_directions=told_directions,
        told_assets=told_assets,
        told_keys=told_keys,
        told_at_ms=told_at_ms,
        seen_headlines=seen_headlines,
        seen_event_ids=seen_event_ids,
        seen_directions=seen_directions,
        seen_assets=seen_assets,
    )


__all__ = [
    "CONFIRMED_FACT_KINDS",
    "CONFLICT_TOLD_WINDOW_MS",
    "DECISION_TABLE_RULES",
    "DEFAULT_POLICY",
    "DROP_FACT_KINDS",
    "ESCALATE_FAMILIES",
    "FACT_KIND_RULES",
    "MATERIAL_FACT_KINDS",
    "PRICE_MOVE_EXCEPTION_EXCLUDED_KINDS",
    "PRICE_MOVE_EXCEPTION_MARKETS",
    "PRICE_MOVE_EXCEPTION_PERCENT",
    "PUSH_FACT_KINDS",
    "RULE_CONFLICT_CLAIM_UNCORROBORATED",
    "RULE_CONFLICT_RUNNING_STORYLINE",
    "RULE_ESCALATE_CORROBORATED",
    "RULE_ESCALATE_UNCORROBORATED",
    "RULE_FACT_KIND_UNAVAILABLE",
    "RULE_LISTING_DETERMINISTIC",
    "RULE_PRICE_REPORT_WITHOUT_BASIS",
    "RULE_RESTATEMENT",
    "RULE_SINGLE_NAME_WITHOUT_INSTRUMENT",
    "RULE_STALE_SOURCE_ARTIFACT",
    "RULE_WATCHLIST_OBJECTIVE_GUARD",
    "DecidePolicy",
    "DecisionResult",
    "DegradedJudgment",
    "GateFacts",
    "StorylineStatus",
    "confirmed_fact_kind",
    "decide",
    "decision_table_row",
    "fallback_verdict",
    "grounded_restatement",
    "grounded_watchlist_hits",
    "price_move_basis",
    "rule_baseline",
    "storyline_status",
]
