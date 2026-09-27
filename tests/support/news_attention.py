"""Deterministic editorial stand-in for notification seam tests."""

from tracefold.news.updates.attention import AttentionAssessment, AttentionDecision


class NotifyAll:
    identity = "fixture_attention_notify_all"

    async def assess(self, claims, *, sources, watch_symbols):
        return AttentionAssessment(
            decisions=tuple(AttentionDecision(claim_ref=claim.ref, disposition="notify") for claim in claims)
        )


class FeedOnly(NotifyAll):
    identity = "fixture_attention_feed_only"

    async def assess(self, claims, *, sources, watch_symbols):
        return AttentionAssessment(
            decisions=tuple(AttentionDecision(claim_ref=claim.ref, disposition="feed_only") for claim in claims)
        )
