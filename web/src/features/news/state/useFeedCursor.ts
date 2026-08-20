import { type RefObject, useCallback, useEffect, useRef, useState } from "react";

/**
 * Keyboard reading position in the feed.
 *
 * The cursor is an index, but the browser focus is the real thing: `J`/`K` move focus onto the row element so
 * the platform scrolls it into view, screen readers announce it, and `Enter` is the row's own activation
 * rather than a synthetic one. Rows are `tabindex="-1"`; the list itself is the tab stop.
 */
export function useFeedCursor({
  count,
  enabled,
  listRef,
  onActivate,
  onLabel,
}: {
  count: number;
  enabled: boolean;
  listRef: RefObject<HTMLElement | null>;
  onActivate: (index: number) => void;
  onLabel: (index: number) => void;
}) {
  const [cursor, setCursor] = useState(-1);
  const cursorRef = useRef(cursor);
  cursorRef.current = cursor;
  // The callbacks close over the caller's current event list, which changes on every poll; behind a ref the
  // key listener below is installed once.
  const handlers = useRef({ onActivate, onLabel });
  handlers.current = { onActivate, onLabel };

  // A shorter list (a filter change, a narrower window) must not leave the cursor past the end.
  useEffect(() => {
    if (cursorRef.current >= count) setCursor(count ? count - 1 : -1);
  }, [count]);

  const focusRow = useCallback(
    (index: number) => {
      setCursor(index);
      const rows = listRef.current?.querySelectorAll<HTMLElement>("[data-event-id]");
      rows?.[index]?.focus();
    },
    [listRef],
  );

  const move = useCallback(
    (delta: number) => {
      if (!count) return;
      const from = cursorRef.current;
      const next = from < 0 ? (delta > 0 ? 0 : count - 1) : from + delta;
      focusRow(Math.min(count - 1, Math.max(0, next)));
    },
    [count, focusRow],
  );

  useEffect(() => {
    if (!enabled) return undefined;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.metaKey || event.ctrlKey || event.altKey || isTyping(event.target)) return;
      const index = cursorRef.current;
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
          if (index >= 0) {
            event.preventDefault();
            handlers.current.onActivate(index);
          }
          return;
        case "x":
        case "X":
          if (index >= 0) {
            event.preventDefault();
            handlers.current.onLabel(index);
          }
          return;
        default:
          return;
      }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [enabled, move]);

  return { cursor, setCursor: focusRow };
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
