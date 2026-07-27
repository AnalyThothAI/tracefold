import { buildTokenCaseViewModel } from "@features/token-case";
import { TokenCasePanel } from "@shared/ui/case-file";
import { render, screen } from "@testing-library/react";
import { tokenCaseFixture } from "@tests/fixtures/tokenCaseFixture";
import { describe, expect, it, vi } from "vitest";

describe("TokenCasePanel", () => {
  it("renders the shared token case anatomy", () => {
    const vm = buildTokenCaseViewModel({
      dossier: tokenCaseFixture(),
      route: { window: "1h" },
    });

    render(<TokenCasePanel vm={vm} onLoadMorePosts={vi.fn()} onWindowChange={vi.fn()} />);

    expect(screen.getByRole("region", { name: /Token case/i })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /\$HANSA/i })).toBeInTheDocument();
    expect(screen.getByText("Mention Timeline")).toBeInTheDocument();
    expect(screen.getByText("Live Market")).toBeInTheDocument();
    expect(screen.getByText("Data Gaps")).toBeInTheDocument();
    expect(screen.getAllByText(/原文/)[0]).toBeInTheDocument();
  });
});
