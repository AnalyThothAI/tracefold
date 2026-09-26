import { researchReturnPath } from "@shared/routing/researchContext";
import { PageHeader } from "@shared/ui/PageHeader";
import { PageReadingContent, PageShell } from "@shared/ui/PageShell";
import { Link, useSearchParams } from "react-router-dom";

import { GroupDetail } from "./NewsMarketGroupTable";
import "./newsMarket.css";

export function NewsMarketItemPage({ token, itemId }: { token: string; itemId: string }) {
  const [params] = useSearchParams();
  const from = researchReturnPath(params.get("research_from"));
  return (
    <PageShell archetype="case" label="市场观察依据">
      <Link to={from ?? "/news/market"}>返回研究列表</Link>
      <PageHeader
        title="市场观察依据"
        subtitle="一条原始观察、它的离散观测过程，以及通知与策略入口。"
      />
      <PageReadingContent>
        <GroupDetail token={token} itemId={itemId} />
      </PageReadingContent>
    </PageShell>
  );
}
