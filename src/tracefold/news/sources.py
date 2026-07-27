"""Frozen WorldMonitor ``full/en`` inventory plus Tracefold crypto sources."""

from __future__ import annotations

import hashlib
import re
from typing import Final, TypedDict

from .models import NewsSourceDefinition

WORLDMONITOR_COMMIT: Final = "f73de5b7dde76ff292f800d7d06f3529d2178d43"

# category, display name, URL, WorldMonitor source tier
_WM_FULL_EN: Final[tuple[tuple[str, str, str, int], ...]] = (
    ("politics", "BBC World", "https://feeds.bbci.co.uk/news/world/rss.xml", 2),
    ("politics", "Guardian World", "https://www.theguardian.com/world/rss", 2),
    (
        "politics",
        "AP News",
        "https://news.google.com/rss/search?q=site%3Aapnews.com%20when%3A1d&hl=en-US&gl=US&ceid=US:en",
        1,
    ),
    (
        "politics",
        "Reuters World",
        "https://news.google.com/rss/search?q=site%3Areuters.com%20world%20when%3A1d&hl=en-US&gl=US&ceid=US:en",
        1,
    ),
    (
        "politics",
        "CNN World",
        "https://news.google.com/rss/search?q=site%3Acnn.com%20world%20news%20when%3A1d&hl=en-US&gl=US&ceid=US:en",
        2,
    ),
    ("politics", "Trump - Truth Social", "https://trumpstruth.org/feed", 4),
    (
        "us",
        "Reuters US",
        "https://news.google.com/rss/search?q=site%3Areuters.com%20US%20when%3A1d&hl=en-US&gl=US&ceid=US:en",
        1,
    ),
    ("us", "NPR News", "https://feeds.npr.org/1001/rss.xml", 2),
    ("us", "PBS NewsHour", "https://www.pbs.org/newshour/feeds/rss/headlines", 2),
    ("us", "ABC News", "https://feeds.abcnews.com/abcnews/topstories", 2),
    ("us", "CBS News", "https://www.cbsnews.com/latest/rss/main", 2),
    ("us", "NBC News", "https://feeds.nbcnews.com/nbcnews/public/news", 2),
    ("us", "Wall Street Journal", "https://feeds.content.dowjones.io/public/rss/RSSUSnews", 1),
    ("us", "Politico", "https://rss.politico.com/politics-news.xml", 2),
    ("us", "The Hill", "https://thehill.com/news/feed", 3),
    ("us", "Axios", "https://api.axios.com/feed/", 2),
    ("europe", "France 24", "https://www.france24.com/en/rss", 2),
    ("europe", "EuroNews", "https://www.euronews.com/rss?format=xml", 2),
    ("europe", "Le Monde", "https://www.lemonde.fr/en/rss/une.xml", 2),
    ("europe", "DW News", "https://rss.dw.com/xml/rss-en-all", 2),
    ("europe", "Balkan Insight", "https://balkaninsight.com/feed/", 1),
    ("middleeast", "BBC Middle East", "https://feeds.bbci.co.uk/news/world/middle_east/rss.xml", 2),
    ("middleeast", "Al Jazeera", "https://www.aljazeera.com/xml/rss/all.xml", 2),
    ("middleeast", "Guardian ME", "https://www.theguardian.com/world/middleeast/rss", 2),
    ("middleeast", "Oman Observer", "https://www.omanobserver.om/rssFeed/1", 4),
    ("middleeast", "The National", "https://www.thenationalnews.com/arc/outboundfeeds/rss/?outputType=xml", 2),
    ("tech", "Hacker News", "https://hnrss.org/frontpage", 4),
    ("tech", "Ars Technica", "https://feeds.arstechnica.com/arstechnica/technology-lab", 3),
    ("tech", "The Verge", "https://www.theverge.com/rss/index.xml", 4),
    ("tech", "MIT Tech Review", "https://www.technologyreview.com/feed/", 3),
    (
        "ai",
        "AI News",
        "https://news.google.com/rss/search?q=(OpenAI%20OR%20Anthropic%20OR%20Google%20AI%20OR%20%22large%20language%20model%22%20OR%20ChatGPT)%20when%3A2d&hl=en-US&gl=US&ceid=US:en",
        4,
    ),
    ("ai", "VentureBeat AI", "https://venturebeat.com/category/ai/feed/", 4),
    ("ai", "The Verge AI", "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml", 4),
    ("ai", "MIT Tech Review", "https://www.technologyreview.com/topic/artificial-intelligence/feed", 3),
    ("ai", "ArXiv AI", "https://export.arxiv.org/rss/cs.AI", 4),
    ("finance", "CNBC", "https://www.cnbc.com/id/100003114/device/rss/rss.html", 2),
    (
        "finance",
        "MarketWatch",
        "https://news.google.com/rss/search?q=site%3Amarketwatch.com%20markets%20when%3A1d&hl=en-US&gl=US&ceid=US:en",
        2,
    ),
    ("finance", "Yahoo Finance", "https://finance.yahoo.com/news/rssindex", 4),
    ("finance", "Financial Times", "https://www.ft.com/rss/home", 2),
    (
        "finance",
        "Reuters Business",
        "https://news.google.com/rss/search?q=site%3Areuters.com%20business%20markets%20when%3A1d&hl=en-US&gl=US&ceid=US:en",
        1,
    ),
    ("gov", "White House", "https://www.whitehouse.gov/briefings-statements/feed/", 1),
    ("gov", "White House Actions", "https://www.whitehouse.gov/presidential-actions/feed/", 1),
    (
        "gov",
        "State Dept",
        "https://news.google.com/rss/search?q=(site%3Astate.gov%20OR%20%22State%20Department%22)%20when%3A1d&hl=en-US&gl=US&ceid=US:en",
        1,
    ),
    ("gov", "Pentagon", "https://www.war.gov/DesktopModules/ArticleCS/RSS.ashx?ContentType=1&Site=945", 1),
    ("gov", "Federal Reserve", "https://www.federalreserve.gov/feeds/press_all.xml", 3),
    ("gov", "SEC", "https://www.sec.gov/news/pressreleases.rss", 3),
    ("gov", "UN News", "https://news.un.org/feed/subscribe/en/news/all/rss.xml", 1),
    ("gov", "CISA", "https://www.cisa.gov/cybersecurity-advisories/all.xml", 1),
    (
        "gov",
        "Treasury",
        "https://news.google.com/rss/search?q=site%3Atreasury.gov%20when%3A1d&hl=en-US&gl=US&ceid=US:en",
        2,
    ),
    ("gov", "DOJ", "https://news.google.com/rss/search?q=site%3Ajustice.gov%20when%3A1d&hl=en-US&gl=US&ceid=US:en", 2),
    ("africa", "BBC Africa", "https://feeds.bbci.co.uk/news/world/africa/rss.xml", 4),
    ("africa", "News24", "https://feeds.news24.com/articles/news24/TopStories/rss", 4),
    ("africa", "Africanews", "https://www.africanews.com/feed/", 4),
    ("africa", "Premium Times", "https://www.premiumtimesng.com/feed", 2),
    ("latam", "BBC Latin America", "https://feeds.bbci.co.uk/news/world/latin_america/rss.xml", 4),
    ("latam", "Guardian Americas", "https://www.theguardian.com/world/americas/rss", 4),
    ("latam", "InSight Crime", "https://insightcrime.org/feed/", 4),
    ("asia", "BBC Asia", "https://feeds.bbci.co.uk/news/world/asia/rss.xml", 4),
    ("asia", "The Diplomat", "https://thediplomat.com/feed/", 3),
    (
        "asia",
        "Nikkei Asia",
        "https://news.google.com/rss/search?q=site%3Aasia.nikkei.com%20when%3A3d&hl=en-US&gl=US&ceid=US:en",
        2,
    ),
    ("asia", "CNA", "https://www.channelnewsasia.com/api/v1/rss-outbound-feed?_format=xml", 4),
    ("asia", "NDTV", "https://feeds.feedburner.com/ndtvnews-top-stories", 4),
    (
        "asia",
        "South China Morning Post",
        "https://news.google.com/rss/search?q=site%3Ascmp.com%20when%3A2d&hl=en-US&gl=US&ceid=US:en",
        4,
    ),
    ("asia", "The Hindu", "https://www.thehindu.com/feeder/default.rss", 4),
    (
        "asia",
        "Asia News",
        "https://news.google.com/rss/search?q=site%3Aasianews.it%20when%3A3d&hl=en-US&gl=US&ceid=US:en",
        4,
    ),
    (
        "asia",
        "Xinhua",
        "https://news.google.com/rss/search?q=site%3Axinhuanet.com%20OR%20Xinhua%20when%3A1d&hl=en-US&gl=US&ceid=US:en",
        3,
    ),
    (
        "energy",
        "Oil & Gas",
        "https://news.google.com/rss/search?q=(oil%20price%20OR%20OPEC%20OR%20%22natural%20gas%22%20OR%20pipeline%20OR%20LNG)%20when%3A2d&hl=en-US&gl=US&ceid=US:en",
        4,
    ),
    (
        "energy",
        "Reuters Energy",
        "https://news.google.com/rss/search?q=site%3Areuters.com%20energy%20when%3A2d&hl=en-US&gl=US&ceid=US:en",
        4,
    ),
    (
        "energy",
        "Nuclear Energy",
        "https://news.google.com/rss/search?q=(%22nuclear%20energy%22%20OR%20%22nuclear%20power%22%20OR%20%22nuclear%20reactor%22)%20when%3A3d&hl=en-US&gl=US&ceid=US:en",
        4,
    ),
    ("thinktanks", "Foreign Policy", "https://foreignpolicy.com/feed/", 3),
    ("thinktanks", "Atlantic Council", "https://www.atlanticcouncil.org/feed/", 3),
    ("thinktanks", "Foreign Affairs", "https://www.foreignaffairs.com/rss.xml", 3),
    ("thinktanks", "War on the Rocks", "https://warontherocks.com/feed/", 2),
    ("thinktanks", "CSIS", "https://www.csis.org/rss.xml", 3),
    ("crisis", "CrisisWatch", "https://www.crisisgroup.org/rss", 3),
    ("crisis", "IAEA", "https://www.iaea.org/feeds/topnews", 1),
    ("crisis", "WHO", "https://www.who.int/rss-feeds/news-english.xml", 1),
    (
        "layoffs",
        "Layoffs.fyi",
        "https://news.google.com/rss/search?q=tech%2Bcompany%2Blayoffs%2Bannounced%20when%3A3d&hl=en-US&gl=US&ceid=US:en",
        3,
    ),
    ("layoffs", "TechCrunch Layoffs", "https://techcrunch.com/tag/layoffs/feed/", 4),
    (
        "layoffs",
        "Layoffs News",
        "https://news.google.com/rss/search?q=(layoffs%20OR%20%22job%20cuts%22%20OR%20%22workforce%20reduction%22)%20when%3A3d&hl=en-US&gl=US&ceid=US:en",
        4,
    ),
)

_WM_INTEL: Final[tuple[tuple[str, str, str, int], ...]] = (
    ("intel", "Defense One", "https://www.defenseone.com/rss/all/", 3),
    ("intel", "The War Zone", "https://www.twz.com/feed", 3),
    (
        "intel",
        "Defense News",
        "https://www.defensenews.com/arc/outboundfeeds/rss/?outputType=xml",
        3,
    ),
    ("intel", "Breaking Defense", "https://breakingdefense.com/feed/", 3),
    (
        "intel",
        "Military Times",
        "https://www.militarytimes.com/arc/outboundfeeds/rss/?outputType=xml",
        2,
    ),
    ("intel", "Task & Purpose", "https://taskandpurpose.com/feed/", 3),
    (
        "intel",
        "USNI News",
        "https://news.google.com/rss/search?q=site:news.usni.org+when:3d&hl=en-US&gl=US&ceid=US:en",
        2,
    ),
    ("intel", "gCaptain", "https://gcaptain.com/feed/", 3),
    (
        "intel",
        "Oryx OSINT",
        "https://www.oryxspioenkop.com/feeds/posts/default?alt=rss",
        2,
    ),
    ("intel", "Foreign Policy", "https://foreignpolicy.com/feed/", 3),
    ("intel", "Foreign Affairs", "https://www.foreignaffairs.com/rss.xml", 3),
    ("intel", "Atlantic Council", "https://www.atlanticcouncil.org/feed/", 3),
    (
        "intel",
        "Bellingcat",
        "https://news.google.com/rss/search?q=site%3Abellingcat.com%20when%3A7d&hl=en-US&gl=US&ceid=US:en",
        3,
    ),
    ("intel", "Krebs Security", "https://krebsonsecurity.com/feed/", 3),
    (
        "intel",
        "Arms Control Assn",
        "https://news.google.com/rss/search?q=site%3Aarmscontrol.org%20when%3A7d&hl=en-US&gl=US&ceid=US:en",
        2,
    ),
    (
        "intel",
        "Bulletin of Atomic Scientists",
        "https://news.google.com/rss/search?q=site%3Athebulletin.org%20when%3A7d&hl=en-US&gl=US&ceid=US:en",
        2,
    ),
    ("intel", "FAO News", "https://www.fao.org/feeds/fao-newsroom-rss", 4),
    ("intel", "OCCRP", "https://www.occrp.org/en/feed", 2),
    ("intel", "DFRLab", "https://dfrlab.org/feed/", 2),
    ("intel", "Lighthouse Reports", "https://www.lighthousereports.com/feed/", 3),
    ("intel", "The Sentry", "https://thesentry.org/feed/", 3),
    ("intel", "GITOC", "https://globalinitiative.net/feed/", 3),
    ("intel", "VSquare", "https://vsquare.org/feed/", 3),
    ("intel", "Correctiv", "https://correctiv.org/feed/", 3),
)

_CRYPTO: Final[tuple[tuple[str, str, str, int], ...]] = (
    ("crypto", "CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/", 3),
    ("crypto", "Cointelegraph", "https://cointelegraph.com/rss", 3),
    (
        "crypto",
        "The Block",
        "https://news.google.com/rss/search?q=site:theblock.co+when:1d&hl=en-US&gl=US&ceid=US:en",
        3,
    ),
    ("crypto", "Decrypt", "https://decrypt.co/feed", 3),
    ("crypto", "The Defiant", "https://thedefiant.io/feed", 3),
    ("crypto", "Bitcoin Magazine", "https://bitcoinmagazine.com/feed", 3),
    ("crypto", "DL News", "https://news.google.com/rss/search?q=site:dlnews.com+when:3d&hl=en-US&gl=US&ceid=US:en", 3),
    ("crypto", "CryptoSlate", "https://cryptoslate.com/feed/", 3),
    ("crypto", "Unchained", "https://unchainedcrypto.com/feed/", 3),
    (
        "crypto",
        "DeFi News",
        "https://news.google.com/rss/search?q=(DeFi+OR+%22decentralized+finance%22)+when:3d&hl=en-US&gl=US&ceid=US:en",
        4,
    ),
    (
        "crypto",
        "Bloomberg Crypto",
        "https://news.google.com/rss/search?q=bloomberg+crypto+when:1d&hl=en-US&gl=US&ceid=US:en",
        1,
    ),
    (
        "crypto",
        "Reuters Crypto",
        "https://news.google.com/rss/search?q=reuters+crypto+when:1d&hl=en-US&gl=US&ceid=US:en",
        1,
    ),
    (
        "crypto",
        "Wu Blockchain",
        "https://news.google.com/rss/search?q=site:wublockchain.com+when:7d&hl=en-US&gl=US&ceid=US:en",
        3,
    ),
    ("crypto", "Messari", "https://news.google.com/rss/search?q=site:messari.io+when:3d&hl=en-US&gl=US&ceid=US:en", 3),
    (
        "crypto",
        "Stablecoin Policy",
        "https://news.google.com/rss/search?q=(stablecoin+regulation+OR+%22stablecoin+bill%22)+when:7d&hl=en-US&gl=US&ceid=US:en",
        4,
    ),
    ("crypto", "6551NEWS", "http://rsshub:1200/telegram/channel/news6551", 2),
)


class _SourceAccumulator(TypedDict):
    name: str
    url: str
    tier: int
    memberships: set[str]


def _source_id(name: str, url: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    digest = hashlib.sha256(url.encode()).hexdigest()[:8]
    return f"news-{slug}-{digest}"


def default_sources() -> tuple[NewsSourceDefinition, ...]:
    physical: dict[tuple[str, str], _SourceAccumulator] = {}
    for membership, name, url, tier in (*_WM_FULL_EN, *_WM_INTEL, *_CRYPTO):
        key = (name, url)
        row = physical.setdefault(
            key,
            {"name": name, "url": url, "tier": tier, "memberships": set()},
        )
        row["tier"] = min(int(row["tier"]), tier)
        row["memberships"].add(membership)
    return tuple(
        NewsSourceDefinition(
            source_id=_source_id(str(row["name"]), str(row["url"])),
            name=str(row["name"]),
            feed_url=str(row["url"]),
            tier=int(row["tier"]),
            lang="zh" if row["name"] == "6551NEWS" else "en",
            memberships=tuple(sorted(str(value) for value in row["memberships"])),
            refresh_interval_seconds=120,
        )
        for row in physical.values()
    )


__all__ = ["WORLDMONITOR_COMMIT", "default_sources"]
