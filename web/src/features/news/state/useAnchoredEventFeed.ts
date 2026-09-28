import { type RefObject, useCallback, useEffect, useRef, useState } from "react";

import type { NewsFeedEvent } from "../api/newsQueries";

const FRESH_MS = 1_400;
const EMPTY_IDS: ReadonlySet<string> = new Set<string>();

/** Keep scroll position and deferred insertion order; entity content always comes from Query. */
export function useAnchoredEventFeed(
  listRef: RefObject<HTMLDivElement | null>,
  serverEvents: NewsFeedEvent[],
  firstPageEvents: NewsFeedEvent[],
  identity: string,
) {
  const [count, setCount] = useState(0);
  const [freshIds, setFreshIds] = useState<ReadonlySet<string>>(EMPTY_IDS);
  const freshTimer = useRef<number | undefined>(undefined);
  const [, setRevision] = useState(0);
  const acceptedIdsRef = useRef<string[]>(serverEvents.map((event) => event.event_id));
  const awayFromTopRef = useRef(false);
  const deferredTopIdsRef = useRef<Set<string>>(new Set());
  const identityRef = useRef<string | null>(null);
  const knownIdsRef = useRef<Set<string>>(new Set());
  const scrollContainerRef = useRef<HTMLElement | null>(null);
  const firstPageKey = firstPageEvents.map((event) => event.event_id).join("\u001f");
  const identityChanged = identityRef.current !== identity;
  const addedTopIds = identityChanged
    ? []
    : firstPageEvents.flatMap((event) =>
        knownIdsRef.current.has(event.event_id) ? [] : [event.event_id],
      );
  const startsDeferral = addedTopIds.length > 0 && awayFromTopRef.current;
  const excludedTopIds = startsDeferral
    ? new Set([...deferredTopIdsRef.current, ...addedTopIds])
    : deferredTopIdsRef.current;
  const byId = new Map(serverEvents.map((event) => [event.event_id, event]));
  const visibleIds =
    identityChanged || (!deferredTopIdsRef.current.size && !startsDeferral)
      ? serverEvents.map((event) => event.event_id)
      : appendNonDeferredTail(acceptedIdsRef.current, serverEvents, excludedTopIds, byId);
  const events = visibleIds.flatMap((id) => {
    const event = byId.get(id);
    return event ? [event] : [];
  });

  const markFresh = useCallback((ids: readonly string[]) => {
    if (!ids.length) return;
    window.clearTimeout(freshTimer.current);
    setFreshIds(new Set(ids));
    freshTimer.current = window.setTimeout(() => setFreshIds(EMPTY_IDS), FRESH_MS);
  }, []);

  useEffect(() => () => window.clearTimeout(freshTimer.current), []);

  useEffect(() => {
    const scrollContainer =
      listRef.current?.closest<HTMLElement>(".center-column") ?? document.documentElement;
    scrollContainerRef.current = scrollContainer;
    const handleScroll = () => {
      awayFromTopRef.current = scrollContainer.scrollTop > 96;
      if (!awayFromTopRef.current && deferredTopIdsRef.current.size) {
        acceptedIdsRef.current = serverEvents.map((event) => event.event_id);
        deferredTopIdsRef.current.clear();
        setCount(0);
        setRevision((current) => current + 1);
      }
    };
    handleScroll();
    scrollContainer.addEventListener("scroll", handleScroll, { passive: true });
    return () => scrollContainer.removeEventListener("scroll", handleScroll);
  }, [firstPageKey, listRef, serverEvents]);

  useEffect(() => {
    const currentIds = new Set(firstPageEvents.map((event) => event.event_id));
    if (identityRef.current !== identity) {
      identityRef.current = identity;
      knownIdsRef.current = currentIds;
      acceptedIdsRef.current = serverEvents.map((event) => event.event_id);
      deferredTopIdsRef.current.clear();
      setCount(0);
      return;
    }
    const newlyAddedIds = firstPageEvents.flatMap((event) =>
      knownIdsRef.current.has(event.event_id) ? [] : [event.event_id],
    );
    knownIdsRef.current = currentIds;
    if (newlyAddedIds.length && awayFromTopRef.current) {
      for (const eventId of newlyAddedIds) deferredTopIdsRef.current.add(eventId);
      setCount(deferredTopIdsRef.current.size);
      return;
    }
    if (!deferredTopIdsRef.current.size) {
      acceptedIdsRef.current = serverEvents.map((event) => event.event_id);
      markFresh(newlyAddedIds);
    }
  }, [firstPageKey, firstPageEvents, identity, markFresh, serverEvents]);

  const reveal = () => {
    markFresh([...deferredTopIdsRef.current]);
    acceptedIdsRef.current = serverEvents.map((event) => event.event_id);
    deferredTopIdsRef.current.clear();
    setRevision((current) => current + 1);
    const scrollContainer = scrollContainerRef.current;
    if (scrollContainer && typeof scrollContainer.scrollTo === "function") {
      scrollContainer.scrollTo({ behavior: "smooth", top: 0 });
    } else if (scrollContainer) {
      scrollContainer.scrollTop = 0;
    }
    awayFromTopRef.current = false;
    setCount(0);
  };

  return { count, events, freshIds, reveal };
}

function appendNonDeferredTail(
  acceptedIds: string[],
  serverEvents: NewsFeedEvent[],
  deferredTopIds: Set<string>,
  byId: Map<string, NewsFeedEvent>,
): string[] {
  const kept = acceptedIds.filter((id) => byId.has(id));
  const seen = new Set(kept);
  for (const event of serverEvents) {
    if (deferredTopIds.has(event.event_id) || seen.has(event.event_id)) continue;
    kept.push(event.event_id);
    seen.add(event.event_id);
  }
  return kept;
}
