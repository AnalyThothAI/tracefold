"""Historical sibling claims are audited and retired as knowledge, not rewritten evidence."""

from __future__ import annotations

import pytest

from tests.support.news_head_scope import historical_head
from tests.support.news_update_semantic import STAMP
from tracefold.news.storage.head_scope_repairs import audit_head_scope, audit_scope_rows
from tracefold.news.updates.public import public_updates
from tracefold.news.updates.scope_repair import retract_out_of_scope


def test_scope_audit_and_retraction_publish_only_the_invalidated_claim() -> None:
    head, row = historical_head()
    report = audit_scope_rows([row])
    assert report["heads"] == report["affected_heads"] == 1
    assert report["outside_active_claims"] == 1
    assert report["unresolved_active_claims"] == 0
    proof = audit_head_scope(row)
    bad_ref = next(claim.ref for claim in head.claims if claim.statement.startswith("Beta"))
    assert [item["claim_ref"] for item in proof["outside"]] == [bad_ref]
    corrected = retract_out_of_scope(head, (bad_ref,), adopted_at_ms=STAMP + 2)
    assert corrected.previous_content_revision == head.content_revision
    assert corrected.retired_claim_refs == (bad_ref,)
    assert corrected.topics == ("alpha",)
    assert corrected.evidence == head.evidence and corrected.claims == head.claims
    assert [(change.kind, change.previous_ref) for change in corrected.changes] == [("scope_retraction", bad_ref)]
    (public,) = public_updates(corrected, semantic_completed_at_ms=STAMP + 2)
    assert public.kind == "source_update"
    assert public.retired_claim_refs == public.affected_claim_refs == (bad_ref,)
    assert public.claim_refs == (bad_ref,)
    row["document"] = corrected.model_dump(mode="json")
    assert audit_head_scope(row)["outside"] == []
    with pytest.raises(ValueError, match="news_scope_repair_claims_not_active"):
        retract_out_of_scope(corrected, (bad_ref,), adopted_at_ms=STAMP + 3)
