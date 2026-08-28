import { newsEventPath, newsSymbolPath } from "@shared/routing/paths";
import { useRouteReferrer } from "@shared/routing/routeReferrer";
import { Drawer } from "@shared/ui/Drawer";
import { IconButton } from "@shared/ui/IconButton";
import * as PageState from "@shared/ui/PageState";
import { ChevronRight, X } from "lucide-react";
import { Link } from "react-router-dom";

import { useNewsEventWithToken, useNewsQuotesWithToken } from "../../api/newsQueries";
import { clockTime, displayAssetRefs, displayAssets } from "../../model/newsLabels";
import { NewsAssetChips } from "../chrome/NewsAssetChips";
import { NewsKindBadge } from "../chrome/NewsKindBadge";
import { NewsOutcomeBadge } from "../chrome/NewsOutcomeBadge";
import { NewsQuoteReadState } from "../chrome/NewsQuoteReadState";

import { NewsEventDrawerTimeline } from "./NewsTimeline";

import "./newsEventDrawer.css";

/**
 * The Event beside the list, not instead of it (design proposal ⑦).
 *
 * Reviewing a queue used to mean a full-page navigation per Event and a scroll back to where you were. The
 * drawer is deliberately non-modal: the list stays live behind it, and clicking the next row swaps what the
 * drawer shows instead of closing it. The Event's own page is still the canonical, shareable surface — the
 * footer link and any modified click go straight there.
 *
 * It carries the compact Artifact reading order: source, assets, judgment, then the four-step judgment chain.
 * Quotes, technical detail, related reporting and every raw verdict record stay on the full page.
 */
export function NewsEventDrawer({
  eventId,
  feedSearch,
  onClose,
  restoreFocusTo,
  token,
}: {
  eventId: string | null;
  feedSearch: string;
  onClose: () => void;
  restoreFocusTo?: HTMLElement | null;
  token: string;
}) {
  const query = useNewsEventWithToken(token, eventId);
  const referrer = useRouteReferrer();
  const detail = eventId ? query.data : undefined;
  const event = detail?.event;
  const triage = detail?.triage;
  const assets = displayAssetRefs(event?.grounded_assets ?? [], event?.assets);
  const quotesQuery = useNewsQuotesWithToken(
    token,
    assets.filter((asset) => asset.listed).map((asset) => asset.symbol),
  );
  const quotes = (quotesQuery.data?.quotes ?? []).filter((quote) =>
    assets.some((asset) => asset.symbol === quote.requested_symbol),
  );
  const quotesBySymbol = Object.fromEntries(quotes.map((quote) => [quote.requested_symbol, quote]));
  const primaryTag = displayAssets(
    (triage?.assets ?? []).filter((asset) => asset.role === "primary").map((asset) => asset.symbol),
  )[0];
  const primarySymbol = primaryTag
    ? (assets.find((asset) => displayAssets([asset.symbol, asset.base_symbol]).includes(primaryTag))
        ?.base_symbol ?? primaryTag)
    : undefined;
  const headline =
    triage?.headline_zh?.trim() || triage?.title_zh?.trim() || event?.leader_title || "事件";
  return (
    <Drawer
      actions={
        eventId ? (
          <IconButton aria-label="关闭" onClick={onClose} size="sm">
            <X aria-hidden />
          </IconButton>
        ) : null
      }
      eyebrow={
        detail ? (
          <>
            <span className="news-drawer-time">{clockTime(detail.event.opened_at_ms)}</span>
            <span aria-hidden>·</span>
            <span>{detail.event.reporting_origin || "未知来源"}</span>
            <NewsKindBadge kind={detail.event.event_kind} />
            <NewsOutcomeBadge outcome={detail.outcome} />
          </>
        ) : null
      }
      modal={false}
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
      open={Boolean(eventId)}
      restoreFocusTo={restoreFocusTo}
      title={headline}
      width={520}
    >
      {query.isLoading && !detail ? (
        <PageState.Loading label="正在读取事件" layout="inline" rows={4} />
      ) : null}
      {query.isError && !detail ? (
        <PageState.Error error={query.error} onRetry={() => void query.refetch()} />
      ) : null}
      {detail && event ? (
        <NewsQuoteReadState query={quotesQuery}>
          <div className="news-drawer-body">
            <h2 className="news-drawer-headline">{headline}</h2>
            <p className="news-drawer-original">{event.leader_title}</p>
            {assets.length ? <NewsAssetChips assets={assets} quotes={quotesBySymbol} /> : null}
            {triage?.why_zh ? <p className="news-drawer-why">{triage.why_zh}</p> : null}
            <h3 className="news-drawer-section-title">判定链路</h3>
            <NewsEventDrawerTimeline steps={detail.timeline ?? []} />
            <footer className="news-drawer-footer">
              <Link state={{ feedSearch }} to={newsEventPath(event.event_id)}>
                打开事件详情
                <ChevronRight aria-hidden />
              </Link>
              {primarySymbol ? (
                <Link state={referrer} to={newsSymbolPath(primarySymbol)}>
                  代币页 {primarySymbol}
                  <ChevronRight aria-hidden />
                </Link>
              ) : null}
            </footer>
          </div>
        </NewsQuoteReadState>
      ) : null}
    </Drawer>
  );
}
