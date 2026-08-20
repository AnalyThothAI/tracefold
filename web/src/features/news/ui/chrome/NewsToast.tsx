import { Check } from "lucide-react";

import "./newsToast.css";

/**
 * The live region is always mounted so a screen reader hears the message when it appears; only the visible
 * capsule comes and goes.
 */
export function NewsToast({ message }: { message: string | null }) {
  return (
    <>
      <span aria-live="polite" className="sr-only" role="status">
        {message ?? ""}
      </span>
      {message ? (
        <span aria-hidden className="news-toast">
          <Check aria-hidden />
          {message}
        </span>
      ) : null}
    </>
  );
}
