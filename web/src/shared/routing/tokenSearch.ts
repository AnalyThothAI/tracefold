import type { TokenFlowItem, WindowKey } from "@lib/types";

import { searchPath } from "./paths";

export function tokenSearchQuery(item: TokenFlowItem): string {
  const address = item.identity.address?.trim();
  if (address) {
    return address;
  }

  const instId = item.identity.inst_id?.trim();
  if (instId) {
    return instId;
  }

  const targetId = item.identity.target_id?.trim();
  if (targetId) {
    return targetId;
  }

  const symbol = item.identity.symbol?.trim();
  if (symbol) {
    return symbol.startsWith("$") ? symbol : `$${symbol}`;
  }

  return item.identity.identity_key;
}

export function tokenSearchPath(item: TokenFlowItem, window: WindowKey): string {
  return searchPath({
    q: tokenSearchQuery(item),
    window,
  });
}
