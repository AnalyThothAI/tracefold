import { getApi, postApi } from "@lib/api/client";
import type { components } from "@lib/types/openapi";
import { queryKeys } from "@shared/query/queryKeys";
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

type NewsSchemas = components["schemas"];

export type NewsFeed = NewsSchemas["NewsFeedData"];
export type NewsFeedCounts = NewsSchemas["NewsFeedCountsData"];
export type NewsFeedEvent = NewsSchemas["NewsFeedEventData"];
export type NewsEvent = NewsSchemas["NewsEventData"];
export type NewsAssetRef = NewsSchemas["NewsAssetRefData"];
export type NewsSymbolNormalization = NewsSchemas["NewsSymbolNormalizationData"];
export type NewsEventDetail = NewsSchemas["NewsEventDetailData"];
export type NewsEventMember = NewsSchemas["NewsEventMemberData"];
export type NewsVerdict = NewsSchemas["NewsVerdictData"];
export type NewsDelivery = NewsSchemas["NewsDeliveryData"];
export type NewsDeliverySummary = NewsSchemas["NewsDeliverySummaryData"];
export type NewsTriageSummary = NewsSchemas["NewsTriageSummaryData"];
export type NewsStatus = NewsSchemas["NewsStatusData"];
export type NewsIncident = NewsSchemas["NewsIncidentData"];
export type NewsOutcome = NewsSchemas["NewsOutcomeData"];
export type NewsOutcomeKind = NewsOutcome["kind"];
export type NewsOutcomeGroup = NewsOutcome["group"];
export type NewsTimelineStep = NewsSchemas["NewsTimelineStepData"];
export type NewsHealth = NewsSchemas["NewsHealthData"];
export type NewsHealthItem = NewsSchemas["NewsHealthItemData"];
export type NewsHealthLevel = NewsHealthItem["level"];
export type NewsFunnel = NewsSchemas["NewsFunnelData"];
export type NewsReasonCount = NewsSchemas["NewsReasonCountData"];
export type NewsQuote = NewsSchemas["NewsQuoteData"];
export type NewsQuoteState = NewsQuote["state"];
export type NewsQuotes = NewsSchemas["NewsQuotesData"];
export type NewsReaction = NewsSchemas["NewsReactionSummaryData"];
export type NewsReactionState = NewsReaction["state"];
export type NewsEventReaction = NewsSchemas["NewsEventReactionData"];
export type NewsReview = NewsSchemas["NewsReviewData"];
export type NewsReviewEvidence = NewsSchemas["NewsReviewEvidenceData"];
export type NewsReviewSubmit = NewsSchemas["NewsReviewSubmitData"];
export type NewsReviewTask = NewsSchemas["NewsReviewTaskData"];
export type NewsEventRubricSubmission = NewsSchemas["EventRubricSubmission"];
export type NewsBlindPairwiseSubmission = NewsSchemas["BlindPairwiseSubmission"];
export type NewsExternalMissSubmission = NewsSchemas["ExternalMissSubmission"];
export type NewsReviewResponseSubmission = NewsEventRubricSubmission | NewsBlindPairwiseSubmission;
export type NewsReviewCoverage = NewsSchemas["NewsReviewCoverageData"];
export type NewsReviewDirection = NewsSchemas["NewsReviewDirectionData"];
export type NewsReviewMagnitude = NewsSchemas["NewsReviewMagnitudeData"];
export type NewsReviewEventType = NewsSchemas["NewsReviewEventTypeData"];
export type NewsReviewMiss = NewsSchemas["NewsReviewMissData"];
export type NewsPriceStatus = NewsSchemas["NewsPriceStatusData"];
export type NewsFeedOi = NewsSchemas["NewsFeedOiData"];
export type NewsOiStatus = NewsSchemas["NewsOiStatusData"];
export type NewsOiPolicy = NewsSchemas["NewsOiPolicyData"];
export type NewsOiTradeFloors = NewsSchemas["NewsOiTradeFloorsData"];
export type NewsOiWindowSymbol = NewsSchemas["NewsOiWindowSymbolData"];
export type NewsSymbol = NewsSchemas["NewsSymbolData"];
export type NewsSymbolContract = NewsSchemas["NewsSymbolContractData"];

export type NewsFeedDecision = "push" | "escalate" | "drop" | "throttled" | "degraded";
export type NewsFeedOutcome = NewsOutcomeGroup;
export type NewsFeedDirection = "bullish" | "bearish" | "neutral";
export type NewsFeedChannel = "news" | "oi";
export const NEWS_FEED_OUTCOMES: readonly NewsFeedOutcome[] = ["pushed", "held", "pending"];
export const NEWS_FEED_DIRECTIONS: readonly NewsFeedDirection[] = ["bullish", "bearish", "neutral"];
export const NEWS_FEED_CHANNELS: readonly NewsFeedChannel[] = ["news", "oi"];
/** The three windows in the approved Event-feed visual. */
export const NEWS_FEED_HOURS: readonly number[] = [1, 24, 168];
export const NEWS_FEED_DEFAULT_HOURS = 24;

export const NEWS_FEED_DECISIONS: readonly NewsFeedDecision[] = [
  "push",
  "escalate",
  "drop",
  "throttled",
  "degraded",
];
export const NEWS_FEED_PAGE_SIZE = 25;
export const NEWS_FEED_REFETCH_MS = 3_000;
export const NEWS_STATUS_REFETCH_MS = 15_000;
export const NEWS_EVENT_REFETCH_MS = 15_000;
/**
 * Quotes have their own query key and their own rhythm (#88): a price that changed must not make the feed
 * and its counts query return a new body, and polling faster than the collector writes is pure noise. The
 * server refreshes每 20 s, so 15 s keeps the reader within one cycle of the truth without asking three times
 * for the same bytes.
 */
export const NEWS_QUOTES_REFETCH_MS = 15_000;
/** The review window is a stable aggregate; a minute is plenty and keeps a 720 h query off the hot path. */
export const NEWS_REVIEW_REFETCH_MS = 60_000;
/** `/api/news/quotes` accepts at most this many symbols; the hook batches the visible rows into one call. */
export const NEWS_QUOTES_SYMBOL_MAX = 100;
export const NEWS_REVIEW_HOURS: readonly number[] = [24, 72, 168, 720];
export const NEWS_REVIEW_DEFAULT_HOURS = 168;

export type NewsFeedFilters = {
  admission: string | null;
  decision: NewsFeedDecision | null;
  family: string | null;
  hours: number | null;
  outcome: NewsFeedOutcome | null;
  directions: NewsFeedDirection[];
  channels: NewsFeedChannel[];
  q: string;
  symbol: string | null;
};

/**
 * The 持仓异动 monitor's tabs (#207). `全部` is the absence of the filter, so it is not a server value.
 *
 * The other three are the server's own grouping of the judge's rule names — `pushed` is the one qualifying
 * rule, `withheld` the three threshold rules, `parse_failed` the provider-contract failure. `decision`
 * cannot express the split: a frame held by a threshold and one whose template stopped parsing are both
 * `drop`, and both carry `override_rule = telemetry_deterministic`.
 */
export type NewsOiOutcome = "pushed" | "withheld" | "parse_failed";
export const NEWS_OI_TABS = ["all", "pushed", "withheld", "parse_failed"] as const;
export type NewsOiTab = (typeof NEWS_OI_TABS)[number];
/** The monitor is a 24 h read: the counts it shows beside each tab are the server's 24 h aggregates. */
export const NEWS_OI_HOURS = 24;
export const NEWS_OI_PAGE_SIZE = 50;
/** The frame table follows the feed's own rhythm — a telemetry frame lands every few minutes. */
export const NEWS_OI_REFETCH_MS = 5_000;
export const NEWS_OI_ADMISSION = "telemetry_deterministic";

const fetchNewsFeed = async (token: string, filters: NewsFeedFilters, cursor: string | null) =>
  (
    await getApi<NewsFeed>("/api/news/feed", {
      etagKey: `news-feed:${JSON.stringify([
        filters.q,
        filters.family,
        filters.admission,
        filters.decision,
        filters.symbol,
        filters.outcome,
        filters.hours,
        filters.directions.join(","),
        filters.channels.join(","),
        cursor ?? "first",
      ])}`,
      params: {
        admission: filters.admission,
        cursor,
        decision: filters.decision,
        direction: filters.directions.join(",") || null,
        family: filters.family,
        hours: filters.hours ?? undefined,
        limit: NEWS_FEED_PAGE_SIZE,
        outcome: filters.outcome,
        q: filters.q,
        channel: filters.channels.join(",") || null,
        symbol: filters.symbol,
      },
      token,
    })
  ).data;

export const useNewsFeedWithToken = (token: string, filters: NewsFeedFilters) =>
  useQuery({
    enabled: Boolean(token),
    queryKey: queryKeys.newsFeed(filters),
    queryFn: () => fetchNewsFeed(token, filters, null),
    refetchInterval: NEWS_FEED_REFETCH_MS,
    staleTime: 2_000,
  });

export const useNewsFeedHistoryWithToken = (
  token: string,
  filters: NewsFeedFilters,
  firstCursor: string | null,
  enabled: boolean,
) =>
  useInfiniteQuery({
    enabled: Boolean(token && firstCursor && enabled),
    queryKey: queryKeys.newsFeedHistory(filters, firstCursor ?? ""),
    queryFn: ({ pageParam }) => fetchNewsFeed(token, filters, pageParam),
    initialPageParam: firstCursor ?? "",
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
    staleTime: Number.POSITIVE_INFINITY,
  });

const fetchNewsOiFeed = async (token: string, tab: NewsOiTab, cursor: string | null) =>
  (
    await getApi<NewsFeed>("/api/news/feed", {
      etagKey: `news-oi-feed:${tab}:${cursor ?? "first"}`,
      params: {
        admission: NEWS_OI_ADMISSION,
        cursor,
        hours: NEWS_OI_HOURS,
        limit: NEWS_OI_PAGE_SIZE,
        // `all` is sent, not omitted: it narrows nothing, but it is how the request identifies itself as
        // the monitor, which is what lets the server skip the outcome-group aggregate this page never reads.
        oi: tab,
      },
      token,
    })
  ).data;

/**
 * One page of #137's deterministic telemetry frames, filtered server-side by the gate that judged them.
 *
 * Every tab is a real request rather than a filter over a loaded page: the counts beside the tabs are the
 * server's 24 h aggregates, so a client-side split would leave them describing the window while the rows
 * below described one page of it.
 */
export const useNewsOiFeedWithToken = (token: string, tab: NewsOiTab) =>
  useQuery({
    enabled: Boolean(token),
    queryKey: queryKeys.newsOiFeed(tab),
    queryFn: () => fetchNewsOiFeed(token, tab, null),
    refetchInterval: NEWS_OI_REFETCH_MS,
    staleTime: 2_000,
  });

/**
 * The pages behind the first one, on an explicit action (`加载更多帧`) — never automatic scroll.
 *
 * A 24 h window holds roughly 190 frames and a tab can name more than one page of them, so without this the
 * table would show 50 rows under a tab labelled 136 and offer no way to the rest. Loaded pages are frozen:
 * only the first page follows the 5 s poll, exactly as the Event feed's history does.
 */
export const useNewsOiFeedHistoryWithToken = (
  token: string,
  tab: NewsOiTab,
  firstCursor: string | null,
  enabled: boolean,
) =>
  useInfiniteQuery({
    enabled: Boolean(token && firstCursor && enabled),
    queryKey: queryKeys.newsOiFeedHistory(tab, firstCursor ?? ""),
    queryFn: ({ pageParam }) => fetchNewsOiFeed(token, tab, pageParam),
    initialPageParam: firstCursor ?? "",
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
    staleTime: Number.POSITIVE_INFINITY,
  });

export const useNewsEventWithToken = (token: string, eventId?: string | null) =>
  useQuery({
    enabled: Boolean(token && eventId),
    queryKey: queryKeys.newsEvent(eventId ?? ""),
    queryFn: async () =>
      (
        await getApi<NewsEventDetail>(`/api/news/events/${encodeURIComponent(eventId ?? "")}`, {
          etagKey: `news-event:${eventId ?? ""}`,
          token,
        })
      ).data,
    refetchInterval: NEWS_EVENT_REFETCH_MS,
    staleTime: 5_000,
  });

export const useNewsStatusWithToken = (token: string) =>
  useQuery({
    enabled: Boolean(token),
    queryKey: queryKeys.newsStatus(),
    queryFn: async () =>
      (
        await getApi<NewsStatus>("/api/news/status", {
          etagKey: "news-status",
          token,
        })
      ).data,
    refetchInterval: NEWS_STATUS_REFETCH_MS,
    staleTime: 5_000,
  });

/**
 * Current quotes for one deduplicated symbol batch (#88).
 *
 * The batch is sorted and deduplicated before it becomes a query key, so two surfaces asking for the same
 * symbols share one cache entry and one request. The server deduplicates again — a client cannot multiply
 * repository or provider work by repeating a symbol.
 */
/**
 * What one `base_symbol` is (#207 PR-W1). Identity, and nothing that moves.
 *
 * No `refetchInterval`: the universe snapshot lands on a schedule measured in hours, and the three things
 * on the token page that *do* move — Events, quote, rank window — each arrive on their own key at their own
 * rhythm. Polling this would re-render the page for bytes that cannot have changed.
 */
export const useNewsSymbolWithToken = (token: string, base: string) => {
  const normalized = base.trim().toUpperCase().replace(/^XYZ-/, "");
  return useQuery({
    enabled: Boolean(token && normalized),
    queryKey: queryKeys.newsSymbol(normalized),
    queryFn: async () =>
      (
        await getApi<NewsSymbol>(`/api/news/symbols/${encodeURIComponent(normalized)}`, {
          etagKey: `news-symbol:${normalized}`,
          token,
        })
      ).data,
    staleTime: 60_000,
  });
};

export const useNewsQuotesWithToken = (token: string, symbols: readonly string[]) => {
  const batch = [...new Set(symbols.map((symbol) => symbol.trim()).filter(Boolean))]
    .sort()
    .slice(0, NEWS_QUOTES_SYMBOL_MAX);
  return useQuery({
    enabled: Boolean(token && batch.length),
    queryKey: queryKeys.newsQuotes(batch),
    queryFn: async () =>
      (
        await getApi<NewsQuotes>("/api/news/quotes", {
          etagKey: `news-quotes:${batch.join(",")}`,
          params: { symbols: batch.join(",") },
          token,
        })
      ).data,
    refetchInterval: NEWS_QUOTES_REFETCH_MS,
    staleTime: 2_000,
  });
};

export type NewsReviewQuery = {
  view: "queue" | "coverage" | "proposals" | "market";
  mode?: "event" | "pairwise";
  status?: "pending" | "accepted" | "all";
  hours: number;
  event?: string;
  task?: string;
  proposal?: string;
  cohort?: string;
  stratum?: string;
  cursor?: string;
};

export const useNewsReviewWithToken = (token: string, query: NewsReviewQuery) =>
  useQuery({
    enabled: Boolean(token),
    queryKey: queryKeys.newsReview(JSON.stringify(query)),
    queryFn: async () =>
      (
        await getApi<NewsReview>("/api/news/review", {
          etagKey: `news-review:${JSON.stringify(query)}`,
          params: query,
          token,
        })
      ).data,
    refetchInterval: NEWS_REVIEW_REFETCH_MS,
    staleTime: 30_000,
  });

export const useNewsReviewEvidenceWithToken = (token: string, task: NewsReviewTask | null) =>
  useQuery({
    enabled: Boolean(token && task),
    queryKey: ["news-review-evidence", task?.task_id, task?.task_version],
    queryFn: async () =>
      (
        await getApi<NewsReviewEvidence>(
          `/api/news/review/tasks/${encodeURIComponent(task?.task_id ?? "")}/evidence`,
          {
            headers: { "If-Match": `"${task?.task_version ?? ""}"` },
            token,
          },
        )
      ).data,
    staleTime: Number.POSITIVE_INFINITY,
  });

export const useSubmitNewsReview = (token: string) => {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({
      task,
      body,
    }: {
      task: NewsReviewTask;
      body: NewsReviewResponseSubmission;
    }) =>
      (
        await postApi<NewsReviewSubmit>(
          `/api/news/review/tasks/${encodeURIComponent(task.task_id)}/responses`,
          {
            body,
            headers: {
              "Idempotency-Key": crypto.randomUUID(),
              "If-Match": `"${task.task_version}"`,
            },
            token,
          },
        )
      ).data,
    onSuccess: (_receipt, { task }) => {
      void queryClient.invalidateQueries({ queryKey: ["news-review"] });
      void queryClient.invalidateQueries({
        queryKey: ["news-review-evidence", task.task_id, task.task_version],
      });
      if (task.event_id)
        void queryClient.invalidateQueries({ queryKey: queryKeys.newsEvent(task.event_id) });
    },
  });
};

export const useSubmitExternalMiss = (token: string) => {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (body: NewsExternalMissSubmission) =>
      (
        await postApi<NewsReviewSubmit>("/api/news/review/external-misses", {
          body,
          headers: { "Idempotency-Key": crypto.randomUUID() },
          token,
        })
      ).data,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["news-review"] }),
  });
};
