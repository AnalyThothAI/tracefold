export type TokenCaseWindow = "5m" | "1h" | "4h" | "24h";
export type TokenCaseTone = "neutral" | "health" | "info" | "warn" | "risk" | "opportunity";

export type TokenCaseMetric = {
  key: string;
  label: string;
  value: string;
  detail: string;
  tone: TokenCaseTone;
};

export type TokenCasePostEvent = {
  id: string;
  handle: string | null;
  text: string;
  sourceText?: string | null;
  detailsLabel?: string | null;
  url: string | null;
  timestampMs: number | null;
  timeLabel: string | null;
  phase: string | null;
  role: string | null;
  pills: Array<{ label: string; tone: TokenCaseTone }>;
  market: {
    eventPriceLabel: string;
    liveDeltaLabel: string | null;
    providerLabel: string;
    tone: TokenCaseTone;
  } | null;
  quality: {
    contributions: Array<{ label: string; value: string; reason: string }>;
  };
};

export type TokenCaseMarketView = {
  status: string;
  provider: string | null;
  priceLabel: string;
  marketCapLabel: string;
  liquidityLabel: string;
  holdersLabel: string;
  volume24hLabel: string;
  openInterestLabel: string;
  observedAtLabel: string | null;
  emptyTitle: string | null;
  emptyDetail: string | null;
  tone: TokenCaseTone;
};

export type TokenCaseViewModel = {
  target: {
    targetType: "Asset" | "CexToken" | string;
    targetId: string;
    symbol: string | null;
    name: string | null;
    chainId: string | null;
    address: string | null;
    displayTitle: string;
    shortId: string;
  };
  route: {
    window: TokenCaseWindow;
    searchHref: string;
  };
  hero: {
    logoUrl: string | null;
    title: string;
    subtitle: string;
    contractLabel: string | null;
    actions: Array<{ label: string; href: string; tone: TokenCaseTone }>;
  };
  metrics: TokenCaseMetric[];
  timeline: {
    items: TokenCasePostEvent[];
    focusedEventId: string | null;
    focusStatus: "found" | "loading" | "unavailable" | null;
    hasMore: boolean;
    isLoading: boolean;
    isFetchingNextPage: boolean;
    emptyLabel: string | null;
  };
  market: TokenCaseMarketView;
  dataGaps: string[];
};
