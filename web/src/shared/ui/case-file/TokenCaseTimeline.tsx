import type { TokenCaseViewModel } from "@shared/model/tokenCaseViewModel";

import { TokenCasePostEventCard } from "./TokenCasePostEventCard";
import styles from "./TokenCaseTimeline.module.css";

type TokenCaseTimelineProps = {
  timeline: TokenCaseViewModel["timeline"];
  onLoadMorePosts: () => void;
};

export function TokenCaseTimeline({ timeline, onLoadMorePosts }: TokenCaseTimelineProps) {
  return (
    <section className={styles.timeline} aria-labelledby="token-case-timeline">
      <header className={styles.header}>
        <div>
          <span>Mention stream</span>
          <h2 id="token-case-timeline">Mention Timeline</h2>
        </div>
      </header>
      <div className={styles.events}>
        {timeline.items.map((item) => (
          <TokenCasePostEventCard key={item.id} item={item} />
        ))}
      </div>
      {timeline.emptyLabel ? <p className={styles.empty}>{timeline.emptyLabel}</p> : null}
      {timeline.hasMore ? (
        <button
          className={styles.loadMore}
          type="button"
          disabled={timeline.isLoading || timeline.isFetchingNextPage}
          onClick={onLoadMorePosts}
        >
          {timeline.isFetchingNextPage ? "Loading" : "Load more"}
        </button>
      ) : null}
    </section>
  );
}
