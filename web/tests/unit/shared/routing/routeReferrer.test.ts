import { routeReferrerFromState } from "@shared/routing/routeReferrer";
import { describe, expect, it } from "vitest";

const FEED = { label: "事件流", to: "/news" };

describe("routeReferrerFromState", () => {
  it("carries a named route back with its query state", () => {
    expect(routeReferrerFromState({ label: "事件流", to: "/news?outcome=held&hours=168" })).toEqual(
      {
        label: "事件流",
        to: "/news?outcome=held&hours=168",
      },
    );
    expect(routeReferrerFromState({ label: "市场事实", to: "/news/market" })).toEqual({
      label: "市场事实",
      to: "/news/market",
    });
  });

  it("falls back to the feed for a cold URL", () => {
    // A shared token-page link carries no referrer, and a back control that pointed nowhere would be worse
    // than one that points at the feed.
    expect(routeReferrerFromState(null)).toEqual(FEED);
    expect(routeReferrerFromState(undefined)).toEqual(FEED);
    expect(routeReferrerFromState({})).toEqual(FEED);
  });

  it("refuses a destination this console does not serve", () => {
    /*
     * `location.state` is whatever the last navigation put there, including across a browser restore, and
     * a back link is a real navigation. Only paths the router actually answers are accepted — an absolute
     * URL, a protocol-relative one and a retired route all fall back rather than becoming a link.
     */
    for (const to of [
      "https://example.test/phish",
      "//example.test/phish",
      "/news/review",
      "javascript:alert(1)",
      "/news/symbols/BTC",
      // The route `/news/market` replaced (#553 PR-1). A restored browser session can still carry it.
      "/news/oi",
    ]) {
      expect(routeReferrerFromState({ label: "看起来像事件流", to })).toEqual(FEED);
    }
  });

  it("refuses a state whose fields are not strings", () => {
    expect(routeReferrerFromState({ label: 1, to: "/news" })).toEqual(FEED);
    expect(routeReferrerFromState({ label: "事件流", to: 2 })).toEqual(FEED);
  });
});
