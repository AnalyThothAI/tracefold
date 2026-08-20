import { type RefObject, useCallback, useEffect, useRef, useState } from "react";

/**
 * Keyboard reading position in the feed.
 *
 * The cursor is an Event id, not an index: the feed re-polls every three seconds and prepends new Events
 * while the reader is at the top, so an index would silently start pointing at a different row than the one
 * highlighted and focused.
 *
 * The browser focus is the real thing — `J`/`K` move focus onto the row element so the platform scrolls it
 * into view and screen readers announce it. Rows are `tabindex="-1"`; the list itself is the tab stop.
 */
export function useFeedCursor({
  enabled,
  eventIds,
  listRef,
  onActivate,
  onLabel,
}: {
  enabled: boolean;
  eventIds: string[];
  listRef: RefObject<HTMLElement | null>;
  onActivate: (eventId: string) => void;
  onLabel: (eventId: string) => void;
}) {
  const [cursor, setCursor] = useState<string | null>(null);
  const cursorRef = useRef(cursor);
  cursorRef.current = cursor;
  const idsRef = useRef(eventIds);
  idsRef.current = eventIds;
  // The callbacks close over the caller's current event list, which changes on every poll; behind a ref the
  // key listener below is installed once.
  const handlers = useRef({ onActivate, onLabel });
  handlers.current = { onActivate, onLabel };

  // An Event that left the feed (a filter change, a narrower window) takes the cursor with it.
  useEffect(() => {
    if (cursorRef.current && !eventIds.includes(cursorRef.current)) setCursor(null);
  }, [eventIds]);

  const focusEvent = useCallback(
    (eventId: string) => {
      setCursor(eventId);
      listRef.current
        ?.querySelector<HTMLElement>(`[data-event-id="${CSS.escape(eventId)}"]`)
        ?.focus();
    },
    [listRef],
  );

  const move = useCallback(
    (delta: number) => {
      const ids = idsRef.current;
      if (!ids.length) return;
      const from = cursorRef.current ? ids.indexOf(cursorRef.current) : -1;
      const next = from < 0 ? (delta > 0 ? 0 : ids.length - 1) : from + delta;
      focusEvent(ids[Math.min(ids.length - 1, Math.max(0, next))]);
    },
    [focusEvent],
  );

  useEffect(() => {
    if (!enabled) return undefined;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.metaKey || event.ctrlKey || event.altKey || isTyping(event.target)) return;
      const cursorId = cursorRef.current;
      switch (event.key) {
        case "j":
        case "ArrowDown":
          event.preventDefault();
          move(1);
          return;
        case "k":
        case "ArrowUp":
          event.preventDefault();
          move(-1);
          return;
        case "Enter":
          // A focused control owns its own Enter — a task tab, a row action, 加载更多事件.
          if (cursorId && !isActivatable(event.target)) {
            event.preventDefault();
            handlers.current.onActivate(cursorId);
          }
          return;
        case "x":
        case "X":
          if (cursorId) {
            event.preventDefault();
            handlers.current.onLabel(cursorId);
          }
          return;
        default:
          return;
      }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [enabled, move]);

  return { cursor };
}

function isTyping(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  return (
    target.tagName === "INPUT" ||
    target.tagName === "TEXTAREA" ||
    target.tagName === "SELECT" ||
    target.isContentEditable
  );
}

/** Something the platform will activate on Enter by itself. The feed row is not one — it is `tabindex="-1"`. */
function isActivatable(target: EventTarget | null): boolean {
  if (!(target instanceof Element)) return false;
  const control = target.closest("a, button, summary, [role='tab'], [role='button']");
  return control != null;
}
