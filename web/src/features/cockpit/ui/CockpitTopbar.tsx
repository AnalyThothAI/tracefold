import type { OpenApiStatusData } from "@lib/types";
import { ChevronDown, Search, TriangleAlert } from "lucide-react";
import { Popover } from "radix-ui";
import { useEffect, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";

import "./CockpitTopbar.css";

const NEWS_SEARCH_ARIA_LABEL = "news search";
/** Approved copy, now on every route (#256). Server-side `q` stays the authority over which fields match. */
const NEWS_SEARCH_PLACEHOLDER = "标的 / 事件关键词";

/**
 * One route-context fact from a status read. `value` is undefined until the poll answers.
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
  /** `null` is a neutral server fact, not a browser-computed health judgment. */
  level: "ok" | "warn" | "bad" | "off" | null;
  summary: string;
};

export type CockpitHealth = {
  /** Stable visible copy for routes whose approved chrome names the affordance, independent of health. */
  buttonText?: string;
  level: "ok" | "warn" | "bad" | "off";
  /** The overall word, e.g. `流水线注意`. */
  headline: string;
  /** The worst item's own sentence. */
  summary: string;
  items: CockpitHealthRow[];
  to: string;
};

export type CockpitTopbarProps = {
  figures?: CockpitTopbarFigure[];
  /** `null` only when the read itself has nothing to report; the affordance is otherwise always present. */
  health?: CockpitHealth | null;
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
        {/* An inert keycap: the box is the whole search interaction and `/` binds nothing. */}
        <span aria-hidden className="cockpit-searchbar-keycap">
          /
        </span>
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
      </div>
    </header>
  );
}

/**
 * Pipeline health, and the only door to 流水线状态 (#256).
 *
 * The artifact lights the lamp when the pipeline is not ok. It stays visible when it *is* ok because the
 * status page holds no navigation slot: hiding the affordance on the healthy path would make the page
 * unreachable exactly when a reader wants to confirm that nothing is wrong. One click opens the
 * server-owned stage lines, the instrument snapshot and the destination.
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
          <span className="topbar-health-summary">
            {health.buttonText || health.summary || health.headline}
          </span>
          <ChevronDown aria-hidden className="topbar-health-chevron" />
        </button>
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Content
          align="start"
          className="topbar-health-popover"
          collisionPadding={12}
          sideOffset={6}
        >
          {/* A failed read has a headline and a door but no stage lines: there is no health to break down. */}
          {health.items.length === 0 ? (
            <b className="topbar-health-popover-title">{health.headline}</b>
          ) : (
            <ul className="topbar-health-items">
              {health.items.map((item) => (
                <li data-level={item.level ?? undefined} key={item.key}>
                  <span
                    aria-hidden
                    className="topbar-health-dot"
                    data-level={item.level ?? undefined}
                  />
                  <b>{item.label}</b>
                  <span>{item.summary}</span>
                </li>
              ))}
            </ul>
          )}
          <Link className="topbar-health-link" to={health.to}>
            打开流水线状态 →
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
