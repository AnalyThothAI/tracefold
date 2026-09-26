# News topics and source authority

The editorial News runtime uses EventUpdate claims, changes and evidence
relations. The former `news_taxonomy_v1` four-axis result
(`subject_codes`, `event_family`, `change_state`,
`assertion_status`) is historical data, not a current model output, policy
gate, API filter or Trading input. A model's wording does not establish that
a source's claim is true. See [News EventUpdate](design/news-event-updates.md)
and [Contracts](CONTRACTS.md) for the current result and read surfaces.

## IPTC navigation topics

`tracefold.news.updates.topics` pins the reviewed IPTC Media Topics codebook.
An EventUpdate may carry up to three known qcodes; the broad parent cannot
be combined with a pinned descendant. These codes help navigation and
notification presentation. They do not collapse a multi-claim article into
one event family or decide whether a claim is new, supported or worth sending.
The topic codebook identity enters the News program identity.

The upstream standard is [IPTC Media Topics](https://iptc.org/standards/media-topics/).
IPTC supplies topic identifiers; it does not supply Tracefold's event
relationships, notification rules or source verification.

## Cited source authority

`tracefold.news.taxonomy` retains the code-owned source-authority
classifier and its exact source-name, handle and domain allowlists. The
current EventUpdate attaches `source_authority` to each cited Source, rather
than assigning one publisher rank to the whole Event. Classification uses
the structured reporting source, exact normalized names or handles, and a
parsed HTTP(S) hostname with a domain boundary. It does not infer authority
from strategy IDs, fuzzy substrings, URL text or another member's identity.

The classes are `regulatory_filing`, `issuer_first_party`,
`reputable_secondary` and `unknown`. A first-party source can establish
that it made a statement, but cannot by itself verify its assertion about a
third party or a future action. A relay, personal account, or belligerent's
state media does not borrow an origin's authority. EventUpdate evidence
relations record which source supports, refutes, reports or does not address
each claim. Reader notification's `key` flag requires corroboration as
well as a qualifying change and topic; the classifier alone does not grant
that flag.

## Historical records

Migration `20260926_0404` leaves old verdict, review and learning rows
untouched. Serve exposes old verdicts as `legacy_verdict`; it does not
translate their taxonomy labels into EventUpdate claims. The current
ReviewDesk reviews the new intent and its evidence. The former taxonomy
Gold/drafter, GEPA optimization, program registry and release/canary
commands are removed from the executable surface. Historical rows remain
audit material and do not activate an old program.
