import type { components } from "@lib/types/openapi";

type Schemas = components["schemas"];
type MacroIndicatorData = Schemas["MacroIndicatorData"];
type MacroEconomySectionData = Schemas["MacroEconomySectionData"];
type MacroPositionData = Schemas["MacroPositionData"];
type MacroCrossAssetReturnMatrixRowData = Schemas["MacroCrossAssetReturnMatrixRowData"];
type MacroCrossAssetNormalizedGroupData = Schemas["MacroCrossAssetNormalizedGroupData"];
type MacroCrossAssetSourceIdentityData = Schemas["MacroCrossAssetSourceIdentityData"];
type MacroCrossAssetSourceSelectionData = Schemas["MacroCrossAssetSourceSelectionData"];
type MacroCurveYieldSnapshotData = Schemas["MacroCurveYieldSnapshotData"];
type MacroCurveBreakevenSnapshotData = Schemas["MacroCurveBreakevenSnapshotData"];
type MacroCurveSpreadPointData = Schemas["MacroCurveSpreadPointData"];
type MacroHistoryPointData = Schemas["MacroHistoryPointData"];
type MacroCorrelationData = Schemas["MacroCorrelationData"];

export type MacroHistoryPoint = {
  date: string;
  value: number;
};

export type MacroIndicatorView = {
  datasetId: string;
  label: string;
  unit: string;
  latestValue: number;
  asOf: string;
  change1w: number | null;
  change1m: number | null;
  sampleCount: number;
  percentile: number | null;
  historyStart: string;
  historyEnd: string;
  sourceUrl: string;
  history: MacroHistoryPoint[];
};

export type MacroCurvePoint = {
  tenor: string;
  years: number;
  value: number;
};

export type MacroCurveSnapshotView = {
  window: "current" | "previous" | "1w" | "mtd" | "3m";
  asOf: string;
  points: MacroCurvePoint[];
};

export type MacroReleaseView = {
  datasetId: string;
  label: string;
  referencePeriod: string;
  scheduledAtMs: number | null;
  publishedAtMs: number | null;
  receivedAtMs: number;
  actualValue: number | null;
  estimateValue: number | null;
  priorValue: number | null;
  revisedPriorValue: number | null;
  surprise: number | null;
  revision: number | null;
  seasonalAdjustment: MacroEconomySectionData["official_releases"][number]["observations"][number]["seasonal_adjustment"];
  unit: string;
  sourceUrl: string;
};

export type MacroPositionView = {
  contractCode: string;
  contractName: string;
  reportDate: string;
  leveragedNetPctOi: number;
  assetManagerNetPctOi: number;
  dealerNetPctOi: number;
  sourceUrl: string;
};

export type MacroAssetFactView = {
  datasetId: string;
  label: string;
  sourceRole: MacroCrossAssetSourceSelectionData["source_role"];
  unit: string;
  latestValue: number;
  asOf: string;
  marketTimeMs: number | null;
  change1dPct: number | null;
  change1wPct: number | null;
  change1mPct: number | null;
  sourceUrl: string;
};

export type MacroCrossAssetSourceView = {
  datasetId: string;
  label: string;
  sourceRole: MacroCrossAssetSourceSelectionData["source_role"];
  fact: MacroAssetFactView | null;
};

export type MacroCrossAssetReturnRowView = {
  displayOrder: number;
  groupId: string;
  groupLabel: string;
  symbol: string;
  label: string;
  identityPolicy: MacroCrossAssetReturnMatrixRowData["identity_policy"];
  selectionPolicy: MacroCrossAssetReturnMatrixRowData["selection_policy"];
  latestSource: MacroCrossAssetSourceView;
  returnSource: MacroCrossAssetSourceView;
};

export type MacroCrossAssetNormalizedSeriesView = {
  displayOrder: number;
  symbol: string;
  label: string;
  source: MacroCrossAssetSourceView;
  points: MacroHistoryPoint[];
};

export type MacroCrossAssetNormalizedGroupView = {
  displayOrder: number;
  groupId: string;
  label: string;
  series: MacroCrossAssetNormalizedSeriesView[];
};

export type MacroCrossAssetSourceIdentityView = {
  displayOrder: number;
  symbol: string;
  label: string;
  evidenceKind: string;
  identityPolicy: MacroCrossAssetSourceIdentityData["identity_policy"];
  selectionPolicy: MacroCrossAssetSourceIdentityData["selection_policy"];
  sources: MacroCrossAssetSourceView[];
};

export type MacroCorrelationView = {
  left: string;
  right: string;
  correlation: number | null;
  sampleCount: number;
  window: MacroCorrelationData["window"];
};

export function parseHistory(rows: readonly MacroHistoryPointData[]): MacroHistoryPoint[] {
  return rows
    .map((row) => ({ date: row.date, value: row.value }))
    .sort((left, right) => left.date.localeCompare(right.date));
}

export function parseSpreadPoints(rows: readonly MacroCurveSpreadPointData[]): MacroHistoryPoint[] {
  return rows
    .map((row) => ({ date: row.date, value: row.value_bp }))
    .sort((left, right) => left.date.localeCompare(right.date));
}

export function parseIndicators(rows: readonly MacroIndicatorData[]): MacroIndicatorView[] {
  return rows.map((row) => ({
    datasetId: row.dataset_id,
    label: row.label,
    unit: row.unit,
    latestValue: row.latest_value,
    asOf: row.as_of,
    change1w: row.change_1w,
    change1m: row.change_1m,
    sampleCount: row.sample_count,
    percentile: row.percentile ?? null,
    historyStart: row.history_start,
    historyEnd: row.history_end,
    sourceUrl: row.source_url,
    history: parseHistory(row.history),
  }));
}

export function parseYieldCurveSnapshots(
  rows: readonly MacroCurveYieldSnapshotData[],
): MacroCurveSnapshotView[] {
  return rows.map((snapshot) => ({
    window: snapshot.window,
    asOf: snapshot.as_of,
    points: snapshot.points
      .map((point) => ({ tenor: point.tenor, years: point.years, value: point.yield_pct }))
      .sort((left, right) => left.years - right.years),
  }));
}

export function parseBreakevenCurveSnapshots(
  rows: readonly MacroCurveBreakevenSnapshotData[],
): MacroCurveSnapshotView[] {
  return rows.map((snapshot) => ({
    window: snapshot.window,
    asOf: snapshot.as_of,
    points: snapshot.points
      .map((point) => ({ tenor: point.tenor, years: point.years, value: point.breakeven_pct }))
      .sort((left, right) => left.years - right.years),
  }));
}

export function parseReleases(section: MacroEconomySectionData): MacroReleaseView[] {
  return section.official_releases
    .flatMap((summary) =>
      summary.observations.map((observation) => ({
        datasetId: summary.dataset_id,
        label: summary.label,
        referencePeriod: observation.reference_period,
        scheduledAtMs: observation.scheduled_at_ms,
        publishedAtMs: observation.published_at_ms,
        receivedAtMs: observation.received_at_ms,
        actualValue: observation.actual_value,
        estimateValue: observation.estimate_value,
        priorValue: observation.prior_value,
        revisedPriorValue: observation.revised_prior_value,
        surprise: observation.surprise,
        revision: observation.revision,
        seasonalAdjustment: observation.seasonal_adjustment,
        unit: observation.unit,
        sourceUrl: observation.source_url,
      })),
    )
    .sort(
      (left, right) =>
        (right.publishedAtMs ?? right.scheduledAtMs ?? 0) -
          (left.publishedAtMs ?? left.scheduledAtMs ?? 0) ||
        right.referencePeriod.localeCompare(left.referencePeriod) ||
        left.datasetId.localeCompare(right.datasetId),
    )
    .slice(0, 12);
}

export function parsePositions(rows: readonly MacroPositionData[]): MacroPositionView[] {
  return rows.map((row) => ({
    contractCode: row.contract_code,
    contractName: row.contract_name,
    reportDate: row.report_date,
    leveragedNetPctOi: row.leveraged_net_pct_oi,
    assetManagerNetPctOi: row.asset_manager_net_pct_oi,
    dealerNetPctOi: row.dealer_net_pct_oi,
    sourceUrl: row.source_url,
  }));
}

export function parseCrossAssetReturnMatrix(
  rows: readonly MacroCrossAssetReturnMatrixRowData[],
): MacroCrossAssetReturnRowView[] {
  return rows
    .map((row) => ({
      displayOrder: row.display_order,
      groupId: row.group_id,
      groupLabel: row.group_label,
      symbol: row.symbol,
      label: row.label,
      identityPolicy: row.identity_policy,
      selectionPolicy: row.selection_policy,
      latestSource: parseCrossAssetSource(row.latest_source),
      returnSource: parseCrossAssetSource(row.return_source),
    }))
    .sort((left, right) => left.displayOrder - right.displayOrder);
}

export function parseCrossAssetNormalizedGroups(
  rows: readonly MacroCrossAssetNormalizedGroupData[],
): MacroCrossAssetNormalizedGroupView[] {
  return rows
    .map((group) => ({
      displayOrder: group.display_order,
      groupId: group.group_id,
      label: group.label,
      series: group.series
        .map((series) => ({
          displayOrder: series.display_order,
          symbol: series.symbol,
          label: series.label,
          source: parseCrossAssetSource(series.source),
          points: series.points
            .map((point) => ({ date: point.date, value: point.normalized_value }))
            .sort((left, right) => left.date.localeCompare(right.date)),
        }))
        .sort((left, right) => left.displayOrder - right.displayOrder),
    }))
    .sort((left, right) => left.displayOrder - right.displayOrder);
}

export function parseCrossAssetSourceIdentity(
  rows: readonly MacroCrossAssetSourceIdentityData[],
): MacroCrossAssetSourceIdentityView[] {
  return rows
    .map((row) => ({
      displayOrder: row.display_order,
      symbol: row.symbol,
      label: row.label,
      evidenceKind: row.evidence_kind,
      identityPolicy: row.identity_policy,
      selectionPolicy: row.selection_policy,
      sources: row.sources.map(parseCrossAssetSource),
    }))
    .sort((left, right) => left.displayOrder - right.displayOrder);
}

function parseCrossAssetSource(
  source: MacroCrossAssetSourceSelectionData,
): MacroCrossAssetSourceView {
  const identity = {
    datasetId: source.dataset_id,
    label: source.label,
    sourceRole: source.source_role,
  };
  if (!source.fact) return { ...identity, fact: null };
  const fact = source.fact;
  const marketFact = "market_time_ms" in fact ? fact : null;
  return {
    ...identity,
    fact: {
      ...identity,
      unit: fact.unit,
      latestValue: fact.latest_value,
      asOf: fact.as_of,
      marketTimeMs: marketFact?.market_time_ms ?? null,
      change1dPct: marketFact?.change_1d_pct ?? null,
      change1wPct: "change_1w_pct" in fact ? fact.change_1w_pct : fact.change_1w,
      change1mPct: "change_1m_pct" in fact ? fact.change_1m_pct : fact.change_1m,
      sourceUrl: fact.source_url,
    },
  };
}

export function parseCorrelations(rows: readonly MacroCorrelationData[]): MacroCorrelationView[] {
  return rows.map((row) => ({
    left: row.left,
    right: row.right,
    correlation: row.correlation,
    sampleCount: row.sample_count,
    window: row.window,
  }));
}
