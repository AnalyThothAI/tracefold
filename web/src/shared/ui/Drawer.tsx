import { Dialog } from "radix-ui";
import type { ReactNode } from "react";

import "./Drawer.css";

/**
 * A side sheet. Radix owns the `Esc` handler, the dismiss layer and the aria wiring; this owns the frame and
 * the header row, so the tablet navigation drawer and the Event drawer are the same object with a different
 * side.
 *
 * `modal` is the important switch. Navigation is modal: it dims the page, traps focus and is the only thing
 * you can touch. The Event drawer is *not* — the whole point is that the list stays on screen and clicking
 * the next row swaps what the drawer shows, which a focus trap and a dismiss-on-outside-click would each
 * make impossible. A non-modal drawer draws no scrim, because a scrim over something still usable is a lie.
 *
 * `title` is required and may be replaced on screen by an `eyebrow`: a panel has to say what it is either way.
 */
export function Drawer({
  actions,
  children,
  eyebrow,
  flush = false,
  modal = true,
  onOpenChange,
  open,
  side = "right",
  title,
  width,
}: {
  actions?: ReactNode;
  children: ReactNode;
  eyebrow?: ReactNode;
  /** For a body that brings its own padding — a navigation list, a full-bleed table. */
  flush?: boolean;
  modal?: boolean;
  onOpenChange: (open: boolean) => void;
  open: boolean;
  side?: "left" | "right";
  title: string;
  width?: number;
}) {
  return (
    <Dialog.Root modal={modal} onOpenChange={onOpenChange} open={open}>
      <Dialog.Portal>
        {modal ? <Dialog.Overlay className="ui-drawer-overlay" /> : null}
        <Dialog.Content
          aria-describedby={undefined}
          className="ui-drawer"
          data-flush={flush || undefined}
          data-side={side}
          /*
           * A non-modal sheet keeps its dismiss layer out of the page's way. Radix would otherwise close it
           * the moment a pointer or focus lands outside, and clicking the next row — the way the reader moves
           * through the queue — is exactly that. It closes on `Esc` or on its own control instead.
           *
           * Opening still moves focus into the panel. Suppressing that was only ever there to protect the
           * `J`/`K` walk the console no longer has; without it a keyboard reader would have to tab through
           * the rest of the feed to reach a panel Radix portals to the end of `body`.
           */
          onFocusOutside={modal ? undefined : (event) => event.preventDefault()}
          onInteractOutside={modal ? undefined : (event) => event.preventDefault()}
          style={width ? { width: `min(${width}px, 100%)` } : undefined}
        >
          <header className="ui-drawer-head">
            {eyebrow ? <span className="ui-drawer-eyebrow">{eyebrow}</span> : null}
            <Dialog.Title className={eyebrow ? "sr-only" : "ui-drawer-title"}>{title}</Dialog.Title>
            {actions ? <span className="ui-drawer-actions">{actions}</span> : null}
          </header>
          <div className="ui-drawer-body">{children}</div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
