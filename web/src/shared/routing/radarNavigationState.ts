export type RadarNavigationState = {
  radarScrollTop: number;
};

export function radarNavigationState(scrollTop: number): RadarNavigationState {
  return {
    radarScrollTop: Number.isFinite(scrollTop) && scrollTop > 0 ? scrollTop : 0,
  };
}

export function radarScrollTopFromState(value: unknown): number | null {
  if (!value || typeof value !== "object") return null;
  const scrollTop = (value as { radarScrollTop?: unknown }).radarScrollTop;
  return typeof scrollTop === "number" && Number.isFinite(scrollTop) && scrollTop >= 0
    ? scrollTop
    : null;
}
