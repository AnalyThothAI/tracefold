import { createContext, useContext } from "react";

import type { MarketTargetRef, SocketSnapshot } from "./socketTypes";

export type SocketContextValue = SocketSnapshot & {
  registerMarketTargets: (targets: MarketTargetRef[]) => () => void;
};

export const idleSocketSnapshot: SocketSnapshot = {
  status: "idle",
  lastMessageAt: null,
};

export const SocketContext = createContext<SocketContextValue | null>(null);

export function useSocketSnapshot(): SocketSnapshot {
  const context = useContext(SocketContext);
  if (!context) {
    return idleSocketSnapshot;
  }
  return {
    status: context.status,
    lastMessageAt: context.lastMessageAt,
  };
}

export function useSocketRegistry() {
  const context = useContext(SocketContext);
  if (!context) {
    return null;
  }
  return context.registerMarketTargets;
}
