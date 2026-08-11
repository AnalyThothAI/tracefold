import { RadarPage } from "@features/live";
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

describe("RadarPage", () => {
  it("owns one scan surface with one explicit child interface", () => {
    render(
      <RadarPage>
        <div data-testid="radar-content" />
      </RadarPage>,
    );

    expect(screen.getByTestId("radar-page")).toHaveAttribute("data-page-archetype", "scan");
    expect(screen.getByTestId("radar-content")).toBeInTheDocument();
    expect(screen.queryByText(/Tape/i)).not.toBeInTheDocument();
  });
});
