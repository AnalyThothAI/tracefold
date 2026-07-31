import { CockpitTopbar } from "@features/cockpit";
import { cleanup, render, screen } from "@testing-library/react";
import { appStatusFixture } from "@tests/fixtures/appRouteFixtures";
import { axe } from "jest-axe";
import { createRef } from "react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

afterEach(() => {
  cleanup();
});

describe("CockpitTopbar", () => {
  it("keeps healthy status out of the task-focused topbar", async () => {
    const { container } = render(
      <MemoryRouter>
        <CockpitTopbar
          search={{ inputRef: createRef<HTMLInputElement>(), onSubmitQuery: vi.fn() }}
          status={{
            socketStatus: "connected",
            lastSocketMessageAt: 1_700_000_000_000,
            status: null,
            statusLoading: false,
            statusError: false,
            configReady: true,
          }}
          onRefresh={vi.fn()}
        />
      </MemoryRouter>,
    );

    expect(screen.getByRole("textbox", { name: "global search" })).toBeInTheDocument();
    expect(screen.getByText("Tracefold")).toBeInTheDocument();
    expect(screen.queryByRole("status", { name: /WebSocket/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "notifications" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "刷新" })).toBeInTheDocument();
    expect(await axe(container)).toHaveNoViolations();
  });

  it("shows realtime anomalies without linking to a retired browser Ops route", () => {
    render(
      <MemoryRouter>
        <CockpitTopbar
          search={{ inputRef: createRef<HTMLInputElement>(), onSubmitQuery: vi.fn() }}
          status={{
            socketStatus: "idle",
            lastSocketMessageAt: 1_700_000_000_000,
            status: null,
            statusLoading: false,
            statusError: false,
            configReady: true,
          }}
          onRefresh={vi.fn()}
        />
      </MemoryRouter>,
    );

    expect(screen.getByRole("status")).toHaveAttribute("title", "实时连接 idle");
    expect(screen.getByText("实时连接 idle")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Open ops diagnostics" })).not.toBeInTheDocument();
  });

  it("shows the first runtime reason without a permanent health beacon", () => {
    render(
      <MemoryRouter>
        <CockpitTopbar
          search={{ inputRef: createRef<HTMLInputElement>(), onSubmitQuery: vi.fn() }}
          status={{
            socketStatus: "connected",
            lastSocketMessageAt: 1_700_000_000_000,
            status: appStatusFixture({
              ok: false,
              reasons: ["runtime_missing"],
            }),
            statusLoading: false,
            statusError: false,
            configReady: true,
          }}
          onRefresh={vi.fn()}
        />
      </MemoryRouter>,
    );

    expect(screen.getByRole("status")).toHaveAttribute("title", "runtime_missing");
    expect(screen.getByText("runtime_missing")).toBeInTheDocument();
  });
});
