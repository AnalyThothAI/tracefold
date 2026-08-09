from __future__ import annotations

import gzip
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from tracefold.integrations.news_feeds import (
    NewsFeedAcquisitionError,
    NewsFeedWire,
    RssFeedReader,
    is_public_https_feed_url,
    looks_like_rss_xml,
    parse_rss_feed_wire,
)
from tracefold.news.models import NewsSourceDefinition

NOW_MS = int(datetime(2026, 8, 1, tzinfo=UTC).timestamp() * 1000)
PINNED_WORLDMONITOR_HEAD = "0e8785c43e6a693990a14181ae0a16066c15fc8c"

_PINNED_PARSER_DRIVER = r"""
import fs from 'node:fs';

const source = fs.readFileSync('server/worldmonitor/news/v1/list-feed-digest.ts', 'utf8');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));

const bodyOf = (name) => {
  const index = source.indexOf(`function ${name}(`);
  if (index < 0) throw new Error(`missing ${name}`);
  const parametersStart = source.indexOf('(', index);
  let depth = 1;
  let cursor = parametersStart + 1;
  while (cursor < source.length && depth > 0) {
    if (source[cursor] === '(') depth++;
    else if (source[cursor] === ')') depth--;
    cursor++;
  }
  const bodyStart = source.indexOf('{', cursor);
  depth = 1;
  cursor = bodyStart + 1;
  while (cursor < source.length && depth > 0) {
    if (source[cursor] === '{') depth++;
    else if (source[cursor] === '}') depth--;
    cursor++;
  }
  return source.slice(bodyStart + 1, cursor - 1).replace(/\]!/g, ']');
};

const parser = new Function(`
  const ITEMS_PER_FEED = 5;
  const FUTURE_DATE_TOLERANCE_MS = 60 * 60 * 1000;
  const MAX_DESCRIPTION_LEN = 400;
  const MIN_DESCRIPTION_LEN = 40;
  const DESCRIPTION_TAG_PRIORITY = { rss: ['description', 'content:encoded'], atom: ['summary', 'content'] };
  const DATE_TAG_PRIORITY = {
    rss: ['pubDate', 'dc:date', 'dc:Date.Issued', 'published'],
    atom: ['published', 'updated', 'dc:date', 'dc:Date.Issued'],
  };
  const DESCRIPTION_TAG_REGEX_CACHE = new Map();
  const TAG_REGEX_CACHE = new Map();
  function classifyByKeyword() { return { level: 'info', category: 'general', confidence: 0.5, source: 'keyword' }; }
  function classifyOpinion() { return false; }
  function classifyFeelGood() { return false; }
  function classifyEphemeralLiveCoverage() { return false; }
  const TICKER_DICTIONARY = {};
  function extractTickers() { return []; }
  function decodeNumericReference(codePoint) { ${bodyOf('decodeNumericReference')} }
  function decodeXmlEntities(s) { ${bodyOf('decodeXmlEntities')} }
  function extractRawTagBody(xml, tag) { ${bodyOf('extractRawTagBody')} }
  function normalizeForDescriptionEquality(s) { ${bodyOf('normalizeForDescriptionEquality')} }
  function extractDescription(block, isAtom, title) { ${bodyOf('extractDescription')} }
  function extractTag(xml, tag) { ${bodyOf('extractTag')} }
  function extractFirstDateTag(block, isAtom) { ${bodyOf('extractFirstDateTag')} }
  function parseRssXml(xml, feed, variant) {
    ${bodyOf('parseRssXml')
      .replace('const items: ParsedItem[] = [];', 'const items = [];')
      .replace('let link: string;', 'let link;')}
  }
  return parseRssXml;
`)();

Date.now = () => input.nowMs;
const result = parser(input.xml, { name: 'Test Wire', lang: 'en' }, 'full');
process.stdout.write(JSON.stringify(result));
"""


def _source() -> NewsSourceDefinition:
    return NewsSourceDefinition(
        source_id="news-rss-test",
        name="Test Wire",
        tier=2,
        lang="en",
        source_kind="rss",
        feed_url="https://feed.example.com/rss",
        memberships=("politics",),
    )


def _wire(xml: str) -> NewsFeedWire:
    return NewsFeedWire(
        status_code=200,
        source_name="Test Wire",
        source_lang="en",
        body=xml.encode(),
        etag='"v1"',
        last_modified="Sat, 01 Aug 2026 00:00:00 GMT",
        not_modified=False,
    )


def _public_resolver(_hostname: str) -> tuple[str, ...]:
    return ("93.184.216.34",)


def test_parser_caps_raw_entries_before_applying_acceptance_gates() -> None:
    xml = """
    <rss version="2.0"><channel>
      <item><title></title><pubDate>Sat, 01 Aug 2026 00:00:00 GMT</pubDate></item>
      <item><title>Missing date</title></item>
      <item><title>Invalid date</title><pubDate>not-a-date</pubDate></item>
      <item><title>Too far ahead</title><pubDate>2026-08-01T01:00:00.001Z</pubDate></item>
      <item><guid>kept</guid><title>Kept at tolerance</title><link>javascript:alert(1)</link>
        <pubDate>2026-08-01T01:00:00.000Z</pubDate></item>
      <item><guid>sixth</guid><title>Valid sixth entry must not enter</title>
        <pubDate>Sat, 01 Aug 2026 00:00:00 GMT</pubDate></item>
    </channel></rss>
    """

    fetched = parse_rss_feed_wire(_wire(xml), now_ms=NOW_MS)

    assert [entry.guid for entry in fetched.entries] == ["kept"]
    assert fetched.entries[0].link is None
    assert fetched.entries_seen == 4
    assert fetched.gate_counts == {
        "future_date": 1,
        "invalid_date": 1,
        "missing_date": 1,
        "missing_title": 1,
        "non_http_link": 1,
        "per_feed_cap": 1,
        "undated": 3,
    }


@pytest.mark.parametrize(
    ("root", "entry_tag", "date_tag"),
    (("rss", "item", "pubDate"), ("feed", "entry", "updated")),
)
def test_parser_rejects_feed_when_first_five_entries_have_no_title(
    root: str,
    entry_tag: str,
    date_tag: str,
) -> None:
    titleless = "".join(f"<{entry_tag}><{date_tag}>2026-08-01T00:00:00Z</{date_tag}></{entry_tag}>" for _ in range(5))
    valid_sixth = (
        f"<{entry_tag}><title>Outside the pinned cap</title><{date_tag}>2026-08-01T00:00:00Z</{date_tag}></{entry_tag}>"
    )

    with pytest.raises(NewsFeedAcquisitionError, match="news_rss_parse_no_entries"):
        parse_rss_feed_wire(_wire(f"<{root}>{titleless}{valid_sixth}</{root}>"), now_ms=NOW_MS)


def test_parser_output_matches_pinned_worldmonitor_first_five_fallback() -> None:
    xml = """
    <rss version="2.0"><channel>
      <item><title></title><pubDate>Sat, 01 Aug 2026 00:00:00 GMT</pubDate></item>
      <item><title>Missing date</title></item>
      <item><title>Invalid date</title><pubDate>not-a-date</pubDate></item>
      <item><title>Too far ahead</title><pubDate>2026-08-01T01:00:00.001Z</pubDate></item>
      <item><title>Oil &amp; gas update</title><link>javascript:alert(1)</link>
        <pubDate>2026-08-01T01:00:00.000Z</pubDate>
        <description>
          &lt;p&gt;A sufficiently long description about oil and gas supply conditions.&lt;/p&gt;
        </description>
      </item>
      <item><title>Valid sixth entry must not enter</title>
        <pubDate>Sat, 01 Aug 2026 00:00:00 GMT</pubDate></item>
    </channel></rss>
    """
    local = parse_rss_feed_wire(_wire(xml), now_ms=NOW_MS)
    default_repo = Path(__file__).resolve().parents[2] / "worldmonitor"
    repo = Path(os.environ.get("TRACEFOLD_WORLDMONITOR_REPO", default_repo)).expanduser().resolve()
    if not repo.is_dir():
        pytest.skip("pinned WorldMonitor sibling is not available")
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout.strip()
    assert head == PINNED_WORLDMONITOR_HEAD
    completed = subprocess.run(
        ["node", "--input-type=module", "--eval", _PINNED_PARSER_DRIVER],
        cwd=repo,
        input=json.dumps({"nowMs": NOW_MS, "xml": xml}),
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    pinned = json.loads(completed.stdout)

    assert pinned["parsedTotal"] == 4
    assert pinned["droppedUndated"] == local.gate_counts["undated"] == 3
    assert pinned["droppedFeedCap"] == local.gate_counts["per_feed_cap"] == 1
    assert [
        {
            "source": entry.reporting_origin,
            "title": entry.title,
            "link": entry.link or "",
            "publishedAt": entry.published_at_ms,
            "lang": entry.language,
            "description": entry.description,
        }
        for entry in local.entries
    ] == [
        {
            "source": item["source"],
            "title": item["title"],
            "link": item["link"],
            "publishedAt": item["publishedAt"],
            "lang": item["lang"],
            "description": item["description"],
        }
        for item in pinned["items"]
    ]


def test_parser_ports_rss_atom_dates_entities_and_description_cleanup() -> None:
    rss = """
    <rdf:RDF><item>
      <guid>rss-1</guid>
      <title>Oil &amp; gas &amp;lt;watch&gt;</title>
      <link>https://example.com/story?a=1&amp;b=2</link>
      <dc:Date.Issued>2026-08-01T00:00:00Z</dc:Date.Issued>
      <description><![CDATA[
        <p>A sufficiently long description about the public energy market &amp; its supply.</p>
      ]]></description>
      <content:encoded><![CDATA[<p>Short.</p>]]></content:encoded>
    </item></rdf:RDF>
    """
    rss_result = parse_rss_feed_wire(_wire(rss), now_ms=NOW_MS)

    assert len(rss_result.entries) == 1
    rss_entry = rss_result.entries[0]
    assert rss_entry.title == "Oil & gas &lt;watch>"
    assert rss_entry.link == "https://example.com/story?a=1&b=2"
    assert rss_entry.published_at_ms == NOW_MS
    assert rss_entry.reporting_origin == "Test Wire"
    assert rss_entry.description == "A sufficiently long description about the public energy market & its supply."

    atom = """
    <feed xmlns="http://www.w3.org/2005/Atom"><entry>
      <id>atom-1</id><title><![CDATA[Atom title]]></title>
      <link rel="alternate" href="https://example.com/atom?a=1&amp;b=2" />
      <updated>2026-08-01T00:00:00Z</updated>
      <summary>&lt;p&gt;This Atom summary is intentionally long enough to survive cleanup.&lt;/p&gt;</summary>
    </entry></feed>
    """
    atom_result = parse_rss_feed_wire(_wire(atom), now_ms=NOW_MS)

    assert atom_result.entries[0].guid == "atom-1"
    assert atom_result.entries[0].link == "https://example.com/atom?a=1&amp;b=2"
    assert atom_result.entries[0].description == "This Atom summary is intentionally long enough to survive cleanup."


def test_parser_returns_a_distinct_all_undated_success() -> None:
    fetched = parse_rss_feed_wire(
        _wire("<rss><channel><item><title>No clock</title></item></channel></rss>"),
        now_ms=NOW_MS,
    )

    assert fetched.entries == ()
    assert fetched.entries_seen == 1
    assert fetched.gate_counts == {"missing_date": 1, "per_feed_cap": 0, "undated": 1}


def test_reader_sends_conditional_headers_and_preserves_304_without_a_fact_payload() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(304, headers={"etag": '"v2"'})

    reader = RssFeedReader(
        transport=httpx.MockTransport(handler),
        max_attempts=1,
        resolver=_public_resolver,
    )
    try:
        wire = reader.fetch_wire(
            source=_source(),
            etag='"v1"',
            last_modified="Fri, 31 Jul 2026 00:00:00 GMT",
        )
    finally:
        reader.close()

    assert requests[0].headers["if-none-match"] == '"v1"'
    assert requests[0].headers["if-modified-since"] == "Fri, 31 Jul 2026 00:00:00 GMT"
    assert wire.not_modified is True
    assert wire.etag == '"v2"'
    assert parse_rss_feed_wire(wire).entries == ()


def test_reader_returns_a_single_decoded_gzip_payload() -> None:
    payload = (
        b"<rss><channel><item><title>Decoded once</title>"
        b"<pubDate>Sat, 01 Aug 2026 00:00:00 GMT</pubDate></item></channel></rss>"
    )
    reader = RssFeedReader(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                headers={"content-encoding": "gzip", "content-type": "text/xml; charset=utf-8"},
                content=gzip.compress(payload),
            )
        ),
        max_attempts=1,
        resolver=_public_resolver,
    )
    try:
        wire = reader.fetch_wire(source=_source(), etag=None, last_modified=None)
    finally:
        reader.close()

    assert wire.body == payload
    assert parse_rss_feed_wire(wire, now_ms=NOW_MS).entries[0].title == "Decoded once"


@pytest.mark.parametrize(
    "location",
    ("http://public.example.org/rss", "https://127.0.0.1/rss"),
)
def test_reader_rejects_non_public_redirect_before_following(location: str) -> None:
    requested_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_urls.append(str(request.url))
        if len(requested_urls) == 1:
            return httpx.Response(302, headers={"location": location})
        return httpx.Response(
            200,
            text=(
                "<rss><channel><item><title>Must not be fetched</title>"
                "<pubDate>Sat, 01 Aug 2026 00:00:00 GMT</pubDate></item></channel></rss>"
            ),
        )

    reader = RssFeedReader(
        transport=httpx.MockTransport(handler),
        max_attempts=1,
        resolver=_public_resolver,
    )
    try:
        with pytest.raises(NewsFeedAcquisitionError, match="news_rss_redirect_not_public_https"):
            reader.fetch_wire(source=_source(), etag=None, last_modified=None)
    finally:
        reader.close()

    assert requested_urls == ["https://feed.example.com/rss"]


def test_reader_rejects_initial_hostname_when_any_resolved_address_is_private() -> None:
    requested_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_urls.append(str(request.url))
        raise AssertionError("private-resolved host must not be requested")

    reader = RssFeedReader(
        transport=httpx.MockTransport(handler),
        max_attempts=1,
        resolver=lambda _hostname: ("93.184.216.34", "10.0.0.8"),
    )
    try:
        with pytest.raises(NewsFeedAcquisitionError, match="news_rss_resolved_address_not_public"):
            reader.fetch_wire(source=_source(), etag=None, last_modified=None)
    finally:
        reader.close()

    assert requested_urls == []


def test_reader_rejects_redirect_hostname_that_resolves_to_a_private_address() -> None:
    requested_urls: list[str] = []
    resolved_hosts: list[str] = []

    def resolver(hostname: str) -> tuple[str, ...]:
        resolved_hosts.append(hostname)
        return ("10.0.0.8",) if hostname == "redirected.example.org" else ("93.184.216.34",)

    def handler(request: httpx.Request) -> httpx.Response:
        requested_urls.append(str(request.url))
        if len(requested_urls) == 1:
            return httpx.Response(302, headers={"location": "https://redirected.example.org/rss"})
        return httpx.Response(200, text="<rss></rss>")

    reader = RssFeedReader(
        transport=httpx.MockTransport(handler),
        max_attempts=1,
        resolver=resolver,
    )
    try:
        with pytest.raises(NewsFeedAcquisitionError, match="news_rss_resolved_address_not_public"):
            reader.fetch_wire(source=_source(), etag=None, last_modified=None)
    finally:
        reader.close()

    assert resolved_hosts == ["feed.example.com", "redirected.example.org"]
    assert requested_urls == ["https://feed.example.com/rss"]


def test_reader_follows_public_https_redirect_after_resolving_each_hop() -> None:
    requested_urls: list[str] = []
    resolved_hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_urls.append(str(request.url))
        if len(requested_urls) == 1:
            return httpx.Response(301, headers={"location": "/canonical.xml"})
        return httpx.Response(
            200,
            text=(
                "<rss><channel><item><title>Public redirect</title>"
                "<pubDate>Sat, 01 Aug 2026 00:00:00 GMT</pubDate></item></channel></rss>"
            ),
        )

    def resolver(hostname: str) -> tuple[str, ...]:
        resolved_hosts.append(hostname)
        return ("93.184.216.34",)

    reader = RssFeedReader(
        transport=httpx.MockTransport(handler),
        max_attempts=1,
        resolver=resolver,
    )
    try:
        wire = reader.fetch_wire(source=_source(), etag=None, last_modified=None)
    finally:
        reader.close()

    assert requested_urls == [
        "https://feed.example.com/rss",
        "https://feed.example.com/canonical.xml",
    ]
    assert resolved_hosts == ["feed.example.com", "feed.example.com"]
    assert parse_rss_feed_wire(wire, now_ms=NOW_MS).entries[0].title == "Public redirect"


def test_source_shapes_and_request_attempt_budget_are_hard_cut() -> None:
    with pytest.raises(ValueError, match="news_rss_memberships_required"):
        NewsSourceDefinition(
            source_id="rss-without-membership",
            name="Broken RSS",
            tier=4,
            source_kind="rss",
            feed_url="https://example.com/rss",
        )
    with pytest.raises(ValueError, match="opennews_source_shape_invalid"):
        NewsSourceDefinition(
            source_id="opennews-with-feed",
            name="Broken OpenNews",
            tier=4,
            source_kind="opennews",
            feed_url="https://example.com/rss",
        )
    with pytest.raises(ValueError, match="news_rss_max_attempts_exceeded"):
        RssFeedReader(max_attempts=3)


def test_reader_rejects_html_and_oversized_decoded_bodies() -> None:
    responses = iter(
        (
            httpx.Response(200, text="<!doctype html><html>blocked</html>"),
            httpx.Response(200, content=b"<rss>" + b"x" * 5_000_001 + b"</rss>"),
        )
    )

    reader = RssFeedReader(
        transport=httpx.MockTransport(lambda _request: next(responses)),
        max_attempts=1,
        resolver=_public_resolver,
    )
    try:
        with pytest.raises(NewsFeedAcquisitionError, match="news_rss_non_feed_response"):
            reader.fetch_wire(source=_source(), etag=None, last_modified=None)
        with pytest.raises(NewsFeedAcquisitionError, match="news_rss_body_oversized"):
            reader.fetch_wire(source=_source(), etag=None, last_modified=None)
    finally:
        reader.close()


@pytest.mark.parametrize(
    ("value", "expected"),
    (
        ("https://feeds.example.com/rss", True),
        ("http://feeds.example.com/rss", False),
        ("https://localhost/rss", False),
        ("https://127.0.0.1/rss", False),
        ("https://user:password@feeds.example.com/rss", False),
        ("https://feeds.example.com./rss", False),
    ),
)
def test_public_feed_url_gate(value: str, expected: bool) -> None:
    assert is_public_https_feed_url(value) is expected


def test_feed_shape_sniffer_accepts_rss_atom_rdf_and_rejects_html() -> None:
    assert looks_like_rss_xml("<?xml version='1.0'?><rss version='2.0'>")
    assert looks_like_rss_xml("<?xml version='1.0'?><feed xmlns='urn:atom'>")
    assert looks_like_rss_xml("<?xml version='1.0'?><rdf:RDF>")
    assert not looks_like_rss_xml("<!doctype html><html><rss>not a feed</rss></html>")
