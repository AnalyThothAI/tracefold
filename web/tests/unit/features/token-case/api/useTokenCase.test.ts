import {
  canSeedTokenCasePosts,
  shouldEnableTokenCasePostsQuery,
} from "@features/token-case/api/useTokenCase";
import { tokenCaseFixture } from "@tests/fixtures/tokenCaseFixture";
import { describe, expect, it } from "vitest";

const target = {
  target_type: "Asset" as const,
  target_id: "asset:solana:token:FhoxjfsuStvRQKRXSuB9ZDB7WRGjqhUPxa3NztWspump",
};

describe("useTokenCase post seeding", () => {
  it("seeds only when the dossier first page matches the requested server query", () => {
    const initialPosts = tokenCaseFixture().posts;

    expect(
      canSeedTokenCasePosts({
        initialPosts,
        target,
        window: "1h",
        range: "current_window",
      }),
    ).toBe(true);
  });

  it("does not seed before the dossier first page exists", () => {
    expect(
      canSeedTokenCasePosts({
        initialPosts: null,
        target,
        window: "1h",
        range: "current_window",
      }),
    ).toBe(false);
  });

  it("keeps the posts query idle while the dossier seed is still loading", () => {
    expect(
      shouldEnableTokenCasePostsQuery({
        token: "secret",
        target,
        initialPosts: undefined,
        hasSeedPosts: false,
      }),
    ).toBe(false);
  });

  it("allows a cold or mismatched query to fetch its own first page", () => {
    expect(
      shouldEnableTokenCasePostsQuery({
        token: "secret",
        target,
        initialPosts: null,
        hasSeedPosts: false,
      }),
    ).toBe(true);
    expect(
      shouldEnableTokenCasePostsQuery({
        token: "secret",
        target,
        initialPosts: tokenCaseFixture().posts,
        hasSeedPosts: false,
      }),
    ).toBe(true);
  });

  it("does not refetch the first page when a matching dossier seed is present", () => {
    expect(
      shouldEnableTokenCasePostsQuery({
        token: "secret",
        target,
        initialPosts: tokenCaseFixture().posts,
        hasSeedPosts: true,
      }),
    ).toBe(false);
  });
});
