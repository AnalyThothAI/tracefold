import { BrandMark } from "@shared/ui/icons";
import { Link, useLocation } from "react-router-dom";

import { APP_NAVIGATION_GROUPS, type AppNavigationItem } from "./appNavigation";
import "./AppSidebar.css";

export type AppNavigationCounts = { events?: number; oiFrames?: number };
/**
 * The words a destination can carry instead of a number (#207 PR-W4). `tradingMode` is the capital
 * lane's ledger mode — `PAPER` today — because "is anything on that page real money" is the question a
 * reader has before opening it, and a volume would not answer it.
 */
export type AppNavigationBadges = { tradingMode?: string };

/**
 * The console's navigation, in the frame at desktop width and inside the tablet drawer below it. One
 * component for both, so the two presentations cannot disagree about what exists or which one is current.
 *
 * `onNavigate` is how the drawer closes itself: the in-frame sidebar passes nothing and stays put.
 *
 * Pipeline health used to ride here as a dot on the 流水线状态 entry. It moved to the topbar (#207): the
 * dot was visible only while the sidebar was, which is neither of the two narrower frames, and it could say
 * that something was wrong without saying what.
 */
export function AppSidebar({
  badges,
  counts,
  inDrawer = false,
  onNavigate,
}: {
  badges?: AppNavigationBadges;
  counts?: AppNavigationCounts;
  inDrawer?: boolean;
  onNavigate?: () => void;
}) {
  return (
    <aside
      aria-label="Application sidebar"
      className="cockpit-app-sidebar"
      data-in-drawer={inDrawer || undefined}
    >
      {inDrawer ? null : <AppBrand />}
      <AppNavigation badges={badges} counts={counts} onNavigate={onNavigate} />
    </aside>
  );
}

/** The product mark. It is the sidebar's header at desktop width and the drawer's header inside the drawer. */
export function AppBrand() {
  return (
    <div className="cockpit-app-sidebar-brand">
      <BrandMark className="cockpit-app-sidebar-mark" />
      <span className="cockpit-app-sidebar-brand-copy">
        <b>Tracefold</b>
        <small>News V3 Console</small>
      </span>
    </div>
  );
}

export function AppNavigation({
  badges,
  counts,
  onNavigate,
}: {
  badges?: AppNavigationBadges;
  counts?: AppNavigationCounts;
  onNavigate?: () => void;
}) {
  const { pathname } = useLocation();
  return (
    <nav aria-label="Primary navigation" className="cockpit-app-sidebar-nav">
      {APP_NAVIGATION_GROUPS.map((group) => (
        <section className="cockpit-app-sidebar-group" key={group.label}>
          <h2 className="cockpit-app-sidebar-group-heading">{group.label}</h2>
          <div className="cockpit-app-sidebar-items">
            {group.items.map((item) => (
              <AppSidebarItem
                active={item.isActive(pathname)}
                badge={item.badge ? badges?.[item.badge] : undefined}
                count={item.count ? counts?.[item.count] : undefined}
                item={item}
                key={item.to}
                onNavigate={onNavigate}
              />
            ))}
          </div>
        </section>
      ))}
    </nav>
  );
}

function AppSidebarItem({
  active,
  badge,
  count,
  item,
  onNavigate,
}: {
  active: boolean;
  badge?: string;
  count?: number;
  item: AppNavigationItem;
  onNavigate?: () => void;
}) {
  const Icon = item.icon;
  return (
    /*
     * A plain Link with `aria-current` driven by the same predicate as the visual state. `NavLink` would
     * decide for itself by prefix, and `/news` is a prefix of `/news/oi` — two links would announce
     * themselves as the current page.
     */
    <Link
      aria-current={active ? "page" : undefined}
      className="cockpit-app-sidebar-item"
      data-active={active || undefined}
      onClick={onNavigate}
      to={item.to}
    >
      <span aria-hidden className="cockpit-app-sidebar-rail" />
      <Icon aria-hidden />
      <span className="cockpit-app-sidebar-label">{item.label}</span>
      {/*
       * The count is decoration on the link, not part of what it is: folding it into the accessible name
       * would make the destination announce itself differently every three seconds. The same figures are
       * announced properly by the feed's labelled 24 h funnel and by the OI monitor's own telemetry band.
       */}
      {count == null ? null : (
        <span aria-hidden className="cockpit-app-sidebar-count" title="过去 24 小时收到">
          {compactCount(count)}
        </span>
      )}
      {/*
       * `aria-hidden` for the same reason the count is: the destination is 交易 whether the ledger is on
       * paper or not, and folding the mode into the link's name would rename it when configuration changed.
       * The page itself states the mode in a labelled figure.
       */}
      {badge ? (
        <span aria-hidden className="cockpit-app-sidebar-badge" title="资本通道当前模式">
          {badge}
        </span>
      ) : null}
    </Link>
  );
}

/**
 * `1.4k`, not `1,463`: the sidebar has 30px of room beside a label, and the exact figure is one click away on
 * the funnel that owns it. Below a thousand there is nothing to compact.
 *
 * Truncated, never rounded — a shorthand for "how much is behind this link" must not report more than
 * arrived.
 */
function compactCount(value: number): string {
  if (value < 1000) return String(value);
  return `${(Math.floor(value / 100) / 10).toFixed(1).replace(/\.0$/, "")}k`;
}
