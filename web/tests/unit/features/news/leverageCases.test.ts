import {
  leverageCases,
  leverageHorizon,
  leverageRemaining,
  leverageTimeline,
  type LeverageThresholds,
} from "@features/news/model/leverageCases";
import { tradingLedgerEntries } from "@features/trading";
import { newsOiFrameFixture } from "@tests/fixtures/newsFixture";
import {
  tradingIntentFixture,
  tradingIntentsFixture,
  tradingStatusFixture,
} from "@tests/fixtures/tradingFixture";
import { describe, expect, it } from "vitest";

const STATUS = tradingStatusFixture();
const THRESHOLDS: LeverageThresholds = { gate: STATUS.gate, strategies: STATUS.strategies ?? [] };
const NOW = 1_756_000_000_000;

describe("leverage Case → Intent → Outcome projection", () => {
  it("uses the intent ledger as authority", () => {
    const [item] = leverageCases(
      [newsOiFrameFixture({ event_id: "evt-oi-sol" })],
      tradingLedgerEntries(tradingIntentsFixture({ cases_without_intents: [] })),
      THRESHOLDS,
      NOW,
    );

    expect(item.id).toBe("case-sol");
    expect(item.executionEnvironment).toBe("BINANCE_USDM_DEMO");
    expect(item.capital).toBe("OPEN_PROTECTED");
    expect(leverageTimeline(item).at(-1)).toMatchObject({ key: "intent", label: "Intent" });
  });

  it("uses the code-owned holding horizon and terminal outcome", () => {
    const intent = tradingIntentFixture({
      closed_at_ms: NOW - 1_000,
      execution_phase: "EXIT",
      execution_state: "TERMINAL",
      terminal_outcome: "CLOSED_FLAT",
    });
    const [item] = leverageCases(
      [],
      tradingLedgerEntries(tradingIntentsFixture({ cases_without_intents: [], intents: [intent] })),
      THRESHOLDS,
      NOW,
    );

    expect(leverageHorizon(item)).toBe("3 分钟 · Intent 冻结策略");
    expect(leverageRemaining(item, NOW)).toBe("已了结");
    expect(leverageTimeline(item).at(-1)).toMatchObject({ label: "Outcome" });
  });
});
