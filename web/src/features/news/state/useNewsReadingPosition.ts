import { useQueryClient } from "@tanstack/react-query";
import { useLayoutEffect, useRef } from "react";

import {
  newsReadingSession,
  readNewsReadingState,
  writeNewsReadingState,
} from "./newsReadingSession";

function readingContainer(): HTMLElement {
  return document.querySelector<HTMLElement>(".center-column") ?? document.documentElement;
}

/** Restore once the retained list window is rendered, rather than clamping against its loading state. */
export function useNewsListPosition(identity: string, ready: boolean) {
  const session = newsReadingSession(useQueryClient());
  const active = useRef<{
    container: HTMLElement;
    identity: string;
    restored: boolean;
    target: number;
    current: number;
  } | null>(null);

  useLayoutEffect(() => {
    const container = readingContainer();
    const key = `scroll:${identity}`;
    const target = readNewsReadingState(session, key, 0);
    const position = { container, identity, restored: false, target, current: target };
    active.current = position;
    container.scrollTop = 0;
    const track = () => {
      if (position.restored) position.current = container.scrollTop;
    };
    container.addEventListener("scroll", track, { passive: true });
    return () => {
      container.removeEventListener("scroll", track);
      // DOM replacement can already have shortened the scroller. Keep the last observed list offset.
      if (position.restored) writeNewsReadingState(session, key, position.current);
      active.current = null;
    };
  }, [identity, session]);

  useLayoutEffect(() => {
    const position = active.current;
    if (!ready || !position || position.identity !== identity || position.restored) return;
    position.container.scrollTop = position.target;
    position.current = position.container.scrollTop;
    position.restored = true;
  }, [identity, ready]);
}

/** A different document starts at its top; query-only tab navigation keeps its reading position. */
export function useNewsDetailStart(identity: string, enabled = true) {
  useLayoutEffect(() => {
    if (enabled) readingContainer().scrollTop = 0;
  }, [enabled, identity]);
}
