import type { TokenFlowItem, WindowKey } from "@lib/types";
import { tokenTargetPath } from "@shared/routing/paths";
import { tokenSearchPath } from "@shared/routing/tokenSearch";

import { targetRefFromTokenItem } from "../../../domain/tokenTarget";

const TOKEN_RADAR_DETAIL_WINDOW: WindowKey = "24h";

export function tokenRadarDetailHref(item: TokenFlowItem): string {
  const target = targetRefFromTokenItem(item);
  if (!target) {
    return tokenSearchPath(item, TOKEN_RADAR_DETAIL_WINDOW);
  }
  return tokenTargetPath({
    targetId: target.target_id,
    targetType: target.target_type,
    window: TOKEN_RADAR_DETAIL_WINDOW,
  });
}
