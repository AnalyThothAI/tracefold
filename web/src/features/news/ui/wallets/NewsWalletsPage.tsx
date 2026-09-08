import { ActionButton } from "@shared/ui/ActionButton";
import { EmptyNote } from "@shared/ui/EmptyNote";
import { Metric, MetricRow } from "@shared/ui/Metric";
import { PageShell } from "@shared/ui/PageShell";
import * as PageState from "@shared/ui/PageState";
import type { FormEvent } from "react";
import { useSearchParams } from "react-router-dom";

import {
  NEWS_WALLET_CARD_WINDOWS,
  useNewsWalletCardsWithToken,
  useNewsWalletsWithToken,
  type NewsWalletCardFilters,
} from "../../api/newsQueries";
import { displayTime, optionalTime } from "../../model/newsLabels";
import { formatPrice } from "../../model/newsPrice";
import {
  nextWalletParams,
  parseWalletFilters,
  WALLET_CARD_FILTERS,
  walletCardLabel,
} from "../../model/walletFacts";
import { NewsPageHeader } from "../chrome/NewsChrome";

import { WalletResearchRow } from "./WalletResearchRow";
import { WalletRosterTable, WalletFillsTable } from "./WalletSupportingTables";
import "./newsWallets.css";

export function NewsWalletsPage({ token }: { token: string }) {
  const [params, setParams] = useSearchParams();
  const filters = parseWalletFilters(params);
  const tab = params.get("tab") === "roster" ? "roster" : "research";
  const walletsQuery = useNewsWalletsWithToken(token);
  const cardsQuery = useNewsWalletCardsWithToken(token, filters);
  const data = cardsQuery.data;
  const roster = walletsQuery.data?.roster;
  const setFilters = (next: NewsWalletCardFilters) => setParams(nextWalletParams(next));
  const changeFilters = (next: NewsWalletCardFilters) =>
    setFilters({ ...next, cursor: undefined, toMs: undefined });
  const applyAddresses = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    changeFilters({
      ...filters,
      walletAddress: String(form.get("wallet") ?? "")
        .trim()
        .toLowerCase(),
      tokenAddress: String(form.get("token") ?? "")
        .trim()
        .toLowerCase(),
      segmentKey: undefined,
    });
  };
  return (
    <PageShell archetype="scan" className="news-wallets-shell" label="钱包研究">
      <NewsPageHeader
        title="钱包研究"
        subtitle="谁买了什么、处于什么阶段，观察之后价格怎样变化。先读事实，再展开依据。"
      />
      <div className="news-wallets-windows news-research-tabs" aria-label="钱包视图" role="group">
        {(["research", "roster"] as const).map((value) => (
          <button
            type="button"
            key={value}
            className="news-wallets-window"
            aria-pressed={tab === value}
            data-active={tab === value || undefined}
            onClick={() => {
              const next = new URLSearchParams(params);
              next.delete("item");
              if (value === "roster") next.set("tab", value);
              else next.delete("tab");
              setParams(next);
            }}
          >
            {value === "research"
              ? "买入动向"
              : `跟踪钱包${roster ? ` · ${roster.members.length}` : ""}`}
          </button>
        ))}
      </div>
      {tab === "research" ? (
        <div className="news-wallets-body">
          <section className="news-wallets-panel" aria-label="买入研究">
            <div className="news-wallets-toolbar">
              <div className="news-wallets-windows" role="group" aria-label="按类型筛选">
                {WALLET_CARD_FILTERS.map((kind) => (
                  <button
                    type="button"
                    key={kind}
                    className="news-wallets-window"
                    aria-pressed={filters.kind === kind}
                    data-active={filters.kind === kind || undefined}
                    onClick={() => changeFilters({ ...filters, kind })}
                  >
                    {kind === "all" ? "全部观察" : walletCardLabel(kind)}
                  </button>
                ))}
              </div>
              <div className="news-wallets-windows" role="group" aria-label="按窗口筛选">
                {NEWS_WALLET_CARD_WINDOWS.map((window) => (
                  <button
                    type="button"
                    key={window}
                    className="news-wallets-window"
                    aria-pressed={filters.window === window}
                    data-active={filters.window === window || undefined}
                    onClick={() => changeFilters({ ...filters, window })}
                  >
                    {window}
                  </button>
                ))}
              </div>
            </div>
            <form
              className="news-wallets-filters"
              onSubmit={applyAddresses}
              key={filters.walletAddress + filters.tokenAddress}
            >
              <label>
                钱包地址
                <input
                  name="wallet"
                  placeholder="0x… 精确地址"
                  pattern="0x[0-9a-fA-F]{40}"
                  defaultValue={filters.walletAddress}
                />
              </label>
              <label>
                代币合约
                <input
                  name="token"
                  placeholder="0x… 精确地址"
                  pattern="0x[0-9a-fA-F]{40}"
                  defaultValue={filters.tokenAddress}
                />
              </label>
              <ActionButton type="submit" size="sm">
                筛选
              </ActionButton>
              {filters.walletAddress || filters.tokenAddress || filters.segmentKey ? (
                <ActionButton
                  size="sm"
                  onClick={() =>
                    setFilters({
                      window: filters.window,
                      kind: filters.kind,
                      walletAddress: "",
                      tokenAddress: "",
                    })
                  }
                >
                  清除筛选
                </ActionButton>
              ) : null}
            </form>
            {data ? (
              <p className="news-wallets-note">
                {displayTime(data.window_from_ms)} → {displayTime(data.window_to_ms)} ·
                包含未发送候选 ·{" "}
                {filters.view === "observations" ? "逐次观察" : "同段合并，金额取最新累计快照"}
              </p>
            ) : null}
          </section>
          {data ? (
            <MetricRow className="news-wallets-summary" columns={4} label="当前筛选完整范围">
              <Metric
                eyebrow="观察段"
                value={data.totals.segments}
                caption={`${data.totals.observations} 次观察 · 分页前统计`}
              />
              <Metric eyebrow="钱包" value={data.totals.wallets} caption="按链与地址去重" />
              <Metric eyebrow="代币" value={data.totals.tokens} caption="按链与合约去重" />
              <Metric
                eyebrow="买入段已计价金额"
                value={`$${formatPrice(data.totals.priced_buy_usd)}`}
                caption="各段最新累计值 · 非钱包总仓位"
              />
            </MetricRow>
          ) : null}
          {cardsQuery.isError && !data ? (
            <PageState.Error error={cardsQuery.error} onRetry={() => void cardsQuery.refetch()} />
          ) : !data ? (
            <PageState.Loading label="正在读取钱包观察" layout="panel" rows={5} />
          ) : (
            <PageState.Stale
              failedRefresh={cardsQuery.isError ? "观察刷新失败，保留上次读取的事实。" : undefined}
              onRetry={() => void cardsQuery.refetch()}
              updating={cardsQuery.isFetching}
            >
              <section className="news-wallets-panel" aria-label="钱包观察列表">
                {data.cards.length ? (
                  data.cards.map((card) => (
                    <WalletResearchRow
                      key={card.item_id}
                      card={card}
                      filters={filters}
                      open={params.get("item") === card.item_id}
                      onOpen={() => {
                        const next = new URLSearchParams(params);
                        if (params.get("item") === card.item_id) next.delete("item");
                        else {
                          next.set("item", card.item_id);
                          next.set("to_ms", String(data.window_to_ms));
                        }
                        setParams(next, { replace: true });
                      }}
                    />
                  ))
                ) : (
                  <EmptyNote>当前窗口与筛选下没有观察记录。</EmptyNote>
                )}
                <div className="news-wallets-toolbar">
                  <small>
                    本页 {data.cards.length} 条 · 全范围{" "}
                    {filters.view === "observations"
                      ? data.totals.observations
                      : data.totals.segments}{" "}
                    条
                  </small>
                  {filters.toMs ? (
                    <ActionButton size="sm" onClick={() => changeFilters(filters)}>
                      回到最新
                    </ActionButton>
                  ) : null}
                  {filters.cursor ? (
                    <ActionButton
                      size="sm"
                      onClick={() => setFilters({ ...filters, cursor: undefined })}
                    >
                      回到首屏
                    </ActionButton>
                  ) : null}
                  {data.next_cursor ? (
                    <ActionButton
                      size="sm"
                      onClick={() =>
                        setFilters({
                          ...filters,
                          cursor: data.next_cursor!,
                          toMs: data.window_to_ms,
                        })
                      }
                    >
                      下一页
                    </ActionButton>
                  ) : null}
                </div>
              </section>
              {filters.walletAddress && filters.tokenAddress ? (
                <section className="news-wallets-panel">
                  <WalletFillsTable fills={data.fills} limit={data.limit} />
                  {!data.fills_complete ? (
                    <p className="news-wallets-note">
                      流水超过本页上限，仅显示最新 {data.limit} 笔；不代表完整历史。
                    </p>
                  ) : null}
                </section>
              ) : null}
            </PageState.Stale>
          )}
        </div>
      ) : (
        <section className="news-wallets-panel">
          <div className="news-wallets-toolbar">
            <b>跟踪名单</b>
            <small>{optionalTime(roster?.taken_at_ms)} 取得</small>
          </div>
          <p className="news-wallets-note">
            来源 {roster?.provider ?? "未取得"} ·
            来源表现榜与持仓规模榜分别记录。统计周期以供应商原始口径为准，不代表可复制的盈利能力。
          </p>
          {walletsQuery.isError && !roster ? (
            <PageState.Error
              error={walletsQuery.error}
              onRetry={() => void walletsQuery.refetch()}
            />
          ) : !roster ? (
            <PageState.Loading layout="panel" label="正在读取跟踪钱包" rows={4} />
          ) : (
            <WalletRosterTable filters={filters} members={roster.members} />
          )}
        </section>
      )}
      <details className="news-wallets-panel news-wallet-data-details">
        <summary>采集与数据说明</summary>
        <p className="news-wallets-note">
          链上流水区分买入、卖出和转出；观察期首次买入不等于真实新仓。未核实的价格保留原始值与来源，不参与价格变化比较。
        </p>
        <p className="news-wallets-note">
          最近成功采集 {optionalTime(walletsQuery.data?.tape?.last_success_at_ms)} ·{" "}
          {walletsQuery.isError
            ? "采集状态读取失败"
            : walletsQuery.data?.tape?.last_outcome || "未取得采集状态"}
        </p>
      </details>
    </PageShell>
  );
}
