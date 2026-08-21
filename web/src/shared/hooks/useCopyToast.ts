import { useCallback, useEffect, useRef, useState } from "react";

/** Long enough to read one line, short enough that it never becomes part of the page. */
const TOAST_MS = 1_800;

/**
 * A one-line confirmation for actions that leave no trace on the page — copying a `tracefold news label`
 * command from a row, the detail page, the review queue or the command palette. Nothing here writes to the
 * server; the toast says what landed on the clipboard, and `Toast` says it out loud through a polite live
 * region.
 *
 * The shell owns the single instance and hands `copy` down through the route context, so a copy started
 * anywhere is confirmed in exactly one place.
 */
export function useCopyToast() {
  const [message, setMessage] = useState<string | null>(null);
  const timer = useRef<number | undefined>(undefined);

  useEffect(() => () => window.clearTimeout(timer.current), []);

  const notify = useCallback((text: string) => {
    window.clearTimeout(timer.current);
    setMessage(text);
    timer.current = window.setTimeout(() => setMessage(null), TOAST_MS);
  }, []);

  const copy = useCallback(
    (text: string, note: string) => {
      // The Clipboard API only exists in a secure context, and the console is also reached over plain HTTP on
      // the LAN. Optional chaining there would short-circuit the whole chain and leave no clipboard write, no
      // toast, and no error — so say so instead, since copying is the entire labelling affordance.
      if (!navigator.clipboard) {
        notify("此连接不支持自动复制，请手动选择文本");
        return;
      }
      void navigator.clipboard
        .writeText(text)
        .then(() => notify(note))
        .catch(() => notify("复制失败，请手动选择文本"));
    },
    [notify],
  );

  return { copy, message, notify };
}
