import {
  NotificationBell,
  NotificationDrawer,
  WatchlistNotificationDot,
} from "@features/notifications";
import type { NotificationItem, NotificationSummary } from "@lib/types";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { axe } from "jest-axe";
import { afterEach, describe, expect, it, vi } from "vitest";

afterEach(() => cleanup());

const summary: NotificationSummary = {
  subscriber_key: "local",
  unread_count: 3,
  high_unread_count: 1,
  critical_unread_count: 0,
  highest_unread_severity: "high",
  account_unread_counts: { toly: 2 },
};

const notification: NotificationItem = {
  notification_id: "n1",
  dedup_key: "news:pepe",
  rule_id: "watched_account_activity",
  severity: "high",
  title: "PEPE news",
  body: "News score 88",
  entity_type: "token",
  entity_key: "token:eth:pepe",
  author_handle: null,
  symbol: "PEPE",
  chain: "eth",
  address: "0xpepe",
  event_id: null,
  source_table: "news_stories",
  source_id: "token:eth:pepe",
  occurrence_count: 1,
  first_seen_at_ms: 1_700_000_000_000,
  last_seen_at_ms: 1_700_000_000_000,
  created_at_ms: 1_700_000_000_000,
  updated_at_ms: 1_700_000_000_000,
  read_at_ms: null,
  payload: { provider_score: 88 },
  channels: ["in_app"],
};

describe("notification center components", () => {
  it("renders bell unread count and severity state", () => {
    const onClick = vi.fn();

    render(<NotificationBell summary={summary} open={false} onClick={onClick} />);
    fireEvent.click(screen.getByRole("button", { name: /notifications/i }));

    expect(screen.getByText("3")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /notifications/i })).toHaveClass("has-high");
    expect(onClick).toHaveBeenCalledOnce();
  });

  it("renders drawer actions for individual and bulk read state", async () => {
    const onClose = vi.fn();
    const onMarkAllRead = vi.fn();
    const onMarkRead = vi.fn();
    const onOpenNotification = vi.fn();

    const { container } = render(
      <NotificationDrawer
        loading={false}
        notifications={[notification]}
        open
        summary={summary}
        onClose={onClose}
        onMarkAllRead={onMarkAllRead}
        onMarkRead={onMarkRead}
        onOpenNotification={onOpenNotification}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /open PEPE news/i }));
    fireEvent.click(screen.getByRole("button", { name: /mark PEPE news read/i }));
    fireEvent.click(screen.getByRole("button", { name: /mark all read/i }));
    fireEvent.click(screen.getByRole("button", { name: /close notifications/i }));

    expect(screen.getByText("PEPE news")).toBeInTheDocument();
    expect(onOpenNotification).toHaveBeenCalledWith(notification);
    expect(onMarkRead).toHaveBeenCalledWith("n1");
    expect(onMarkAllRead).toHaveBeenCalledOnce();
    expect(onClose).toHaveBeenCalledOnce();
    expect(await axe(container)).toHaveNoViolations();
  });

  it("renders PageState inline loading in the notification drawer", () => {
    const { container } = render(
      <NotificationDrawer
        loading
        notifications={[]}
        open
        summary={summary}
        onClose={vi.fn()}
        onMarkAllRead={vi.fn()}
        onMarkRead={vi.fn()}
        onOpenNotification={vi.fn()}
      />,
    );

    const loading = screen.getByRole("status", { name: "loading notifications" });
    expect(loading).toHaveClass("page-state-loading", "page-state-layout-inline");
    expect(container.querySelector(".page-state-table-skeleton")).toBeInTheDocument();
  });

  it("renders PageState empty state when the notification drawer is clear", () => {
    const { container } = render(
      <NotificationDrawer
        loading={false}
        notifications={[]}
        open
        summary={{ ...summary, unread_count: 0 }}
        onClose={vi.fn()}
        onMarkAllRead={vi.fn()}
        onMarkRead={vi.fn()}
        onOpenNotification={vi.fn()}
      />,
    );

    expect(screen.getByText("clear").closest(".page-state-empty")).toBeInTheDocument();
    expect(container.querySelector(".page-state-empty")).toBeInTheDocument();
  });

  it("only shows watchlist dot when the account has unread notifications", () => {
    const { rerender } = render(<WatchlistNotificationDot count={2} />);

    expect(screen.getByText("2")).toBeInTheDocument();

    rerender(<WatchlistNotificationDot count={0} />);

    expect(screen.queryByText("2")).not.toBeInTheDocument();
  });
});
