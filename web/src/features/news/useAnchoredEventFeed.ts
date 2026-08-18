import { type RefObject, useEffect, useRef, useState } from "react";

import type { NewsFeedEvent } from "./useNewsPage";

/**
 * Keeps the reader's place: while they are scrolled away from the top, new first-page Events are held back
 * (counted in `count`) instead of shifting the list; `reveal()` scrolls up and shows them.
 */
export function useAnchoredEventFeed(
  listRef: RefObject<HTMLDivElement | null>,
  serverEvents: NewsFeedEvent[],
  firstPageEvents: NewsFeedEvent[],
  identity: string,
) {
  const [count, setCount] = useState(0);
  const [, setRevision] = useState(0);
  const acceptedEventsRef = useRef<NewsFeedEvent[]>(serverEvents);
  const awayFromTopRef = useRef(false);
  const deferredTopIdsRef = useRef<Set<string>>(new Set());
  const deferredRef = useRef(false);
  const identityRef = useRef<string | null>(null);
  const knownIdsRef = useRef<Set<string>>(new Set());
  const latestEventsRef = useRef<NewsFeedEvent[]>(serverEvents);
  const scrollContainerRef = useRef<HTMLElement | null>(null);
  const firstPageKey = firstPageEvents.map((event) => event.event_id).join("\u001f");
  latestEventsRef.current = serverEvents;
  const identityChanged = identityRef.current !== identity;
  const addedTopIds = identityChanged
    ? []
    : firstPageEvents.flatMap((event) =>
        knownIdsRef.current.has(event.event_id) ? [] : [event.event_id],
      );
  const startsDeferral = addedTopIds.length > 0 && awayFromTopRef.current;
  const shouldDefer = deferredRef.current || startsDeferral;
  const excludedTopIds = startsDeferral
    ? new Set([...deferredTopIdsRef.current, ...addedTopIds])
    : deferredTopIdsRef.current;
  const events = shouldDefer
    ? appendNonDeferredTail(acceptedEventsRef.current, serverEvents, excludedTopIds)
    : serverEvents;

  useEffect(() => {
    const scrollContainer =
      listRef.current?.closest<HTMLElement>(".center-column") ?? document.documentElement;
    scrollContainerRef.current = scrollContainer;
    const handleScroll = () => {
      awayFromTopRef.current = scrollContainer.scrollTop > 96;
      if (!awayFromTopRef.current && deferredRef.current) {
        acceptedEventsRef.current = latestEventsRef.current;
        deferredTopIdsRef.current.clear();
        deferredRef.current = false;
        setCount(0);
        setRevision((current) => current + 1);
      }
    };
    handleScroll();
    scrollContainer.addEventListener("scroll", handleScroll, { passive: true });
    return () => scrollContainer.removeEventListener("scroll", handleScroll);
  }, [firstPageKey, listRef]);

  useEffect(() => {
    const currentIds = new Set(firstPageEvents.map((event) => event.event_id));
    if (identityRef.current !== identity) {
      identityRef.current = identity;
      knownIdsRef.current = currentIds;
      acceptedEventsRef.current = serverEvents;
      deferredTopIdsRef.current.clear();
      deferredRef.current = false;
      setCount(0);
      return;
    }
    const newlyAddedIds = firstPageEvents.flatMap((event) =>
      knownIdsRef.current.has(event.event_id) ? [] : [event.event_id],
    );
    knownIdsRef.current = currentIds;
    if (newlyAddedIds.length && awayFromTopRef.current) {
      let newlyDeferred = 0;
      for (const eventId of newlyAddedIds) {
        if (deferredTopIdsRef.current.has(eventId)) continue;
        deferredTopIdsRef.current.add(eventId);
        newlyDeferred += 1;
      }
      deferredRef.current = true;
      if (newlyDeferred) setCount((current) => current + newlyDeferred);
      return;
    }
    if (!deferredRef.current) acceptedEventsRef.current = serverEvents;
  }, [firstPageKey, firstPageEvents, identity, serverEvents]);

  const reveal = () => {
    const scrollContainer = scrollContainerRef.current;
    acceptedEventsRef.current = latestEventsRef.current;
    deferredTopIdsRef.current.clear();
    deferredRef.current = false;
    setRevision((current) => current + 1);
    if (scrollContainer && typeof scrollContainer.scrollTo === "function") {
      scrollContainer.scrollTo({ behavior: "smooth", top: 0 });
    } else if (scrollContainer) {
      scrollContainer.scrollTop = 0;
    }
    awayFromTopRef.current = false;
    setCount(0);
  };

  return { count, events, reveal };
}

function appendNonDeferredTail(
  acceptedEvents: NewsFeedEvent[],
  serverEvents: NewsFeedEvent[],
  deferredTopIds: Set<string>,
): NewsFeedEvent[] {
  const seen = new Set(acceptedEvents.map((event) => event.event_id));
  const appended = serverEvents.filter((event) => {
    if (deferredTopIds.has(event.event_id) || seen.has(event.event_id)) return false;
    seen.add(event.event_id);
    return true;
  });
  return appended.length ? [...acceptedEvents, ...appended] : acceptedEvents;
}
