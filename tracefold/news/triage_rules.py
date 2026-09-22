"""The ordinary model policy and its code-owned degraded fallback.

``TRIAGE_POLICY_VERSION`` is ``news_triage_policy_v15`` (#675 §3). v15 adds the decision table below: three
ordered rows that run after the realtime branch has already resolved to push, read facts the code owns
rather than the model's own reading of them, and can only ever downgrade that push to ``drop``. Nothing
else about v14 moves -- the escalate, listing-deterministic and watchlist-objective paths are byte-identical,
the storyline budget and the bigram threshold are untouched -- so a v14 and a v15 replay of the same Event
differ in exactly the rows named here.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field, fields
from typing import Any, Final

from .artifact_identity import canonical_sha
from .events.storyline import NO_STORYLINE_KEY
from .models import Decision, TriageVerdict, base_symbol
from .program.contracts import JUDGMENT_CONTRACT_VERSION, ScoredJudgment, TradeRelevanceV1
from .similarity import max_similarity

# One owner for the withhold key: `outcome` renders it, `repository` counts it.
STALE_SOURCE_KEY: Final = "artifact:stale"

_DIRECTIONAL = frozenset({"bullish", "bearish"})


@dataclass(frozen=True, slots=True)
class DecidePolicy:
    """The six safety/duplicate/budget knobs exposed through ``news.policy``.

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
    # #504 D2: the per-storyline marginal budget, which withdraws policy v7's "no storyline quota" decision. It
    # is a content rule, not a reader quota: it counts cards the reader actually received *on this storyline
    # key* inside the window, exempts a direction reversal and a corroborated `escalate`, and never touches a
    # `none` key (which is not a storyline, just "the registry matched nothing"). On the 2026-09-02 day the
    # model's own novelty judgment let 225 of 355 geopolitical pushes through with >= 8 same-storyline cards
    # already in the told ledger; the p95 storyline-hour was 17 cards. Either knob at 0 switches it off.
    storyline_budget_window_s: int = 3600
    storyline_budget_max: int = 2

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
    # #504 D3: how many provider Items the Deduper merged into this Event. A second independent arrival is the
    # cheapest corroboration there is; a single Item from a source of unknown authority is none.
    member_count: int = 1
    # #675 §3: how many *distinct member texts* the Deduper merged, counted over `evidence_text_sha256`. It is
    # not `member_count` and it is not a cosmetic refinement of it: the #675 Tencent card had two members and
    # both were the same jin10 line arriving twice, so `member_count` read 2 and called one wire two parties.
    # A digest over the normalized provider body is the only thing that can tell a second arrival from a
    # second source. The escalate corroboration rule deliberately still reads `member_count` (see `decide`).
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
    # #504 D2: when each remembered card settled and which final storyline key it settled under, same order.
    # This is the budget ledger — still receipt evidence about *what the reader got*, never a capacity counter:
    # a key appears here only because a card on it was proven delivered.
    seen_at_ms: tuple[int, ...] = ()
    seen_keys: tuple[str, ...] = ()

    @property
    def told_count(self) -> int:
        return len(self.told_directions)

    def told_on_key_within(self, *, now_ms: int | None, window_ms: int) -> int:
        """Shown ledger entries on exactly this storyline key inside the window.

        Exact string equality on the key, the same comparison the storyline budget counts receipts with and
        for the same reason: this counts what the reader was handed on *this* key, and the market-aware
        comparison retrieval uses is deliberately inclusive. A caller with no clock (a pure replay that kept
        no stamp) gets the age-blind count, which is what the ledger's own 4 h recency bound already is.
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


def realtime_eligible(verdict: TriageVerdict, relevance: TradeRelevanceV1) -> bool:
    """The code-owned trade-attention eligibility predicate from policy v11."""

    direct_surface = (
        relevance.tradability in {"direct", "second_order"}
        and bool(relevance.channels)
        and bool(relevance.affected_markets)
    )
    material_change = relevance.development_delta == "state_change" or (
        relevance.development_delta == "material_detail"
        and (relevance.tradability == "direct" or relevance.surprise in {"unscheduled", "material_vs_expectation"})
    )
    return verdict.magnitude >= 2 and direct_surface and material_change


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
    `progression` or `new_fact`, and it is those two that ``_seen_flip`` and ``_budget_exhausted`` still
    protect against the similarity check and the storyline budget.
    """

    if verdict.novelty != "restatement" or status is None or status.told_count == 0:
        return False
    return 0 <= verdict.restates < status.told_count


_CONFLICT_KEY_PREFIX: Final[str] = "conflict:"
# The window the two conflict rows read the ledger over. It is the same 4 h the bounded recent history is
# already cut at, so the rows can never ask for evidence the retrieval does not carry.
CONFLICT_TOLD_WINDOW_MS: Final[int] = 4 * 60 * 60_000
DECISION_TABLE_RULES: Final[tuple[str, ...]] = (
    "price_report_without_basis",
    "conflict_claim_uncorroborated",
    "conflict_running_storyline",
)


def decision_table_row(
    judgment: ScoredJudgment,
    facts: GateFacts,
    status: StorylineStatus | None,
    *,
    now_ms: int | None = None,
) -> str | None:
    """The first v15 row that withholds this already-resolved realtime push, or ``None`` (#675 §3).

    Every input is a fact the code produced and stored before `decide()` ran: the taxonomy Predictor's four
    axes, the `source_authority` the registry issued from the evidence, the Deduper's count of distinct
    member texts, the told ledger, and the Event's own text. None of them is the model's opinion of the
    reader, which is the thing #675 found had no discriminating power at all.

    The whole table is silent when the taxonomy call failed. A classification that does not exist is not
    evidence of anything, and the alternative -- treating `taxonomy_status=unavailable` as "not a price
    report, not a conflict" -- would quietly make a Predictor outage the loudest card's ally.
    """

    editorial = judgment.editorial
    taxonomy = editorial.taxonomy
    if editorial.taxonomy_status != "available" or taxonomy is None:
        return None
    verdict = judgment.verdict
    relevance = editorial.relevance
    family = taxonomy.event_family
    text = f"{facts.title}\n{verdict.headline_zh}"

    # Row 1. A `market_flow_price` fact whose change_state is `reported` is, by the codebook's own
    # definition, "the number was printed". Unless the text says what about the number is new, the reader is
    # being woken for a quote. The exception is the owner's, and it is deliberately narrow.
    if family == "market_flow_price" and taxonomy.change_state == "reported":
        markets = {asset.market_type for asset in verdict.assets if asset.role == "primary"}
        exception = _states_large_daily_move(text) and bool(markets & PRICE_MOVE_EXCEPTION_MARKETS)
        if not price_move_basis(text) and not exception:
            return "price_report_without_basis"

    if family != "geopolitical_conflict":
        return None
    told_on_key = 0 if status is None else status.told_on_key_within(now_ms=now_ms, window_ms=CONFLICT_TOLD_WINDOW_MS)
    state_change = relevance.development_delta == "state_change"

    # Row 2. One unverified party's word, carried by one text, from a source the registry cannot name. The
    # #675 refinery card is exactly this: `claimed`, `unknown`, a single member text, 97 minutes after the
    # reader had already been handed the same storyline. `state_change` is the model's own reading and is
    # honoured only while the reader is not already two cards into the storyline -- during an escalation the
    # model marks nearly every strike a state change, which is how it escaped this row in the first place.
    if (
        editorial.source_authority == "unknown"
        and taxonomy.assertion_status in {"claimed", "rumor"}
        and facts.independent_text_count <= 1
        and (not state_change or told_on_key >= 2)
    ):
        return "conflict_claim_uncorroborated"

    # Row 3. One more item on a conflict the reader is already reading. A state change still gets through:
    # a ceasefire, a closure or a sanction in effect is why the storyline is being followed at all.
    if str(status.key if status else "").startswith(_CONFLICT_KEY_PREFIX) and told_on_key >= 1 and not state_change:
        return "conflict_running_storyline"
    return None


def _budget_exhausted(direction: str, status: StorylineStatus, *, now_ms: int, window_ms: int, budget_max: int) -> bool:
    """True when the reader already received ``budget_max`` cards on this storyline inside the window and this
    one does not reverse the newest *directional* card among them (#504 D2, narrowed by #523 D2).

    Rows are newest first. Every in-window row on the key counts against ``budget_max`` — a neutral card is
    still a card the reader received — but the reversal comparison skips rows the reader could not have read a
    direction from: only a `bullish`/`bearish` row can be contradicted, so neutral, unclear and direction-less
    rows are passed over rather than ending the search (same test as ``_seen_flip``). Comparing against the
    newest row whatever its direction hid real reversals behind one neutral card: "Russia will raise output"
    was withheld against a "will cut output" card 55 minutes earlier because an unrelated neutral card had
    landed on the key between them. Only the newest directional card is consulted, never any older one:
    "against any delivered card" let 101 more cards through and 10 escape on one key in one hour. The ``none``
    key is exempt: it is not a storyline but "the registry matched nothing", and counting it withheld Chile's
    GDP print behind an RBNZ decision in the 2026-09-02 replay (#509 D6).

    The key comparison is exact string equality, deliberately, and it is *not* `same_storyline_key`. This
    counts receipts — how many cards the reader was proven to have received on exactly this key — and the
    market-aware comparison retrieval uses is inclusive, so borrowing it here would withhold cards on the
    strength of a card about a different instrument. One consequence is real and bounded: `asset:` keys
    gained their market in #651 §6.2, so for one budget window after that cutover a card on
    `asset:crypto:SEI` does not count the cards delivered under the untyped `asset:SEI`, and the budget
    restarts. Making it not restart is a policy decision and a policy version, not a mechanical one.
    """

    if status.key == NO_STORYLINE_KEY:
        return False
    delivered = 0
    latest_direction: str | None = None
    for index, key in enumerate(status.seen_keys):
        if key != status.key or index >= len(status.seen_at_ms):
            continue
        if now_ms - int(status.seen_at_ms[index]) > window_ms:
            continue
        if latest_direction is None and index < len(status.seen_directions):
            told = status.seen_directions[index]
            if told in _DIRECTIONAL:
                latest_direction = told
        delivered += 1
    if delivered < budget_max:
        return False
    flipped = direction in _DIRECTIONAL and latest_direction in _DIRECTIONAL and latest_direction != direction
    return not flipped


def decide(
    judgment: ScoredJudgment,
    facts: GateFacts,
    status: StorylineStatus | None,
    *,
    policy: DecidePolicy = DEFAULT_POLICY,
    now_ms: int | None = None,
) -> DecisionResult:
    """Deterministic policy over one current model judgment.

    Runtime policy has no hourly, 2-hour, or 4-hour *reader* quota and no operator mute. Once the semantic
    conditions resolve to push/escalate, a card is withheld only by evidence about content the reader already
    received: a grounded restatement, a stale artifact, a same-fact similarity match, or — policy v12 — the
    per-storyline marginal budget, which counts delivered cards on this storyline key inside a window.
    ``now_ms`` is the settle stamp the window is measured from; a caller that passes none opts out of the budget
    (a pure caller with no ledger time has nothing to measure). Structured and degraded lanes carry their own
    ``DecisionResult`` and cannot enter this function.

    Order is fixed (#504): restatement drop -> admission / reader_value branch -> escalate corroboration ->
    ``single_name_without_instrument`` -> decision table (v15) -> stale source -> similarity -> storyline
    budget. Policy v13 changes one condition inside that branch and none of its order: the deterministic
    listing guard no longer covers a frame the model marked ``reader_value=none``, which falls through to the
    ``reader_value_none`` drop. Policy v15 (#675) appends :func:`decision_table_row` after the single-name
    rule; it reads only the realtime push and only withholds, so every other path is unchanged from v14.
    """

    if facts.admission in {"telemetry_deterministic", "liquidation_deterministic"}:
        raise ValueError("news_model_decide_structured_admission")
    verdict = judgment.verdict
    relevance = judgment.editorial.relevance
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
        return DecisionResult("drop", "restatement", None, baseline, watch_hits)

    final: Decision
    rule: str | None
    # #523 D1: `listing_deterministic` is the provider's `engine_type=listing` tag, not a content judgment, so
    # the admission alone let marketing, trading-competition and operations notices ride the objective guard.
    # Of 56 listing frames in a 24 h window the model scored 17 `reader_value=none`, and 13 of those reached
    # the reader anyway: a Binance trading competition, a "Rug Pulls explained" explainer, a 35% APR
    # promotion. A frame the model itself scored `reader_value=none` is the one case where the tag is provably
    # not about a listing the reader can act on, and it is the only condition added: the branch keeps its
    # position, so a real listing notice still wins over the watchlist guard and over every model rule, and
    # `background` still pushes (moving the branch instead cost four genuine listings in the same replay).
    if facts.admission == "listing_deterministic" and relevance.reader_value != "none":
        final, rule = "push", "listing_deterministic"
    elif watch_hits:
        final, rule = "push", "watchlist_objective_guard"
    elif relevance.reader_value == "escalate" and realtime_eligible(verdict, relevance):
        final, rule = "escalate", "trade_relevance_escalate"
    elif relevance.reader_value == "realtime" and realtime_eligible(verdict, relevance):
        final, rule = "push", "trade_relevance_realtime"
    elif relevance.reader_value in {"background", "none"}:
        final, rule = "drop", f"reader_value_{relevance.reader_value}"
    else:
        final, rule = "drop", "trade_relevance_inconsistent"

    # #504 D3: an `escalate` needs corroboration the model cannot supply. `source_authority` is the code-owned
    # editorial field issued once from the evidence (`taxonomy.py`), and `member_count` is the Deduper's count of
    # independent arrivals. Unknown source *and* a single Item is a claim, not a fact the reader should be woken
    # for: 92 of the 126 escalates on 2026-09-02 were exactly that (an Iranian MP's statement on a Telegram
    # channel was the first v9 escalate). The card keeps every other right of a push. Grounded assets are not
    # corroboration: a provider tag proves which instrument is mentioned, not that a second party confirmed it.
    # #651 §5.3 is why it reads the envelope rather than `editorial.taxonomy.source_authority`: the taxonomy
    # Predictor can fail on its own now, and the loudest card class must not lose its corroboration rule to a
    # classification failure that says nothing about who reported the fact.
    if (
        rule == "trade_relevance_escalate"
        and judgment.editorial.source_authority == "unknown"
        and facts.member_count <= 1
    ):
        final, rule = "push", "trade_relevance_escalate_uncorroborated"

    # #504 PR-A: a single-name fact with no primary instrument names nothing the reader can trade. This checks
    # only that the verdict names *a* primary — never the instrument universe, which has no Hong Kong venue —
    # so an `02015.HK` primary passes and only influences storyline grouping. The seed (PR-B) is the other
    # half of this rule: it asks the model for the listed ticker whenever the company has one.
    if rule == "trade_relevance_realtime" and verdict.scope == "single_name" and not primaries:
        final, rule = "drop", "single_name_without_instrument"

    # #675 §3: the decision table. It sees only a push the *realtime* branch produced -- an escalate, a
    # deterministic listing and the watchlist objective guard reach the reader under v15 exactly as they did
    # under v14 -- and it can only withhold, never admit. The result is a `drop` under the row's own name
    # rather than a `throttled`: a throttle says "the reader got this already", and these three say "this is
    # background", which is the same thing `reader_value_background` says one branch earlier.
    if rule == "trade_relevance_realtime" and final == "push":
        row = decision_table_row(judgment, facts, status, now_ms=now_ms)
        if row is not None:
            return DecisionResult("drop", row, None, baseline, watch_hits)

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
        return DecisionResult("throttled", "stale_source_artifact", STALE_SOURCE_KEY, baseline, watch_hits)

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
    # #504 D2, the last throttle. Only an ordinary push: an `escalate` that survived D3 is corroborated and is
    # the card the budget exists to make room for.
    if (
        final == "push"
        and status is not None
        and now_ms is not None
        and policy.storyline_budget_window_s > 0
        and policy.storyline_budget_max > 0
        and _budget_exhausted(
            verdict.direction,
            status,
            now_ms=now_ms,
            window_ms=policy.storyline_budget_window_s * 1000,
            budget_max=policy.storyline_budget_max,
        )
    ):
        return DecisionResult(
            "throttled",
            rule,
            f"storyline:{status.key}:budget",
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
    verdict = TriageVerdict(
        novelty="new_fact",
        assets=[],
        direction="neutral",
        scope="macro",
        magnitude=0,
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
    seen_at_ms = tuple(int(r.get("at_ms") or 0) for r in rows)
    seen_keys = tuple(str(r.get("storyline_key") or "") for r in rows)
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
        seen_at_ms=seen_at_ms,
        seen_keys=seen_keys,
    )


__all__ = [
    "CONFLICT_TOLD_WINDOW_MS",
    "DECISION_TABLE_RULES",
    "DEFAULT_POLICY",
    "PRICE_MOVE_EXCEPTION_MARKETS",
    "PRICE_MOVE_EXCEPTION_PERCENT",
    "DecidePolicy",
    "DecisionResult",
    "DegradedJudgment",
    "GateFacts",
    "StorylineStatus",
    "decide",
    "decision_table_row",
    "fallback_verdict",
    "grounded_restatement",
    "grounded_watchlist_hits",
    "price_move_basis",
    "realtime_eligible",
    "rule_baseline",
    "storyline_status",
]
