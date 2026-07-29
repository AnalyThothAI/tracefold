import type { JsonObject } from "./macroTypes";

export type MacroHistoryPoint = {
  date: string;
  value: number;
};

export type MacroIndicatorView = {
  datasetId: string;
  label: string;
  unit: string;
  latestValue: number | null;
  asOf: string | null;
  change1w: number | null;
  change1m: number | null;
  sampleCount: number | null;
  percentile: number | null;
  historyStart: string | null;
  historyEnd: string | null;
  sourceUrl: string | null;
  history: MacroHistoryPoint[];
};

export type MacroCurvePoint = {
  tenor: string;
  years: number;
  value: number;
};

export type MacroCurveSnapshotView = {
  window: "current" | "1w" | "1m" | "3m";
  asOf: string;
  points: MacroCurvePoint[];
};

export type MacroReleaseView = {
  datasetId: string;
  label: string;
  referencePeriod: string | null;
  scheduledAtMs: number | null;
  publishedAtMs: number | null;
  receivedAtMs: number | null;
  actualValue: number | null;
  estimateValue: number | null;
  priorValue: number | null;
  revisedPriorValue: number | null;
  surprise: number | null;
  revision: number | null;
  unit: string;
  sourceUrl: string | null;
};

export type MacroPositionView = {
  contractCode: string;
  contractName: string;
  reportDate: string | null;
  leveragedNetPctOi: number | null;
  assetManagerNetPctOi: number | null;
  dealerNetPctOi: number | null;
  sourceUrl: string | null;
};

export type MacroAssetFactView = {
  datasetId: string;
  label: string;
  symbol: string;
  sourceRole: string;
  latestValue: number | null;
  asOf: string | null;
  marketTimeMs: number | null;
  change1dPct: number | null;
  change1wPct: number | null;
  change1mPct: number | null;
  sourceUrl: string | null;
};

export type MacroCrossAssetSourceView = {
  datasetId: string;
  label: string;
  sourceRole: string;
  fact: MacroAssetFactView | null;
};

export type MacroCrossAssetReturnRowView = {
  displayOrder: number;
  groupId: string;
  groupLabel: string;
  symbol: string;
  label: string;
  identityPolicy: string;
  selectionPolicy: string;
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
  identityPolicy: string;
  selectionPolicy: string;
  sources: MacroCrossAssetSourceView[];
};

export type MacroCorrelationView = {
  left: string;
  right: string;
  correlation: number | null;
  sampleCount: number | null;
  window: string;
};

export function asRecord(value: unknown): JsonObject {
  return value != null && typeof value === "object" && !Array.isArray(value)
    ? (value as JsonObject)
    : {};
}

export function asRecords(value: unknown): JsonObject[] {
  return Array.isArray(value)
    ? value.filter(
        (item): item is JsonObject =>
          item != null && typeof item === "object" && !Array.isArray(item),
      )
    : [];
}

export function readText(record: JsonObject, key: string): string | null {
  const value = record[key];
  return typeof value === "string" && value.trim() ? value : null;
}

export function readNumber(record: JsonObject, key: string): number | null {
  const value = record[key];
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

export function readBoolean(record: JsonObject, key: string): boolean | null {
  const value = record[key];
  return typeof value === "boolean" ? value : null;
}

export function readRecord(record: JsonObject, key: string): JsonObject {
  return asRecord(record[key]);
}

export function readRecords(record: JsonObject, key: string): JsonObject[] {
  return asRecords(record[key]);
}

export function parseHistory(value: unknown): MacroHistoryPoint[] {
  return parsePointRecords(value, "value");
}

export function parsePointRecords(value: unknown, valueKey: string): MacroHistoryPoint[] {
  return asRecords(value)
    .map((row) => {
      const date = readText(row, "date");
      const pointValue = readNumber(row, valueKey);
      return date && pointValue != null ? { date, value: pointValue } : null;
    })
    .filter((point): point is MacroHistoryPoint => point != null)
    .sort((left, right) => left.date.localeCompare(right.date));
}

export function parseIndicators(value: unknown): MacroIndicatorView[] {
  const container = asRecord(value);
  const rows = Array.isArray(value) ? asRecords(value) : readRecords(container, "indicators");
  return rows.map((row, index) => ({
    datasetId: readText(row, "dataset_id") ?? `indicator-${index}`,
    label: readText(row, "label") ?? readText(row, "dataset_id") ?? `指标 ${index + 1}`,
    unit: readText(row, "unit") ?? "",
    latestValue: readNumber(row, "latest_value"),
    asOf: readText(row, "as_of"),
    change1w: readNumber(row, "change_1w"),
    change1m: readNumber(row, "change_1m"),
    sampleCount: readNumber(row, "sample_count"),
    percentile: readNumber(row, "percentile"),
    historyStart: readText(row, "history_start"),
    historyEnd: readText(row, "history_end"),
    sourceUrl: readText(row, "source_url"),
    history: parseHistory(row.history),
  }));
}

export function parseCurveSnapshots(
  curve: JsonObject,
  key: string,
  valueKey: "yield_pct" | "breakeven_pct",
): MacroCurveSnapshotView[] {
  return readRecords(curve, key)
    .map((snapshot) => {
      const window = readText(snapshot, "window");
      const asOf = readText(snapshot, "as_of");
      if (!asOf || !isCurveWindow(window)) return null;
      const points = readRecords(snapshot, "points")
        .map((point) => {
          const tenor = readText(point, "tenor");
          const years = readNumber(point, "years");
          const pointValue = readNumber(point, valueKey);
          return tenor && years != null && pointValue != null
            ? { tenor, years, value: pointValue }
            : null;
        })
        .filter((point): point is MacroCurvePoint => point != null)
        .sort((left, right) => left.years - right.years);
      return { window, asOf, points };
    })
    .filter((snapshot): snapshot is MacroCurveSnapshotView => snapshot != null);
}

export function parseReleases(value: unknown): MacroReleaseView[] {
  const container = asRecord(value);
  return readRecords(container, "official_releases")
    .flatMap((summary, summaryIndex) => {
      const datasetId = readText(summary, "dataset_id") ?? `release-${summaryIndex}`;
      const label =
        readText(summary, "label") ?? readText(summary, "dataset_id") ?? `发布 ${summaryIndex + 1}`;
      const observations = readRecords(summary, "observations");
      return observations.map((row) => ({
        datasetId,
        label,
        referencePeriod: readText(row, "reference_period"),
        scheduledAtMs: readNumber(row, "scheduled_at_ms"),
        publishedAtMs: readNumber(row, "published_at_ms"),
        receivedAtMs: readNumber(row, "received_at_ms"),
        actualValue: readNumber(row, "actual_value"),
        estimateValue: readNumber(row, "estimate_value"),
        priorValue: readNumber(row, "prior_value"),
        revisedPriorValue: readNumber(row, "revised_prior_value"),
        surprise: readNumber(row, "surprise"),
        revision: readNumber(row, "revision"),
        unit: readText(row, "unit") ?? "",
        sourceUrl: readText(row, "source_url"),
      }));
    })
    .sort(
      (left, right) =>
        (right.publishedAtMs ?? right.scheduledAtMs ?? 0) -
          (left.publishedAtMs ?? left.scheduledAtMs ?? 0) ||
        (right.referencePeriod ?? "").localeCompare(left.referencePeriod ?? "") ||
        left.datasetId.localeCompare(right.datasetId),
    )
    .slice(0, 12);
}

export function parsePositions(value: unknown): MacroPositionView[] {
  return asRecords(value).map((row, index) => ({
    contractCode: readText(row, "contract_code") ?? `contract-${index}`,
    contractName:
      readText(row, "contract_name") ?? readText(row, "contract_code") ?? `合约 ${index + 1}`,
    reportDate: readText(row, "report_date"),
    leveragedNetPctOi: readNumber(row, "leveraged_net_pct_oi"),
    assetManagerNetPctOi: readNumber(row, "asset_manager_net_pct_oi"),
    dealerNetPctOi: readNumber(row, "dealer_net_pct_oi"),
    sourceUrl: readText(row, "source_url"),
  }));
}

export function parseCrossAssetReturnMatrix(value: unknown): MacroCrossAssetReturnRowView[] {
  return asRecords(value)
    .map((row) => {
      const displayOrder = readNumber(row, "display_order");
      const groupId = readText(row, "group_id");
      const groupLabel = readText(row, "group_label");
      const symbol = readText(row, "symbol");
      const label = readText(row, "label");
      const identityPolicy = readText(row, "identity_policy");
      const selectionPolicy = readText(row, "selection_policy");
      const latestSource = parseCrossAssetSource(row.latest_source);
      const returnSource = parseCrossAssetSource(row.return_source);
      if (
        displayOrder == null ||
        !groupId ||
        !groupLabel ||
        !symbol ||
        !label ||
        !identityPolicy ||
        !selectionPolicy ||
        !latestSource ||
        !returnSource
      ) {
        return null;
      }
      return {
        displayOrder,
        groupId,
        groupLabel,
        symbol,
        label,
        identityPolicy,
        selectionPolicy,
        latestSource,
        returnSource,
      };
    })
    .filter((row): row is MacroCrossAssetReturnRowView => row != null)
    .sort((left, right) => left.displayOrder - right.displayOrder);
}

export function parseCrossAssetNormalizedGroups(
  value: unknown,
): MacroCrossAssetNormalizedGroupView[] {
  return asRecords(value)
    .map((row) => {
      const displayOrder = readNumber(row, "display_order");
      const groupId = readText(row, "group_id");
      const label = readText(row, "label");
      if (displayOrder == null || !groupId || !label) return null;
      const series = readRecords(row, "series")
        .map((seriesRow) => {
          const seriesOrder = readNumber(seriesRow, "display_order");
          const symbol = readText(seriesRow, "symbol");
          const seriesLabel = readText(seriesRow, "label");
          const source = parseCrossAssetSource(seriesRow.source);
          if (seriesOrder == null || !symbol || !seriesLabel || !source) return null;
          return {
            displayOrder: seriesOrder,
            symbol,
            label: seriesLabel,
            source,
            points: parsePointRecords(seriesRow.points, "normalized_value"),
          };
        })
        .filter((seriesRow): seriesRow is MacroCrossAssetNormalizedSeriesView => seriesRow != null)
        .sort((left, right) => left.displayOrder - right.displayOrder);
      return { displayOrder, groupId, label, series };
    })
    .filter((row): row is MacroCrossAssetNormalizedGroupView => row != null)
    .sort((left, right) => left.displayOrder - right.displayOrder);
}

export function parseCrossAssetSourceIdentity(value: unknown): MacroCrossAssetSourceIdentityView[] {
  return asRecords(value)
    .map((row) => {
      const displayOrder = readNumber(row, "display_order");
      const symbol = readText(row, "symbol");
      const label = readText(row, "label");
      const evidenceKind = readText(row, "evidence_kind");
      const identityPolicy = readText(row, "identity_policy");
      const selectionPolicy = readText(row, "selection_policy");
      if (
        displayOrder == null ||
        !symbol ||
        !label ||
        !evidenceKind ||
        !identityPolicy ||
        !selectionPolicy
      ) {
        return null;
      }
      return {
        displayOrder,
        symbol,
        label,
        evidenceKind,
        identityPolicy,
        selectionPolicy,
        sources: asRecords(row.sources)
          .map(parseCrossAssetSource)
          .filter((source): source is MacroCrossAssetSourceView => source != null),
      };
    })
    .filter((row): row is MacroCrossAssetSourceIdentityView => row != null)
    .sort((left, right) => left.displayOrder - right.displayOrder);
}

function parseCrossAssetSource(value: unknown): MacroCrossAssetSourceView | null {
  const row = asRecord(value);
  const datasetId = readText(row, "dataset_id");
  const label = readText(row, "label");
  const sourceRole = readText(row, "source_role");
  if (!datasetId || !label || !sourceRole) return null;
  return {
    datasetId,
    label,
    sourceRole,
    fact: parseCrossAssetFact(row.fact, { datasetId, label, sourceRole }),
  };
}

function parseCrossAssetFact(
  value: unknown,
  identity: Pick<MacroCrossAssetSourceView, "datasetId" | "label" | "sourceRole">,
): MacroAssetFactView | null {
  const row = asRecord(value);
  if (!Object.keys(row).length) return null;
  return {
    ...identity,
    symbol: "",
    latestValue: readNumber(row, "latest_value"),
    asOf: readText(row, "as_of"),
    marketTimeMs: readNumber(row, "market_time_ms"),
    change1dPct: readNumber(row, "change_1d_pct"),
    change1wPct: readNumber(row, "change_1w_pct"),
    change1mPct: readNumber(row, "change_1m_pct"),
    sourceUrl: readText(row, "source_url"),
  };
}

export function parseCorrelations(value: unknown): MacroCorrelationView[] {
  return asRecords(value).map((row, index) => ({
    left: readText(row, "left") ?? `L${index + 1}`,
    right: readText(row, "right") ?? `R${index + 1}`,
    correlation: readNumber(row, "correlation"),
    sampleCount: readNumber(row, "sample_count"),
    window: readText(row, "window") ?? "",
  }));
}

function isCurveWindow(value: string | null): value is MacroCurveSnapshotView["window"] {
  return value === "current" || value === "1w" || value === "1m" || value === "3m";
}
