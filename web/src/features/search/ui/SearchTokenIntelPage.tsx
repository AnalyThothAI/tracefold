import { buildTokenCaseViewModel } from "@features/token-case";
import type { TokenCaseDossier } from "@lib/types";
import { TokenCasePanel } from "@shared/ui/case-file";
import { useMemo } from "react";

import type { SearchRouteState } from "../state/searchRouteState";

type SearchTokenIntelPageProps = {
  result: TokenCaseDossier;
  routeState: SearchRouteState;
  onRouteChange: (patch: Partial<SearchRouteState>) => void;
};

export function SearchTokenIntelPage({
  result,
  routeState,
  onRouteChange,
}: SearchTokenIntelPageProps) {
  const searchPosts = useMemo(
    () => ({
      ...result.posts,
      has_more: false,
      next_cursor: null,
    }),
    [result.posts],
  );
  const vm = useMemo(
    () =>
      buildTokenCaseViewModel({
        dossier: result,
        route: {
          window: routeState.window,
          focus: null,
          triggerEventId: null,
        },
        posts: searchPosts,
        isLoadingPosts: false,
        isFetchingNextPage: false,
      }),
    [result, routeState.window, searchPosts],
  );

  return (
    <TokenCasePanel
      vm={vm}
      onWindowChange={(window) => onRouteChange({ window })}
      onLoadMorePosts={() => undefined}
    />
  );
}
