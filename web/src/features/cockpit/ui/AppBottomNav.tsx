import { Link, useLocation } from "react-router-dom";

import { APP_NAVIGATION_GROUPS, type AppNavigationItem } from "./appNavigation";
import "./AppBottomNav.css";

/**
 * The phone's whole navigation (#87). Below the tablet breakpoint the sidebar drawer is gone: a drawer costs
 * a tap to open before the reader can even see where they could go, and this console has few enough
 * destinations to show all of them at once under the thumb.
 *
 * It reads the same `APP_NAVIGATION_GROUPS` the sidebar does, so a new destination appears in both without
 * either presentation growing its own list. Groups are flattened — the "Research" heading is a desktop
 * affordance, not something a three-item bar needs.
 */
export function AppBottomNav() {
  const { pathname } = useLocation();
  const items = APP_NAVIGATION_GROUPS.flatMap((group) => group.items);
  return (
    <nav aria-label="Primary navigation" className="cockpit-bottom-nav">
      {items.map((item) => (
        <AppBottomNavItem active={item.isActive(pathname)} item={item} key={item.to} />
      ))}
    </nav>
  );
}

function AppBottomNavItem({ active, item }: { active: boolean; item: AppNavigationItem }) {
  const Icon = item.icon;
  return (
    // `aria-current` comes from the same predicate the sidebar uses rather than from `NavLink`, which would
    // decide by prefix and announce `/news` as current while the reader is on `/news/status`.
    <Link aria-current={active ? "page" : undefined} data-active={active || undefined} to={item.to}>
      <Icon aria-hidden />
      <span>{item.label}</span>
    </Link>
  );
}
