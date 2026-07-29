import {
  parseCrossAssetReturnMatrix,
  parseCrossAssetSourceIdentity,
} from "@features/macro/model/macroViewModels";
import { describe, expect, it } from "vitest";

describe("macro asset view model contract", () => {
  it("keeps cross-asset identity on the source selection and reads canonical fact fields only", () => {
    const [row] = parseCrossAssetReturnMatrix([
      {
        display_order: 1,
        group_id: "server-group",
        group_label: "服务端分组",
        symbol: "SPY",
        label: "服务端资产标签",
        identity_policy: "separate_source_facts_no_blend",
        selection_policy: "intraday_latest_and_daily_returns_exact",
        latest_source: {
          dataset_id: "server.latest",
          label: "服务端最新价源",
          source_role: "intraday_proxy",
          fact: {
            dataset_id: "fact.must.not.override",
            label: "事实层旧标签",
            latest_value: 321,
            value: 999,
          },
        },
        return_source: {
          dataset_id: "server.returns",
          label: "服务端收益源",
          source_role: "decision_primary",
          fact: {
            value: 888,
          },
        },
      },
    ]);

    expect(row?.latestSource).toMatchObject({
      datasetId: "server.latest",
      label: "服务端最新价源",
      sourceRole: "intraday_proxy",
    });
    expect(row?.latestSource.fact).toMatchObject({
      datasetId: "server.latest",
      label: "服务端最新价源",
      latestValue: 321,
      sourceRole: "intraday_proxy",
    });
    expect(row?.returnSource.fact?.latestValue).toBeNull();
  });

  it("retains exact cross-asset source identity when its fact is null", () => {
    const [row] = parseCrossAssetSourceIdentity([
      {
        display_order: 1,
        symbol: "WTI",
        label: "WTI Cushing 现货",
        evidence_kind: "official_benchmark",
        identity_policy: "separate_source_facts_no_blend",
        selection_policy: "decision_primary_only_no_fallback",
        sources: [
          {
            dataset_id: "fred.dcoilwtico",
            label: "WTI 官方现货",
            source_role: "decision_primary",
            fact: null,
          },
        ],
      },
    ]);

    expect(row?.sources[0]).toEqual({
      datasetId: "fred.dcoilwtico",
      fact: null,
      label: "WTI 官方现货",
      sourceRole: "decision_primary",
    });
  });
});
