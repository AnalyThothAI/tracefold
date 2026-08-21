import { newsEventPath } from "@shared/routing/paths";
import { ActionButton } from "@shared/ui/ActionButton";
import { Drawer } from "@shared/ui/Drawer";
import { FactGrid } from "@shared/ui/FactGrid";
import * as PageState from "@shared/ui/PageState";
import { ExternalLink } from "lucide-react";
import { Link } from "react-router-dom";

import { useNewsEventWithToken, useNewsQuotesWithToken } from "../../api/newsQueries";
import {
  clockTime,
  displayAssetRefs,
  labelCommand,
  validExternalUrl,
} from "../../model/newsLabels";
import { NewsAssetChips } from "../chrome/NewsAssetChips";
import { NewsDirectionChip } from "../chrome/NewsDirectionChip";
import { NewsOutcomeBadge } from "../chrome/NewsOutcomeBadge";

import { NewsQuoteTable } from "./NewsQuoteTable";
import { NewsEventDrawerTimeline } from "./NewsTimeline";

import "./newsEventDrawer.css";

/**
 * The Event beside the list, not instead of it (design proposal ⑦).
 *
 * Reviewing a queue used to mean a full-page navigation per Event and a scroll back to where you were. The
 * drawer is deliberately non-modal: the feed keeps the keyboard, `J`/`K` walk the rows, and the drawer
 * follows the cursor without ever closing. The Event's own page is still the canonical, shareable surface —
 * 打开整页 and any modified click go straight there.
 *
 * It carries the three things a reviewer actually needs: what was judged, what the assets are worth, and how
 * it got here. The audit trail (`技术详情`, 同类报道, every raw verdict record) stays on the full page.
 */
export function NewsEventDrawer({
  copy,
  eventId,
  feedSearch,
  onClose,
  token,
}: {
  copy: (text: string, note: string) => void;
  eventId: string | null;
  feedSearch: string;
  onClose: () => void;
  token: string;
}) {
  const query = useNewsEventWithToken(token, eventId);
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
  const headline =
    triage?.headline_zh?.trim() || triage?.title_zh?.trim() || event?.leader_title || "事件";
  const url = validExternalUrl(event?.leader_url);
  return (
    <Drawer
      actions={
        eventId ? (
          <>
            <span aria-hidden className="news-drawer-hint">
              <kbd>K</kbd>
              <kbd>J</kbd>
              换条
            </span>
            <Link className="news-drawer-open" state={{ feedSearch }} to={newsEventPath(eventId)}>
              打开整页
            </Link>
          </>
        ) : null
      }
      eyebrow={
        detail ? (
          <>
            <NewsOutcomeBadge outcome={detail.outcome} variant="chip" />
            <span className="news-drawer-time">{clockTime(detail.event.opened_at_ms)}</span>
          </>
        ) : null
      }
      modal={false}
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
      open={Boolean(eventId)}
      title={headline}
    >
      {query.isLoading && !detail ? (
        <PageState.Loading label="正在读取事件" layout="inline" rows={4} />
      ) : null}
      {query.isError && !detail ? (
        <PageState.Error error={query.error} onRetry={() => void query.refetch()} />
      ) : null}
      {detail && event ? (
        <div className="news-drawer-body">
          <h2 className="news-drawer-headline">{headline}</h2>
          {triage ? (
            <p className="news-drawer-verdict">
              <NewsDirectionChip size="lg" triage={triage} />
              {triage.event_type_zh ? <span>{triage.event_type_zh}</span> : null}
            </p>
          ) : null}
          {assets.length ? <NewsAssetChips assets={assets} /> : null}
          {triage?.why_zh ? <p className="news-drawer-why">{triage.why_zh}</p> : null}
          {triage ? (
            <FactGrid
              columns={2}
              facts={[
                { label: "范围", value: triage.scope_zh },
                {
                  label: "把握",
                  value: triage.confidence == null ? "" : `${Math.round(triage.confidence * 100)}%`,
                },
                { label: "新颖度", value: triage.novelty_zh },
                {
                  label: "可操作",
                  value: triage.actionable == null ? "" : triage.actionable ? "是" : "否",
                },
              ]}
              label="判定明细"
            />
          ) : null}
          {quotes.length ? <NewsQuoteTable quotes={quotes} /> : null}
          <p className="news-drawer-original">
            <span className="news-drawer-original-label">
              原文 · {event.reporting_origin || "未知来源"}
            </span>
            <span>{event.leader_title}</span>
            {url ? (
              <a href={url} rel="noreferrer" target="_blank">
                打开
                <ExternalLink aria-hidden />
              </a>
            ) : null}
          </p>
          <NewsEventDrawerTimeline steps={detail.timeline ?? []} />
          <div className="news-drawer-labels">
            <ActionButton
              onClick={() => copy(labelCommand(event.event_id, "good"), "已复制「判得对」标注命令")}
              size="sm"
              variant="positive"
            >
              判得对
            </ActionButton>
            <ActionButton
              onClick={() =>
                copy(labelCommand(event.event_id, "noise"), "已复制「不该推」标注命令")
              }
              size="sm"
              variant="negative"
            >
              不该推
            </ActionButton>
            <ActionButton
              onClick={() => copy(labelCommand(event.event_id, "missed"), "已复制「漏推」标注命令")}
              size="sm"
            >
              漏推
            </ActionButton>
          </div>
        </div>
      ) : null}
    </Drawer>
  );
}
