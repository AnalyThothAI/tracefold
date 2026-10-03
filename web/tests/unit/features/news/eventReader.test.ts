import {
  annotatedSource,
  eventReader,
  eventTiming,
  sourceDisplayName,
} from "@features/news/model/eventReader";
import { newsUpdateDetailFixture } from "@tests/fixtures/newsFixture";
import { describe, expect, it } from "vitest";

describe("event reader", () => {
  it("keeps a completed carried plan and real sent lines while rejecting owed older work", () => {
    const detail = newsUpdateDetailFixture();
    const notification = detail.processing!.notification!;
    const intent = detail.processing!.intents![0];
    const old = notification.content_revision;
    detail.event_update!.content_revision = "e".repeat(64);
    notification.carried = true;
    notification.decided_revision = old;
    notification.decided_at_ms = 1000;
    intent.lines = [{ claim_ref: detail.event_update!.claims[1].ref, text_zh: "实际送达的中文" }];
    expect(eventReader(detail).plan).toBe(notification.plan);
    expect(eventReader(detail).latestSent).toBe(intent);
    expect(eventReader(detail).facts[0]).toMatchObject({ sent: true, text: "实际送达的中文" });
    notification.state = "pending";
    expect(eventReader(detail).currentSent).toEqual([]);
    expect(eventReader(detail).plan).toBeNull();
    notification.state = "done";
    detail.processing!.intents = [];
    notification.plan!.action = "no_notification";
    expect(eventReader(detail).plan).toBe(notification.plan);
    expect(eventTiming(detail).end).toBe(1000);
  });
  it("uses frozen Chinese lines only from sent intents of the adopted version", () => {
    const detail = newsUpdateDetailFixture();
    const claim = detail.event_update!.claims[1];
    const intent = detail.processing!.intents![0];
    intent.lines = [{ claim_ref: claim.ref, text_zh: "实际送达的中文" }];
    const failed = {
      ...intent,
      intent_id: "failed",
      state: "terminal" as const,
      lines: [{ claim_ref: claim.ref, text_zh: "未送达的中文" }],
    };
    detail.processing!.intents!.push(failed);
    expect(eventReader(detail).facts[0]).toMatchObject({
      number: 2,
      text: "实际送达的中文",
      sent: true,
    });
    intent.content_revision = "old-version";
    expect(eventReader(detail).facts.find((fact) => fact.number === 2)).toMatchObject({
      text: undefined,
      sent: false,
    });
  });
  it("preserves original text and every fact number when citations overlap", () => {
    const detail = newsUpdateDetailFixture();
    const claims = detail.event_update!.claims;
    claims[0].citations = [
      {
        ...claims[0].citations[0],
        evidence_ref: "source",
        quote: "interface closes; bridge remains",
      },
    ];
    claims[1].citations = [
      { ...claims[1].citations[0], evidence_ref: "source", quote: "bridge remains" },
    ];
    const text = "Original: interface closes; bridge remains. unchanged";
    const parts = annotatedSource(text, "source", claims);
    expect(parts.map((part) => part.text).join("")).toBe(text);
    expect(parts.find((part) => part.text === "bridge remains")!.numbers).toEqual([1, 2]);
    expect(annotatedSource(text, "other", claims)).toEqual([{ text, numbers: [] }]);
  });
  it("uses the delivered turn's timings and refuses stale notification elapsed time", () => {
    const detail = newsUpdateDetailFixture();
    const intent = detail.processing!.intents![0];
    intent.plan_timings = { snapshot_ms: 10, judgment_ms: 20 };
    detail.processing!.notification!.plan!.timings = { snapshot_ms: 999 };
    expect(eventTiming(detail).parts.find((part) => part.label === "读取与召回")!.ms).toBe(10);
    detail.processing!.intents = [];
    detail.processing!.notification!.content_revision = "old-version";
    expect(eventTiming(detail).elapsed).toBeNull();
    expect(eventTiming(detail).parts).toEqual([]);
  });
  it("shows real origin identity before provider attribution", () => {
    const source = newsUpdateDetailFixture().event_update!.sources![0].source;
    expect(
      sourceDisplayName({
        ...source,
        origin_id: "the defiant",
        publisher_id: "OpenNews",
        attribution: "Other",
      }),
    ).toBe("The Defiant");
  });
});
