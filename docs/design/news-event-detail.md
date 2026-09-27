# News Event detail design review

Issue [#722](https://github.com/AnalyThothAI/tracefold/issues/722) follows the claim-count
and repeated-change explanation in [#720](https://github.com/AnalyThothAI/tracefold/issues/720),
delivered by [PR #721](https://github.com/AnalyThothAI/tracefold/pull/721).
The [interactive HTML prototype](news-event-detail-prototype.html) was prepared before
editing the React page. It uses sample content based on one Event, so its shell and
market values are illustrative rather than a live data view.

## Reading task

The reader should be able to answer four questions in order: what happened in this
Event, how many current claims it contains, why a notification was or was not sent,
and which source supports each claim. Historical comparisons remain inspectable
without appearing as more current claims. Rolling quotes should not look like the
Event's measured market response.

## Interaction decisions

- Start with the headline, outcome and a short count of current claims and sources.
- Place each current claim in one numbered reading unit with its first citation.
  Disclose historical comparisons, structured fields and further citations inside
  that unit. A change badge describes a relation; it is not another current claim.
- Keep the notification reason next to the content on desktop and directly after it
  on a narrow viewport. Link each decision back to its claim and retain the full
  processing record below.
- Use a small page directory to reach sources, processing and market data. Source
  relations and disagreements remain visible and the full source text is disclosed.
- Separate current rolling quotes from the Event anchored reaction. The existing
  shared shell and tokens determine final application details.

## Review images

|                                    | Desktop, 1440 px                                              | Mobile, 390 px                                              |
| ---------------------------------- | ------------------------------------------------------------- | ----------------------------------------------------------- |
| Prototype                          | ![Desktop prototype](news-event-detail-prototype-desktop.png) | ![Mobile prototype](news-event-detail-prototype-mobile.png) |
| React preview with live Event data | ![Desktop preview](news-event-detail-preview-desktop.png)     | ![Mobile preview](news-event-detail-preview-mobile.png)     |

The preview images show the local Vite app reading the existing Serve API for Event
`cf4ba24aa1221d9e5c03580685d0328184b5349cc8495db7d74824833e34a70d`.
They record a moment in time; the Event and quotes may change later. The React
preview keeps the full workbench chrome and renders the API's actual labels, which
accounts for visible differences from the prototype.
