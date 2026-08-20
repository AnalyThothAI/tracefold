import { Dialog } from "radix-ui";

import "./ShortcutsDialog.css";

export type Shortcut = { keys: string; what: string };

/**
 * The console's keyboard map. Radix owns the focus trap, the `Esc` handler and the aria wiring; this only
 * lays out the pairs. The list is the caller's, so the shell decides what the console actually binds.
 */
export function ShortcutsDialog({
  onOpenChange,
  open,
  shortcuts,
  title = "快捷键",
}: {
  onOpenChange: (open: boolean) => void;
  open: boolean;
  shortcuts: readonly Shortcut[];
  title?: string;
}) {
  return (
    <Dialog.Root onOpenChange={onOpenChange} open={open}>
      <Dialog.Portal>
        <Dialog.Overlay className="ui-shortcuts-overlay">
          <Dialog.Content className="ui-shortcuts-panel">
            <div className="ui-shortcuts-head">
              <Dialog.Title>{title}</Dialog.Title>
              <Dialog.Description asChild>
                <small>Esc 关闭</small>
              </Dialog.Description>
            </div>
            <dl className="ui-shortcuts-list">
              {shortcuts.map((shortcut) => (
                <div className="ui-shortcuts-row" key={shortcut.what}>
                  <dt>{shortcut.what}</dt>
                  <dd>
                    <kbd>{shortcut.keys}</kbd>
                  </dd>
                </div>
              ))}
            </dl>
          </Dialog.Content>
        </Dialog.Overlay>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
