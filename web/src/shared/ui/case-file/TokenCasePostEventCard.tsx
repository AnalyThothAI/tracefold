import type { TokenCasePostEvent } from "@shared/model/tokenCaseViewModel";
import { forwardRef } from "react";

import styles from "./TokenCasePostEventCard.module.css";

type TokenCasePostEventCardProps = {
  item: TokenCasePostEvent;
  focused?: boolean;
};

export const TokenCasePostEventCard = forwardRef<HTMLElement, TokenCasePostEventCardProps>(
  function TokenCasePostEventCard({ item, focused = false }, ref) {
    const handle = item.handle ? `@${item.handle.replace(/^@+/, "")}` : "unknown";
    const detailText = item.sourceText ?? item.text;
    const showDetails = Boolean(item.sourceText) || item.quality.contributions.length > 0;

    return (
      <article
        aria-current={focused ? "true" : undefined}
        aria-label={focused ? "Trigger evidence" : undefined}
        className={styles.card}
        data-focused={focused || undefined}
        data-phase={item.phase ?? "unknown"}
        ref={ref}
        tabIndex={focused ? -1 : undefined}
      >
        <div className={styles.timeGutter}>
          <time>{item.timeLabel ?? "--"}</time>
          {item.phase ? <span>{item.phase}</span> : null}
        </div>
        <div className={styles.body}>
          <header className={styles.eventHeader}>
            <div>
              <b>{handle}</b>
              {item.role ? <span>{item.role}</span> : null}
            </div>
            <div className={styles.eventActions}>
              {item.market ? (
                <div className={styles.marketQuote} data-tone={item.market.tone}>
                  <span>{item.market.providerLabel}</span>
                  <b>{item.market.eventPriceLabel}</b>
                  {item.market.liveDeltaLabel ? <em>{item.market.liveDeltaLabel}</em> : null}
                </div>
              ) : null}
              {item.url ? (
                <a
                  href={item.url}
                  target="_blank"
                  rel="noreferrer"
                  aria-label={`Open X post by ${handle}`}
                >
                  X
                </a>
              ) : null}
            </div>
          </header>
          <div className={styles.pills}>
            {item.pills.map((pill) => (
              <span key={`${item.id}-${pill.label}`} data-tone={pill.tone}>
                {pill.label}
              </span>
            ))}
          </div>
          <p className={styles.text}>{item.text}</p>
          {showDetails ? (
            <details className={styles.details}>
              <summary>{item.detailsLabel ?? "原文"}</summary>
              {detailText ? <p>{detailText}</p> : null}
              {item.quality.contributions.length ? (
                <dl className={styles.contributions}>
                  {item.quality.contributions.map((contribution) => (
                    <div key={`${item.id}-${contribution.label}`}>
                      <dt>{contribution.label}</dt>
                      <dd>
                        <b>{contribution.value}</b>
                        <span>{contribution.reason}</span>
                      </dd>
                    </div>
                  ))}
                </dl>
              ) : null}
            </details>
          ) : null}
        </div>
      </article>
    );
  },
);
