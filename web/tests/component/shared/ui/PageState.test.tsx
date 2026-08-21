import { ActionButton } from "@shared/ui/ActionButton";
import * as PageState from "@shared/ui/PageState";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { axe } from "jest-axe";
import { describe, expect, it, vi } from "vitest";

describe("PageState shared UI", () => {
  it("renders accessible loading and table skeleton states", async () => {
    const { container } = render(
      <section>
        <PageState.Loading label="loading route data" layout="route" rows={2} />
        <PageState.TableSkeleton compact label="loading compact table" rows={3} />
      </section>,
    );

    expect(screen.getByRole("status", { name: "loading route data" })).toHaveClass(
      "page-state-loading",
      "page-state-layout-route",
    );
    expect(screen.getByRole("status", { name: "loading compact table" })).toHaveClass(
      "page-state-table-skeleton",
      "page-state-table-skeleton-compact",
    );
    // The skeleton keeps the shape of the rows it stands in for: three blocks per row, five rows in all.
    expect(container.querySelectorAll(".page-state-table-block")).toHaveLength(15);
    expect(container.querySelectorAll(".page-state-table-row")).toHaveLength(5);
    expect(await axe(container)).toHaveNoViolations();
  });

  it("renders empty state hints and caller-provided actions", async () => {
    const { container } = render(
      <PageState.Empty
        action={<ActionButton>Reset filters</ActionButton>}
        hint="Try a wider window."
        title="No rows"
      />,
    );

    expect(screen.getByText("No rows")).toBeInTheDocument();
    expect(screen.getByText("Try a wider window.")).toBeInTheDocument();
    expect(screen.getByText("No rows").closest(".page-state-empty")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reset filters" })).toHaveClass("ui-button");
    expect(await axe(container)).toHaveNoViolations();
  });

  it("renders error alerts with canonical retry buttons", async () => {
    const onRetry = vi.fn();
    const { container } = render(
      <PageState.Error error={new Error("backend unavailable")} onRetry={onRetry} />,
    );

    const alert = screen.getByRole("alert");
    expect(alert).toHaveClass("page-state-error");
    expect(alert).toHaveTextContent("backend unavailable");
    const retry = within(alert).getByRole("button", { name: "重试" });

    fireEvent.click(retry);

    expect(onRetry).toHaveBeenCalledOnce();
    expect(await axe(container)).toHaveNoViolations();
  });

  it("marks stale content busy while retaining settled children", () => {
    render(
      <PageState.Stale updating>
        <span>cached rows</span>
      </PageState.Stale>,
    );

    expect(screen.getByText("cached rows").parentElement).toHaveClass("page-state-stale");
    expect(screen.getByText("cached rows").parentElement).toHaveAttribute("aria-busy", "true");
    expect(screen.getByText("正在更新")).toHaveClass("sr-only");
  });
});
