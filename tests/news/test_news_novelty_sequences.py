"""Novelty replays over the frozen #651 sequences, in clock order, through the real policy.

Each case is a read-only production export: the Event, the judgment that was persisted for it, the told
ledger the model was actually shown, and the deliveries that settled. The replay reconstructs the worker's
inputs from that evidence and calls the same `storyline_status` and `decide()` the worker calls. Nothing
here re-implements a policy condition, and no model is invoked: a judgment is either the one production
recorded or one this test controls on purpose, and which it is, is said every time.

The `seen` ledger is the sequence's own receipts rather than the whole 4 h window production measured
against. That is the honest bound of this evidence -- the wider window can only add a withhold, never
remove one -- and it is stated per test where it matters.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from tests.support.news_judgment import scored_judgment, trade_relevance, triage_verdict
from tests.support.news_novelty_sequences import (
    event,
    frozen_policy,
    gate_facts,
    history_row,
    judgment,
    seen_rows,
    sequence,
    sequences,
    settled_at_ms,
    storyline_key,
    told,
    triage_stamp,
)
from tracefold.news.reader_history import build_reader_history
from tracefold.news.similarity import trigram_similarity
from tracefold.news.triage_rules import DEFAULT_POLICY, decide, storyline_status

VISA = "visa_onchain_credit"
CP = "cp_listing_three_venues"


def _steps(sequence_id: str) -> list[dict]:
    return [dict(step) for step in sequence(sequence_id)["steps"]]


def _status(case_key: str, *, delivered: list[str]):
    """The storyline status the worker would have built for this card from those receipts."""

    return storyline_status(
        storyline_key(case_key),
        told=told(case_key),
        seen=seen_rows(delivered, case_key=case_key),
    )


def test_every_sequence_step_names_the_told_entry_it_claims_to_repeat() -> None:
    """The gold target is an index into a real ledger, so the metric can check a `duplicate_of` against it.

    `expected_duplicate_of` is the event the card repeats and `told_index_of_target` is where that event
    sat in this card's own visible ledger. A pair that does not line up would let a later scorer credit a
    citation nobody could have made, so the fixture is checked against the export it was derived from.
    """

    for sequence_document in sequences():
        for step in sequence_document["steps"]:
            case_key = str(step["case"])
            assert str(step["event_id"]) == str(event(case_key)["event_id"])
            assert str(step["storyline_key"]) == storyline_key(case_key)
            index = step["told_index_of_target"]
            target = step["expected_duplicate_of"]
            if index is None:
                assert target is None, case_key
                continue
            entry = told(case_key)[int(index)]
            assert entry["event_id"] == target, case_key
            assert entry["i"] == int(index), case_key


def test_the_replayed_knobs_are_the_shipped_defaults() -> None:
    """#651 §6.3 changes a rule, not a number: `TOLD_MAX`, the windows and `similarity_max` do not move.

    Every frozen arm in these sequences ran the default knobs, so the replay below is the current policy
    over the same values production used rather than the current policy over a tuned one.
    """

    for sequence_document in sequences():
        for step in sequence_document["steps"]:
            assert frozen_policy(str(step["case"])) == DEFAULT_POLICY, step["case"]


def test_the_visa_release_is_dropped_once_it_is_called_a_restatement() -> None:
    """The #630 counterexample, as a controlled label over the frozen evidence.

    `ec2e5a29` and `727ffc0b` are one Visa onchain-credit release carried by two outlets 102 minutes
    apart; the first was delivered and sits at told i=6 of the second. The controlled judgment is the gold
    label -- `restatement` citing 6 -- carrying the direction the first card did *not*: policy v13 read
    that flip as proof of a reversal and let the card through, which is how the reader received the same
    release twice. The similarity check cannot save it either: these two headlines score 0.16.
    """

    first, second = _steps(VISA)
    case_key = str(second["case"])
    entry = told(case_key)[6]
    assert entry["event_id"] == str(first["event_id"]) and entry["direction"] == "bullish"
    assert trigram_similarity(entry["headline_zh"], event(case_key)["comparison_title"]) < 0.25

    status = _status(case_key, delivered=[str(first["case"])])
    controlled = judgment(case_key, novelty="restatement", restates=6, direction="bearish")
    policy = frozen_policy(case_key)
    result = decide(controlled, gate_facts(case_key), status, policy=policy, now_ms=triage_stamp(case_key))

    assert (result.final, result.override_rule, result.throttled_by) == ("drop", "restatement", None)
    assert result.final == second["controlled"]["expected_final_decision"]
    assert result.override_rule == second["controlled"]["expected_override_rule"]

    # And the drop is this guard rather than an accident of another rule: switching the guard off is the
    # only edit, and the same card reaches the reader.
    without_guard = decide(
        controlled,
        gate_facts(case_key),
        status,
        policy=replace(policy, restatement_drop=False),
        now_ms=triage_stamp(case_key),
    )
    assert without_guard.final == "push"


def test_the_recorded_visa_judgment_still_pushes_because_it_claims_a_progression() -> None:
    """The other half of the same card, and the boundary of what this change can prove.

    What actually shipped was `progression`/`restates=-1`, and policy has no standing to contradict a
    novelty label -- so the replayed action is `push`, exactly as recorded. Calling this release a
    progression is the seed's error, and the seed half of #651 §6.3 is NOT VERIFIED here: proving the
    model now labels it a restatement needs a model call, which this suite does not make.
    """

    first, second = _steps(VISA)
    case_key = str(second["case"])
    status = _status(case_key, delivered=[str(first["case"])])
    result = decide(
        judgment(case_key),
        gate_facts(case_key),
        status,
        policy=frozen_policy(case_key),
        now_ms=triage_stamp(case_key),
    )

    assert result.final == second["recorded"]["final_decision"] == "push"
    assert result.override_rule == second["recorded"]["override_rule"]
    assert second["recorded"]["novelty"] == "progression"
    assert second["expected_duplicate_of"] == str(first["event_id"])


def test_the_cp_listing_chain_drops_the_channel_repeat_and_keeps_the_new_venue() -> None:
    """One notice, the same notice on another channel, then a different venue -- under the storyline budget.

    The middle card is the repeat and the third is a real progression: Bithumb listing CP is not Upbit
    listing CP. The three cards share `asset:CP`, so the budget is consulted on the third; it is not
    exhausted, because a dropped card is not a receipt and only one CP card was ever delivered.
    """

    upbit, cross_channel, bithumb = _steps(CP)
    assert settled_at_ms(str(cross_channel["case"])) is None

    for step, delivered, expected in (
        (upbit, [], ("push", "listing_deterministic", None)),
        (cross_channel, [str(upbit["case"])], ("drop", "restatement", None)),
        (bithumb, [str(upbit["case"])], ("push", "listing_deterministic", None)),
    ):
        case_key = str(step["case"])
        status = _status(case_key, delivered=delivered)
        result = decide(
            judgment(case_key),
            gate_facts(case_key),
            status,
            policy=frozen_policy(case_key),
            now_ms=triage_stamp(case_key),
        )
        assert (result.final, result.override_rule, result.throttled_by) == expected, case_key
        assert result.final == step["recorded"]["final_decision"], case_key
        # The receipt ledger the third card was measured against carries the delivered notice and not the
        # dropped one, which is the whole reason the budget had room.
        assert list(status.seen_event_ids) == [str(upbit["event_id"])] * len(delivered)

    assert cross_channel["told_index_of_target"] == 6
    assert told(str(cross_channel["case"]))[6]["event_id"] == str(upbit["event_id"])
    assert bithumb["expected_duplicate_of"] is None


def test_a_reversal_reaches_as_a_progression_and_drops_as_a_restatement() -> None:
    """The split the change rests on, over one pair of frozen texts.

    The cross-channel CP card scores 0.79 against the delivered Upbit card -- far above `similarity_max`,
    so the same-fact check is what decides it. Called a `progression` with the opposite direction it is
    exempt and reaches: `_seen_flip` is untouched, and a real reversal arrives wearing that label. Called
    a `restatement` of the told entry it actually repeats, it drops before the same-fact check is reached.
    The told entry here is `neutral`, so this half is a P2P for the label path rather than for the flip;
    the flip is proven on the Visa pair above, whose told entry is directional.

    The receipt's `direction` is the controlled variable: the Upbit card was judged `neutral`, and a
    neutral row cannot be contradicted, so this holds it `bullish` to make the reversal a real one.
    """

    upbit, cross_channel, _bithumb = _steps(CP)
    case_key = str(cross_channel["case"])
    directional_receipt = {**history_row(str(upbit["case"])), "direction": "bullish"}
    snapshot = build_reader_history(
        [directional_receipt],
        now_ms=triage_stamp(case_key),
        dedupe_family=str(event(case_key)["dedupe_family"] or "general"),
        comparison_fingerprint=str(event(case_key)["comparison_fingerprint"] or ""),
        canonical_assets=[str(value) for value in event(case_key)["grounded_assets"] or ()],
        comparison_title=str(event(case_key)["comparison_title"] or ""),
        include_targeted=False,
    )
    status = storyline_status(
        storyline_key(case_key),
        told=told(case_key),
        seen=[row.as_told_row() for row in snapshot.recent_seen_rows],
    )
    policy = frozen_policy(case_key)
    facts = gate_facts(case_key)

    reversal = decide(
        judgment(case_key, novelty="progression", restates=-1, direction="bearish"),
        facts,
        status,
        policy=policy,
        now_ms=triage_stamp(case_key),
    )
    assert reversal.seen_similarity is not None and reversal.seen_similarity >= policy.similarity_max
    assert (reversal.final, reversal.throttled_by) == ("push", None)

    repeat = decide(
        judgment(case_key, novelty="restatement", restates=6, direction="bearish"),
        facts,
        status,
        policy=policy,
        now_ms=triage_stamp(case_key),
    )
    assert (repeat.final, repeat.override_rule) == ("drop", "restatement")


def test_a_reversal_still_escapes_an_exhausted_storyline_budget() -> None:
    """The second exemption #651 §6.3 keeps, isolated from the first.

    Two CP cards were delivered on `asset:CP` inside the budget window and the newest directional one is
    `bullish`, so an ordinary third card is withheld. A `progression` that reverses it is not -- that is
    the #504 D2 / #523 D2 rule, and dropping the restatement exemption does not touch it. `similarity_max`
    is set to zero here, which is the documented way to switch the same-fact check off, so that the only
    rule this test can be measuring is the budget.
    """

    upbit, cross_channel, bithumb = _steps(CP)
    case_key = str(cross_channel["case"])
    policy = replace(frozen_policy(case_key), similarity_max=0.0)
    now_ms = int(bithumb["settled_at_ms"]) + 60_000
    receipts = [history_row(str(upbit["case"])), history_row(str(bithumb["case"]))]
    assert {row["storyline_key"] for row in receipts} == {"asset:CP"}
    assert [row["direction"] for row in receipts] == ["neutral", "bullish"]

    snapshot = build_reader_history(
        receipts,
        now_ms=now_ms,
        dedupe_family=str(event(case_key)["dedupe_family"] or "general"),
        comparison_fingerprint=str(event(case_key)["comparison_fingerprint"] or ""),
        canonical_assets=[str(value) for value in event(case_key)["grounded_assets"] or ()],
        comparison_title=str(event(case_key)["comparison_title"] or ""),
        include_targeted=False,
    )
    status = storyline_status(
        storyline_key(case_key),
        told=told(case_key),
        seen=[row.as_told_row() for row in snapshot.recent_seen_rows],
    )
    facts = gate_facts(case_key)

    withheld = decide(
        judgment(case_key, novelty="progression", restates=-1, direction="neutral"),
        facts,
        status,
        policy=policy,
        now_ms=now_ms,
    )
    assert (withheld.final, withheld.throttled_by) == ("throttled", "storyline:asset:CP:budget")

    reversal = decide(
        judgment(case_key, novelty="progression", restates=-1, direction="bearish"),
        facts,
        status,
        policy=policy,
        now_ms=now_ms,
    )
    assert (reversal.final, reversal.throttled_by) == ("push", None)


_MACRO_PAIRS = (
    # Two countries' releases of the same indicator. The pair scores 0.27 on character trigrams, above
    # `similarity_max` -- which is why the 4 h receipt ledger and the wider 48 h `told` evidence are
    # separate sets, and why these rows are `told` only here (see `ReaderHistorySnapshot`).
    ("英国8月制造业PMI终值51.7", "美国8月标普全球制造业PMI终值53.9"),
    # One trade story, two different traded quantities.
    ("中国8月原油进口同比增4.3%", "中国8月成品油出口同比降11%"),
    # One series, two statistical periods.
    ("美国二季度GDP终值上修至2.6%", "美国三季度GDP初值1.8%"),
)


@pytest.mark.parametrize(("told_headline", "candidate_headline"), _MACRO_PAIRS)
def test_different_economic_events_in_one_storyline_reach_the_reader_as_new_facts(
    told_headline: str, candidate_headline: str
) -> None:
    """Policy cannot tell these apart, so the seed has to.

    Each pair sits under one macro storyline and shares most of its wording, and the only thing that
    decides whether the second reaches the reader is the novelty label the model wrote. With `new_fact`
    the card is delivered; with `restatement` citing the row beside it, the same card is dropped by the
    same guard that drops a real repeat. That asymmetry is why "two different economic events are not one
    fact because one storyline covers both" is a sentence in the instruction and not a rule in the code.
    """

    now_ms = 1_788_800_000_000
    key = "topic:macro_data"
    rows = [
        {
            "i": 0,
            "event_id": "a" * 64,
            "at_ms": now_ms - 6 * 3_600_000,
            "storyline_key": key,
            "comparison_title": told_headline,
            "symbols": [],
            "magnitude": 2,
            "direction": "neutral",
            "headline_zh": told_headline,
            "why_zh": "",
        }
    ]
    # `told` only: the earlier print is six hours old, outside the 4 h receipt ledger `decide()` measures
    # duplicates against and inside the 48 h evidence the model reads.
    status = storyline_status(key, told=rows, seen=[])
    facts = gate_facts("cp_upbit_first")
    verdict = triage_verdict(
        novelty="new_fact",
        restates=-1,
        assets=[],
        scope="macro",
        direction="neutral",
        headline_zh=candidate_headline,
        why_zh="宏观数据更新。",
    )
    relevance = trade_relevance(impact_breadth="global_systemic", channels=["rates"], affected_markets=["rates"])

    new_fact = decide(
        scored_judgment(verdict, relevance=relevance),
        replace(facts, admission="candidate"),
        status,
        policy=DEFAULT_POLICY,
        now_ms=now_ms,
    )
    assert (new_fact.final, new_fact.throttled_by) == ("push", None)

    mislabelled = decide(
        scored_judgment(verdict.model_copy(update={"novelty": "restatement", "restates": 0}), relevance=relevance),
        replace(facts, admission="candidate"),
        status,
        policy=DEFAULT_POLICY,
        now_ms=now_ms,
    )
    assert (mislabelled.final, mislabelled.override_rule) == ("drop", "restatement")
