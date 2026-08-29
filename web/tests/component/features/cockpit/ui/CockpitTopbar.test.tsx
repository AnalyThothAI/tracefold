import { CockpitTopbar } from "@features/cockpit";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { appStatusFixture } from "@tests/fixtures/appRouteFixtures";
import { axe } from "jest-axe";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

afterEach(() => {
  cleanup();
});

const healthyStatus = {
  status: null,
  statusLoading: false,
  statusError: false,
  configReady: true,
};

describe("CockpitTopbar", () => {
  it("keeps its News search draft synchronized with the URL-owned query", () => {
    const search = {
      onSubmitQuery: vi.fn(),
      query: "bitcoin",
    };
    const { rerender } = render(
      <MemoryRouter>
        <CockpitTopbar title="新闻事件流" search={search} status={healthyStatus} />
      </MemoryRouter>,
    );
    const input = screen.getByRole("textbox", { name: "news search" });
    expect(input).toHaveValue("bitcoin");
    expect(input).toHaveAttribute("placeholder", "标的 / 事件关键词");

    fireEvent.change(input, { target: { value: "local draft" } });
    expect(input).toHaveValue("local draft");

    rerender(
      <MemoryRouter>
        <CockpitTopbar
          title="新闻事件流"
          search={{ ...search, query: "ethereum" }}
          status={healthyStatus}
        />
      </MemoryRouter>,
    );
    expect(input).toHaveValue("ethereum");

    rerender(
      <MemoryRouter>
        <CockpitTopbar
          title="新闻事件流"
          search={{ ...search, query: "" }}
          status={healthyStatus}
        />
      </MemoryRouter>,
    );
    expect(input).toHaveValue("");
  });

  // #256: one topbar on every route. The scan copy and the inert `/` keycap are no longer route-scoped, and
  // there is no refresh control anywhere — every surface polls, and a button that re-asks is theatre.
  it("renders the same search copy and keycap on a secondary route", () => {
    render(
      <MemoryRouter>
        <CockpitTopbar
          search={{ onSubmitQuery: vi.fn() }}
          status={healthyStatus}
          title="事件详情"
        />
      </MemoryRouter>,
    );

    expect(screen.getByRole("textbox", { name: "news search" })).toHaveAttribute(
      "placeholder",
      "标的 / 事件关键词",
    );
    expect(screen.queryByRole("button", { name: "刷新" })).toBeNull();
    expect(document.querySelector(".cockpit-searchbar-keycap")).not.toBeNull();
  });

  it("submits the trimmed News query from the single search entry", () => {
    const onSubmitQuery = vi.fn();
    render(
      <MemoryRouter>
        <CockpitTopbar title="新闻事件流" search={{ onSubmitQuery }} status={healthyStatus} />
      </MemoryRouter>,
    );

    fireEvent.change(screen.getByRole("textbox", { name: "news search" }), {
      target: { value: "BTC ETF" },
    });
    fireEvent.submit(screen.getByRole("textbox", { name: "news search" }).closest("form")!);

    expect(onSubmitQuery).toHaveBeenCalledWith("BTC ETF");
    expect(screen.queryByRole("button", { name: "Main" })).not.toBeInTheDocument();
  });

  it("keeps healthy status out of the task-focused topbar", async () => {
    const { container } = render(
      <MemoryRouter>
        <CockpitTopbar
          title="新闻事件流"
          search={{ onSubmitQuery: vi.fn() }}
          status={{ ...healthyStatus, status: appStatusFixture() }}
        />
      </MemoryRouter>,
    );

    expect(screen.getByRole("textbox", { name: "news search" })).toBeInTheDocument();
    // The frame says where you are; the product name lives in the sidebar, which owns identity.
    expect(screen.getByText("新闻事件流")).toBeInTheDocument();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    expect(screen.queryByText(/实时连接|WebSocket/)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "notifications" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "刷新" })).not.toBeInTheDocument();
    expect(await axe(container)).toHaveNoViolations();
  });

  it("shows the first runtime reason without a permanent health beacon or Ops link", () => {
    render(
      <MemoryRouter>
        <CockpitTopbar
          title="新闻事件流"
          search={{ onSubmitQuery: vi.fn() }}
          status={{
            ...healthyStatus,
            status: appStatusFixture({
              runtime: {
                ...appStatusFixture().runtime,
                ok: false,
                reasons: ["runtime_missing"],
              },
            }),
          }}
        />
      </MemoryRouter>,
    );

    expect(screen.getByRole("status")).toHaveAttribute("title", "runtime_missing");
    expect(screen.getByText("runtime_missing")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Open ops diagnostics" })).not.toBeInTheDocument();
  });

  it("reports a failed status check and an unready configuration", () => {
    const { rerender } = render(
      <MemoryRouter>
        <CockpitTopbar
          title="新闻事件流"
          search={{ onSubmitQuery: vi.fn() }}
          status={{ ...healthyStatus, statusError: true }}
        />
      </MemoryRouter>,
    );
    expect(screen.getByRole("status")).toHaveAttribute("title", "状态检查失败");

    rerender(
      <MemoryRouter>
        <CockpitTopbar
          title="新闻事件流"
          search={{ onSubmitQuery: vi.fn() }}
          status={{ ...healthyStatus, configReady: false }}
        />
      </MemoryRouter>,
    );
    expect(screen.getByRole("status")).toHaveAttribute("title", "配置未就绪");
  });
});
