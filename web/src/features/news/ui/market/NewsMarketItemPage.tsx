import { PageShell } from "@shared/ui/PageShell";
import { Link, useLocation } from "react-router-dom";

import { NewsPageHeader } from "../chrome/NewsChrome";

import { GroupDetail } from "./NewsMarketGroupTable";
import "./newsMarket.css";

export function NewsMarketItemPage({ token, itemId }: { token: string; itemId: string }) {
  const location = useLocation();
  const from = (location.state as { researchFrom?: string } | null)?.researchFrom;
  return (
    <PageShell archetype="case" label="市场观察依据">
      <Link to={from?.startsWith("/news/") || from?.startsWith("/trading") ? from : "/news/market"}>
        返回研究列表
      </Link>
      <NewsPageHeader
        title="市场观察依据"
        subtitle="一条原始观察、它的离散观测过程，以及通知与策略入口。"
      />
      <GroupDetail token={token} itemId={itemId} />
    </PageShell>
  );
}
