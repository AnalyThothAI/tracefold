import { ActionButton } from "@shared/ui/ActionButton";
import { FactGrid } from "@shared/ui/FactGrid";

import { useNewsWalletsWithToken, type NewsWallets } from "../../api/newsQueries";
import { optionalTime } from "../../model/newsLabels";
import { walletReason, walletStatusSentence, walletStatusState } from "../../model/walletFacts";

import { WalletRosterTable } from "./WalletSupportingTables";

/**
 * Whether an alert could have happened at all, above the list of the ones that did.
 *
 * A reader who opens this page to an empty table has to be able to tell four different things apart:
 * the quality pool cannot reach either quorum, it can but its addresses have not watched a whole
 * window yet, the chain cutoff is behind, or nothing qualified. Every number here is the server's —
 * the pool against the two quorums, the monitoring support behind it, the collection cutoff and the
 * episode → intent → sent funnel — and this block only chooses which answer leads.
 *
 * Its read is independent of the event list in both directions: a failure here says so and leaves the
 * events alone, and an event read that fails leaves this block reporting the collection as it found it.
 */
export function WalletStatusBlock({ token }: { token: string }) {
  const query = useNewsWalletsWithToken(token);
  const status = query.data;
  const state = walletStatusState(status, { failed: query.isError });
  return (
    <section
      className="news-wallets-panel news-wallet-status"
      aria-label="名单与采集状态"
      data-status-state={state}
    >
      <p className="news-wallet-status-verdict">{walletStatusSentence(state, status)}</p>
      {query.isError ? (
        <p className="news-wallets-note">
          名单 / 状态读取失败，事件仍可查阅。
          <ActionButton size="sm" onClick={() => void query.refetch()}>
            重试状态
          </ActionButton>
        </p>
      ) : null}
      {status ? <StatusFacts status={status} /> : null}
      <details className="news-wallet-data-details">
        <summary>名单成员 · {status?.roster.members.length ?? 0}</summary>
        <p className="news-wallets-note">
          来源 {status?.roster.provider ?? "未取得"} · {optionalTime(status?.roster.taken_at_ms)}{" "}
          取得。 来源表现榜才参与人数门槛；规模榜仅作观察背景。供应商短期统计不是长期策略胜率。
        </p>
        {status ? <WalletRosterTable members={status.roster.members} /> : null}
      </details>
    </section>
  );
}

function StatusFacts({ status }: { status: NewsWallets }) {
  const { roster, tape, thresholds, funnel } = status;
  const cutoff = optionalTime(tape?.scanned_at_ms);
  return (
    <>
      <FactGrid
        className="news-wallet-status-facts"
        label="名单与采集事实"
        facts={[
          { label: "链数据截止", value: cutoff },
          { label: "最近成功采集", value: optionalTime(tape?.last_success_at_ms) },
          {
            label: "质量地址 / 观察地址",
            value: `${roster.quality_count} / ${roster.whale_count}`,
          },
          {
            label: "完整窗口监控支持",
            value: `${roster.supported_quality_count} / ${roster.quality_count}`,
          },
          { label: "5m / 30m 门槛", value: `${thresholds.fast_n} / ${thresholds.slow_n}` },
          { label: "当前名单", value: thresholds.sufficient ? "足以触发" : "不足以触发" },
          {
            label: "最近完整名单",
            value: roster.version
              ? `v${roster.version} · ${optionalTime(roster.last_success_at_ms)}`
              : "尚未取得",
          },
          {
            label: "24 小时 事件 / 意图 / 已发送",
            value: `${funnel.events} / ${funnel.intents} / ${funnel.sent}`,
          },
        ]}
      />
      <p className="news-wallets-note">
        {status.collection_lagging
          ? `采集落后：链数据截止 ${cutoff}，当前变化可能尚未完整。`
          : `按已完成的链范围计算，链数据截止 ${cutoff}。`}
        {tape?.last_error ? ` 采集异常：${tape.last_error}` : ""}
      </p>
      <p className="news-wallets-note">
        {roster.last_error
          ? `名单刷新失败 ${optionalTime(roster.last_attempt_at_ms)}：${roster.last_error}；保留上一份完整名单。`
          : "名单刷新没有失败记录；只有完整成功的刷新才会发布新版本。"}
      </p>
      <p className="news-wallets-note">
        {funnel.unsent_reason
          ? `主要未发送原因：${walletReason(funnel.unsent_reason)} · ${funnel.unsent_reason_count} 轮`
          : "24 小时内没有未发送的事件。"}
      </p>
    </>
  );
}
