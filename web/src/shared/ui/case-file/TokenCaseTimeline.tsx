import type { TokenCaseViewModel } from "@shared/model/tokenCaseViewModel";
import { useEffect, useRef } from "react";

import { TokenCasePostEventCard } from "./TokenCasePostEventCard";
import styles from "./TokenCaseTimeline.module.css";

type TokenCaseTimelineProps = {
  timeline: TokenCaseViewModel["timeline"];
  onLoadMorePosts: () => void;
};

export function TokenCaseTimeline({ timeline, onLoadMorePosts }: TokenCaseTimelineProps) {
  const focusedElementRef = useRef<HTMLElement | null>(null);
  const scrolledEventRef = useRef<string | null>(null);

  useEffect(() => {
    if (
      timeline.focusStatus !== "found" ||
      !timeline.focusedEventId ||
      scrolledEventRef.current === timeline.focusedEventId
    ) {
      return;
    }
    scrolledEventRef.current = timeline.focusedEventId;
    focusedElementRef.current?.scrollIntoView?.({ block: "center" });
  }, [timeline.focusStatus, timeline.focusedEventId]);

  return (
    <section className={styles.timeline} aria-labelledby="token-case-timeline">
      <header className={styles.header}>
        <div>
          <span>Mention stream</span>
          <h2 id="token-case-timeline">Mention Timeline</h2>
        </div>
      </header>
      {timeline.focusStatus === "loading" ? (
        <p className={styles.focusStatus} role="status">
          Loading trigger evidence
        </p>
      ) : null}
      {timeline.focusStatus === "unavailable" ? (
        <p className={styles.focusStatus} role="status">
          Trigger evidence unavailable
        </p>
      ) : null}
      <div className={styles.events}>
        {timeline.items.map((item) => (
          <TokenCasePostEventCard
            focused={timeline.focusedEventId === item.id}
            item={item}
            key={item.id}
            ref={timeline.focusedEventId === item.id ? focusedElementRef : undefined}
          />
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
