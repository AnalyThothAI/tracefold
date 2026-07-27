import type { LiveMarketUpdatePayload } from "@lib/types";

export const socketScenario: {
  lastMessageAt: number | null;
  liveMarketUpdates: LiveMarketUpdatePayload[];
  status: string;
} = {
  lastMessageAt: 1_777_770_000_000,
  liveMarketUpdates: [],
  status: "connected",
};

export function resetSocketScenario() {
  socketScenario.status = "connected";
  socketScenario.liveMarketUpdates = [];
  socketScenario.lastMessageAt = 1_777_770_000_000;
}
