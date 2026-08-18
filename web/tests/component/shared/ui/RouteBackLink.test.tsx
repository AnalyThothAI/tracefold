import { RouteBackLink } from "@shared/ui/RouteBackLink";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { renderWithProviders } from "@tests/render/renderWithProviders";
import { Route, Routes } from "react-router-dom";
import { describe, expect, it } from "vitest";

describe("RouteBackLink", () => {
  it("renders an accessible return link", () => {
    renderWithProviders(<RouteBackLink to="/news" label="返回" ariaLabel="返回事件流" />, {
      route: "/news/events/evt-1",
    });

    const link = screen.getByRole("link", { name: "返回事件流" });
    expect(link).toHaveAttribute("href", "/news");
    expect(link).toHaveTextContent("返回");
  });

  it("navigates through the active router instead of reloading the document", () => {
    const { container } = renderWithProviders(
      <Routes>
        <Route path="/news" element={<h1>News</h1>} />
        <Route
          path="/news/events/:eventId"
          element={<RouteBackLink to="/news" label="返回" ariaLabel="返回事件流" />}
        />
      </Routes>,
      {
        route: "/news/events/evt-1",
      },
    );

    fireEvent.click(within(container).getByRole("link", { name: "返回事件流" }));

    expect(within(container).getByRole("heading", { name: "News" })).toBeInTheDocument();
  });

  it("does not require a router provider", () => {
    const { container } = render(<RouteBackLink to="/news" label="返回" ariaLabel="返回事件流" />);

    expect(within(container).getByRole("link", { name: "返回事件流" })).toHaveAttribute(
      "href",
      "/news",
    );
  });
});
