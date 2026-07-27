import type { SearchInspectData } from "@lib/types";
import { useMarketSubscription } from "@shared/socket/useMarketSubscription";
import * as PageState from "@shared/ui/PageState";
import { useMemo } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";

import { useSearchInspectQuery } from "../api/useSearchInspectQuery";
import {
  parseSearchRouteState,
  serializeSearchRouteState,
  type SearchRouteState,
} from "../state/searchRouteState";

import { SearchAmbiguousCase } from "./SearchAmbiguousCase";
import { SearchTokenIntelPage } from "./SearchTokenIntelPage";
import { SearchTopicCase } from "./SearchTopicCase";
import "./search.css";
import "./searchDetails.css";

export function SearchIntelPage({ token }: { token?: string }) {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const routeState = parseSearchRouteState(searchParams);
  const query = useSearchInspectQuery({ ...routeState, token });
  const data = query.data?.data ?? null;
  const marketTargets = useMemo(() => searchMarketTargets(data), [data]);
  useMarketSubscription(marketTargets);

  const updateRoute = (patch: Partial<SearchRouteState>) => {
    const next = serializeSearchRouteState({ ...routeState, ...patch });
    navigate({ pathname: "/search", search: `?${next.toString()}` });
  };

  return (
    <section aria-label="Search Intel" className="search-intel-page" data-page-archetype="case">
      <SearchTopBar data={data} routeState={routeState} />

      {!routeState.q ? (
        <PageState.Empty title="输入 token、CA、@handle 或关键词后手动检索。" />
      ) : query.error ? (
        <PageState.Error error={query.error} />
      ) : query.isPending || !data ? (
        <PageState.Loading layout="route" rows={5} label="loading search results" />
      ) : (
        <SearchResultBody data={data} routeState={routeState} onRouteChange={updateRoute} />
      )}
    </section>
  );
}

function searchMarketTargets(data: SearchInspectData | null) {
  const target = data?.query.result_kind === "token_result" ? data.token_result?.target : null;
  if (!target?.target_type || !target.target_id) {
    return [];
  }
  return [{ target_type: target.target_type, target_id: target.target_id }];
}

function SearchTopBar({
  data,
  routeState,
}: {
  data: SearchInspectData | null;
  routeState: SearchRouteState;
}) {
  const resultKind = data?.query.result_kind ?? "pending";
  return (
    <header className="search-intel-topbar">
      <div className="search-intel-titleline">
        <span>case inspect</span>
        <h2>Search Intel</h2>
        <strong>{routeState.q || "empty query"}</strong>
      </div>
      <div className="search-route-meta" aria-label="Search resolver">
        <code>{resultKind}</code>
        {data?.resolver.reasons.map((reason, index) => (
          <code key={`${reason}-${index}`}>{reason}</code>
        ))}
        <code>{data?.query.window ?? routeState.window}</code>
      </div>
    </header>
  );
}

function SearchResultBody({
  data,
  routeState,
  onRouteChange,
}: {
  data: SearchInspectData;
  routeState: SearchRouteState;
  onRouteChange: (patch: Partial<SearchRouteState>) => void;
}) {
  if (data.query.result_kind === "token_result" && data.token_result) {
    return (
      <SearchTokenIntelPage
        result={data.token_result}
        routeState={routeState}
        onRouteChange={onRouteChange}
      />
    );
  }
  if (data.query.result_kind === "ambiguous_result" && data.ambiguous_result) {
    return <SearchAmbiguousCase data={data} result={data.ambiguous_result} />;
  }
  if (data.query.result_kind === "topic_result" && data.topic_result) {
    return <SearchTopicCase data={data} result={data.topic_result} />;
  }
  return <PageState.Empty title="没有可展示的 search 结果。" />;
}
