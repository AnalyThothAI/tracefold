import { Link, useLocation } from "react-router-dom";

import { APP_NAVIGATION_GROUPS, type AppNavigationItem } from "./appNavigation";
import "./AppSidebar.css";

export type AppNavigationCounts = { events?: number };
/** The one signal a destination may carry besides its count: how the pipeline behind it is doing. */
export type AppNavigationLevel = "ok" | "warn" | "bad";

/**
 * The console's navigation, in the frame at desktop width and inside the tablet drawer below it. One
 * component for both, so the two presentations cannot disagree about what exists or which one is current.
 *
 * `onNavigate` is how the drawer closes itself: the in-frame sidebar passes nothing and stays put.
 */
export function AppSidebar({
  counts,
  inDrawer = false,
  onNavigate,
  statusLevel,
}: {
  counts?: AppNavigationCounts;
  inDrawer?: boolean;
  onNavigate?: () => void;
  statusLevel?: AppNavigationLevel;
}) {
  return (
    <aside
      aria-label="Application sidebar"
      className="cockpit-app-sidebar"
      data-in-drawer={inDrawer || undefined}
    >
      {inDrawer ? null : <AppBrand />}
      <AppNavigation counts={counts} onNavigate={onNavigate} statusLevel={statusLevel} />
    </aside>
  );
}

/** The product mark. It is the sidebar's header at desktop width and the drawer's header inside the drawer. */
export function AppBrand() {
  return (
    <div className="cockpit-app-sidebar-brand">
      <span aria-hidden className="cockpit-app-sidebar-mark">
        T
      </span>
      <span className="cockpit-app-sidebar-brand-copy">
        <b>Tracefold</b>
        <small>News V3 Console</small>
      </span>
    </div>
  );
}

export function AppNavigation({
  counts,
  onNavigate,
  statusLevel,
}: {
  counts?: AppNavigationCounts;
  onNavigate?: () => void;
  statusLevel?: AppNavigationLevel;
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
                count={item.count ? counts?.[item.count] : undefined}
                item={item}
                key={item.to}
                level={item.signal === "health" ? statusLevel : undefined}
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
  count,
  item,
  level,
  onNavigate,
}: {
  active: boolean;
  count?: number;
  item: AppNavigationItem;
  level?: AppNavigationLevel;
  onNavigate?: () => void;
}) {
  const Icon = item.icon;
  return (
    /*
     * A plain Link with `aria-current` driven by the same predicate as the visual state. `NavLink` would
     * decide for itself by prefix, and `/news` is a prefix of `/news/status` — two links would announce
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
       * Count and health dot are decoration on the link, not part of what it is: folding either into the
       * accessible name would make the destination announce itself differently every three seconds. The same
       * figures are announced properly by the feed's labelled 24 h funnel and the status route's cards.
       */}
      {count == null ? null : (
        <span aria-hidden className="cockpit-app-sidebar-count" title="过去 24 小时收到">
          {compactCount(count)}
        </span>
      )}
      {level && level !== "ok" ? (
        <span aria-hidden className="cockpit-app-sidebar-signal" data-level={level} />
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
