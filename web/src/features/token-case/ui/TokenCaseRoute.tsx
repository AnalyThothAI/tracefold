import { getAuthToken } from "@lib/api/client";
import { useMarketSubscription } from "@shared/socket/useMarketSubscription";
import * as PageState from "@shared/ui/PageState";
import { TokenCasePanel } from "@shared/ui/case-file";
import { useMemo } from "react";
import { useParams, useSearchParams } from "react-router-dom";

import type { TargetRef } from "../../../domain/tokenTarget";
import {
  mergeTokenCasePostPages,
  useTokenCase,
  useTokenCasePosts,
  useTriggerTargetPost,
} from "../api/useTokenCase";
import { buildTokenCaseViewModel } from "../model/buildTokenCaseViewModel";
import {
  parseTokenCaseRouteState,
  serializeTokenCaseRouteState,
  type TokenCaseRouteState,
} from "../state/tokenCaseRouteState";

export function TokenCaseRoute({ token: tokenProp }: { token?: string } = {}) {
  const { targetType, targetId } = useParams<{ targetType: string; targetId: string }>();
  const [searchParams, setSearchParams] = useSearchParams();
  const routeState = parseTokenCaseRouteState(searchParams);
  const target = useMemo(() => parseTarget(targetType, targetId), [targetId, targetType]);
  const token = tokenProp ?? getAuthToken() ?? "";
  const dossierQuery = useTokenCase({
    token,
    target,
    window: routeState.window,
    postsLimit: 24,
  });
  const dossier = dossierQuery.data?.data ?? null;
  const initialPosts = dossierQuery.isPending ? undefined : (dossier?.posts ?? null);
  const postsQuery = useTokenCasePosts({
    token,
    target,
    window: routeState.window,
    postsLimit: 24,
    initialPosts,
  });
  const mergedPosts = mergeTokenCasePostPages(postsQuery.data?.pages);
  const evidencePosts = mergedPosts ?? dossier?.posts ?? null;
  const focusedEventId = routeState.focus === "trigger" ? routeState.triggerEventId : null;
  const triggerAlreadyLoaded = Boolean(
    focusedEventId && evidencePosts?.items.some((post) => post.event_id === focusedEventId),
  );
  const shouldLookupTrigger = Boolean(
    dossier && focusedEventId && evidencePosts && !triggerAlreadyLoaded,
  );
  const triggerQuery = useTriggerTargetPost({
    token,
    target,
    eventId: focusedEventId,
    enabled: shouldLookupTrigger,
  });
  const subscribedTargets = useMemo(() => (target ? [target] : []), [target]);
  useMarketSubscription(subscribedTargets);

  const updateRoute = (patch: Partial<TokenCaseRouteState>) => {
    setSearchParams(serializeTokenCaseRouteState({ ...routeState, ...patch }));
  };

  if (!target) {
    return (
      <PageState.Empty
        title="Token case target missing"
        hint="Asset and CexToken routes are supported."
      />
    );
  }
  if (!token) {
    return <PageState.Loading layout="route" rows={4} label="loading token case session" />;
  }
  if (dossierQuery.isError) {
    return <PageState.Error error={dossierQuery.error} />;
  }
  if (dossierQuery.isPending) {
    return <PageState.Loading layout="route" rows={5} label="loading token case" />;
  }
  if (!dossier) {
    return <PageState.Empty title="Token case unavailable" />;
  }

  const vm = buildTokenCaseViewModel({
    dossier,
    route: routeState,
    posts: mergedPosts,
    isLoadingPosts: postsQuery.isLoading,
    isFetchingNextPage: postsQuery.isFetchingNextPage,
    focusedPost: triggerQuery.data ?? null,
    focusedEventLoading: shouldLookupTrigger && triggerQuery.isPending,
    focusedEventUnavailable:
      shouldLookupTrigger &&
      !triggerQuery.isPending &&
      (triggerQuery.isError || !triggerQuery.data),
  });

  return (
    <TokenCasePanel
      vm={vm}
      onWindowChange={(window) => updateRoute({ window })}
      onLoadMorePosts={() => {
        if (postsQuery.hasNextPage) {
          void postsQuery.fetchNextPage();
        }
      }}
    />
  );
}

function parseTarget(
  targetType: string | undefined,
  targetId: string | undefined,
): TargetRef | null {
  if ((targetType !== "Asset" && targetType !== "CexToken") || !targetId) {
    return null;
  }
  return { target_type: targetType, target_id: targetId };
}
