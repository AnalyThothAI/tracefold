import type { OpenApiStatusData } from "@lib/types";
import { IconButton } from "@shared/ui/IconButton";
import { ChevronRight, RefreshCw, Search, TriangleAlert } from "lucide-react";
import { Popover } from "radix-ui";
import { useEffect, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";

import "./CockpitTopbar.css";

const NEWS_SEARCH_ARIA_LABEL = "news search";
/*
 * What the box actually searches: the server matches `q` against `search_doc` (context line + leader title)
 * and `leader_title ILIKE`. It does not index venues and does not resolve aliases, so promising
 * `base_symbol / 场所` the way the design does would send readers looking for `hl.perp` into an empty feed
 * (#87 review). Widen the placeholder when the backend widens, not before.
 */
const NEWS_SEARCH_PLACEHOLDER = "搜索新闻事件 / 标题 / 资产";

/**
 * One always-visible number from the pipeline. `value` is undefined until the poll answers.
 *
 * `text` is the escape hatch for a figure that is not a plain count (#88): a hit rate has to arrive with its
 * denominator or not at all, and `56% · N=225` is one server-decided string rather than two numbers the
 * topbar would have to relate to each other.
 */
export type CockpitTopbarFigure = {
  label: string;
  text?: string;
  title?: string;
  tone?: "accent" | "caution";
  value?: number;
};

/**
 * The pipeline's own read, as the topbar lamp shows it (#207).
 *
 * The shape is structural on purpose: the topbar owns where the lamp sits and how it behaves, and News owns
 * what the words are. Every string here is server copy the route passed through — the frame translates
 * nothing and computes no second health state.
 */
export type CockpitHealthRow = {
  key: string;
  label: string;
  level: "ok" | "warn" | "bad" | "off";
  summary: string;
};

export type CockpitHealth = {
  /** Only the two levels worth interrupting for. `ok` and `off` arrive as `null` and draw nothing at all. */
  level: "warn" | "bad";
  /** The overall word, e.g. `流水线注意`. */
  headline: string;
  /** The worst item's own sentence. */
  summary: string;
  items: CockpitHealthRow[];
  to: string;
};

export type CockpitTopbarProps = {
  figures?: CockpitTopbarFigure[];
  /** `null` while the pipeline is healthy — see `HealthLamp`. */
  health?: CockpitHealth | null;
  onRefresh: () => void;
  search: {
    onSubmitQuery: (query: string) => void;
    query?: string;
  };
  status: {
    configReady: boolean;
    status?: OpenApiStatusData | null;
    statusError: boolean;
    statusLoading: boolean;
  };
  /** Which surface the reader is on. The frame says where you are; the page says what is on it. */
  title: string;
};

export function CockpitTopbar({
  figures,
  health,
  navigationTrigger,
  onRefresh,
  search,
  status,
  title,
}: CockpitTopbarProps & { navigationTrigger?: ReactNode }) {
  const [searchDraft, setSearchDraft] = useState(search.query ?? "");
  const anomaly = healthAnomaly(status);
  useEffect(() => setSearchDraft(search.query ?? ""), [search.query]);
  return (
    <header className="topbar">
      <div className="brand">
        {navigationTrigger}
        <span className="topbar-page-title">{title}</span>
        <HealthLamp health={health} />
      </div>

      {/* Enter submits. The box is the whole search interaction — there is no hotkey that focuses it. */}
      <form
        className="searchbar"
        onSubmit={(event) => {
          event.preventDefault();
          search.onSubmitQuery(searchDraft);
        }}
      >
        <Search aria-hidden />
        <label className="sr-only" htmlFor="news-search-input">
          {NEWS_SEARCH_ARIA_LABEL}
        </label>
        <input
          aria-label={NEWS_SEARCH_ARIA_LABEL}
          id="news-search-input"
          onChange={(event) => setSearchDraft(event.target.value)}
          placeholder={NEWS_SEARCH_PLACEHOLDER}
          value={searchDraft}
        />
      </form>

      <div className="topbar-right">
        {/*
         * The numbers an operator glances at without opening a page (#87). Hidden until the poll answers
         * rather than shown as zero — a zero that means "not loaded yet" is worse than a gap.
         */}
        {figures?.some((figure) => figure.value != null || figure.text) ? (
          <span className="topbar-figures">
            {figures
              .filter((figure) => figure.value != null || figure.text)
              .map((figure) => (
                <span data-tone={figure.tone} key={figure.label} title={figure.title}>
                  <small>{figure.label}</small>
                  <b>{figure.text ?? new Intl.NumberFormat("zh-CN").format(figure.value ?? 0)}</b>
                </span>
              ))}
          </span>
        ) : null}
        {anomaly ? (
          <span className="topbar-anomaly" role="status" title={anomaly}>
            <TriangleAlert aria-hidden />
            <span>{anomaly}</span>
          </span>
        ) : null}
        <IconButton aria-label="刷新" onClick={onRefresh} title="刷新">
          <RefreshCw aria-hidden />
        </IconButton>
      </div>
    </header>
  );
}

/**
 * Pipeline health, on every page, and only when there is something to say (#207).
 *
 * A healthy pipeline renders zero pixels. That is the whole rule: a permanently green light is one the
 * reader learns to stop seeing, and 流水线状态 used to spend a navigation slot proving "everything is fine".
 * When a level is `warn` or `bad` the lamp appears beside the page title with the failing item's own
 * sentence, and one click opens the four stage lines and the door to the page that explains them.
 *
 * Radix owns `Esc`, the dismiss layer and `aria-expanded`. The console binds no document-level key handler
 * of its own and this must not become the exception.
 */
function HealthLamp({ health }: { health?: CockpitHealth | null }) {
  if (!health) return null;
  return (
    <Popover.Root>
      <Popover.Trigger asChild>
        <button
          aria-label={`流水线健康：${health.summary || health.headline}`}
          className="topbar-health-lamp"
          data-level={health.level}
          title={health.summary || health.headline}
          type="button"
        >
          <span aria-hidden className="topbar-health-dot" />
          <span className="topbar-health-summary">{health.summary || health.headline}</span>
        </button>
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Content
          align="start"
          className="topbar-health-popover"
          collisionPadding={12}
          sideOffset={6}
        >
          <b className="topbar-health-popover-title">{health.headline}</b>
          {/* A failed read has a headline and a door but no stage lines: there is no health to break down. */}
          {health.items.length === 0 ? null : (
            <ul className="topbar-health-items">
              {health.items.map((item) => (
                <li data-level={item.level} key={item.key}>
                  <span aria-hidden className="topbar-health-dot" data-level={item.level} />
                  <b>{item.label}</b>
                  <span>{item.summary}</span>
                </li>
              ))}
            </ul>
          )}
          <Link className="topbar-health-link" to={health.to}>
            打开流水线状态
            <ChevronRight aria-hidden />
          </Link>
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  );
}

function healthAnomaly({
  configReady,
  status,
  statusError,
  statusLoading,
}: CockpitTopbarProps["status"]): string | null {
  if (!configReady) return "配置未就绪";
  if (statusLoading && !status) return null;
  if (statusError) return "状态检查失败";
  if (status && !status.runtime.ok) return status.runtime.reasons[0] || "服务未就绪";
  return null;
}
