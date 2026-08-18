import { RouteBackLink } from "@shared/ui/RouteBackLink";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { renderWithProviders } from "@tests/render/renderWithProviders";
import { Route, Routes } from "react-router-dom";
import { describe, expect, it } from "vitest";

describe("RouteBackLink", () => {
  it("renders an accessible return link", () => {
    renderWithProviders(<RouteBackLink to="/search" label="返回" ariaLabel="返回 Search" />, {
      route: "/token/Asset/x",
    });

    const link = screen.getByRole("link", { name: "返回 Search" });
    expect(link).toHaveAttribute("href", "/search");
    expect(link).toHaveTextContent("返回");
  });

  it("navigates through the active router instead of reloading the document", () => {
    const { container } = renderWithProviders(
      <Routes>
        <Route path="/search" element={<h1>Search</h1>} />
        <Route
          path="/token/:targetType/:targetId"
          element={<RouteBackLink to="/search" label="返回" ariaLabel="返回 Search" />}
        />
      </Routes>,
      {
        route: "/token/Asset/x",
      },
    );

    fireEvent.click(within(container).getByRole("link", { name: "返回 Search" }));

    expect(within(container).getByRole("heading", { name: "Search" })).toBeInTheDocument();
  });

  it("does not require a router provider", () => {
    const { container } = render(
      <RouteBackLink to="/search" label="返回" ariaLabel="返回 Search" />,
    );

    expect(within(container).getByRole("link", { name: "返回 Search" })).toHaveAttribute(
      "href",
      "/search",
    );
  });
});
