import { Check } from "lucide-react";

import "./Toast.css";

/**
 * The live region is always mounted so a screen reader hears the message when it appears; only the visible
 * capsule comes and goes. Dark ground, white text: it lands over the console's own indigo status words and
 * has to be told apart from them at a glance.
 *
 * The message comes from `useCopyToast`, which the shell owns — there is one toast in the console.
 */
export function Toast({ message }: { message: string | null }) {
  return (
    <>
      <span aria-live="polite" className="sr-only" role="status">
        {message ?? ""}
      </span>
      {message ? (
        <span aria-hidden className="ui-toast">
          <Check aria-hidden />
          {message}
        </span>
      ) : null}
    </>
  );
}
