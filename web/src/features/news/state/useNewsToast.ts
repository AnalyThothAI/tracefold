import { useCallback, useEffect, useRef, useState } from "react";

const TOAST_MS = 1_800;

/**
 * A one-line confirmation for actions that leave no trace on the page — copying a headline or a
 * `tracefold news label` command. Nothing here writes to the server; the toast says what landed on the
 * clipboard, and says so out loud for a screen reader via the polite live region that renders it.
 */
export function useNewsToast() {
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
      void navigator.clipboard
        ?.writeText(text)
        .then(() => notify(note))
        .catch(() => notify("复制失败，请手动选择文本"));
    },
    [notify],
  );

  return { copy, message, notify };
}
