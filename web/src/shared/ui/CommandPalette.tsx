import { Search } from "lucide-react";
import { Dialog } from "radix-ui";
import { useEffect, useMemo, useRef, useState } from "react";

import "./CommandPalette.css";

export type Command = {
  /** A single monospace glyph standing for the kind — `⇥` jump, `⌗` filter, `✓` label, `⌘` navigate. */
  glyph: string;
  hint?: string;
  id: string;
  kind: string;
  label: string;
  run: () => void;
};

/**
 * One entry for jump, filter, label and navigate (design proposal ①).
 *
 * The console's fast path used to be "scroll the list until you find it": every one of these actions already
 * existed, spread across a search box, a filter disclosure, three destinations and a copy button. The palette
 * does not add a capability — it collapses four places into one keystroke, which is why the command list is
 * assembled by the shell from what the routes already expose rather than owned here.
 *
 * Matching is a plain substring over label, hint and kind. There is no ranking: the caller's order is the
 * order, so the same query always puts the same command under the same keystroke.
 */
export function CommandPalette({
  commands,
  onOpenChange,
  open,
}: {
  commands: Command[];
  onOpenChange: (open: boolean) => void;
  open: boolean;
}) {
  const [query, setQuery] = useState("");
  const [index, setIndex] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const results = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return commands;
    return commands.filter((command) =>
      `${command.label}${command.hint ?? ""}${command.kind}`.toLowerCase().includes(needle),
    );
  }, [commands, query]);

  // A fresh palette every time it opens: a stale query from ten minutes ago is never what the reader meant.
  useEffect(() => {
    if (open) {
      setQuery("");
      setIndex(0);
    }
  }, [open]);

  const run = (command: Command) => {
    onOpenChange(false);
    command.run();
  };

  return (
    <Dialog.Root onOpenChange={onOpenChange} open={open}>
      <Dialog.Portal>
        <Dialog.Overlay className="ui-palette-overlay">
          <Dialog.Content
            aria-describedby={undefined}
            className="ui-palette"
            /* A search dialog puts the caret in its box; `autoFocus` would do it before Radix has finished
               moving focus into the panel, and the linter is right that the attribute is the wrong tool. */
            onOpenAutoFocus={(event) => {
              event.preventDefault();
              inputRef.current?.focus();
            }}
            onKeyDown={(event) => {
              if (event.key === "ArrowDown") {
                event.preventDefault();
                setIndex((current) => Math.min(results.length - 1, current + 1));
              } else if (event.key === "ArrowUp") {
                event.preventDefault();
                setIndex((current) => Math.max(0, current - 1));
              } else if (event.key === "Enter") {
                event.preventDefault();
                const command = results[index];
                if (command) run(command);
              }
            }}
          >
            <Dialog.Title className="sr-only">命令面板</Dialog.Title>
            <div className="ui-palette-head">
              <Search aria-hidden />
              <input
                aria-label="命令面板"
                onChange={(event) => {
                  setQuery(event.target.value);
                  setIndex(0);
                }}
                placeholder="跳转、筛选或标注…"
                ref={inputRef}
                value={query}
              />
              <kbd>esc</kbd>
            </div>
            <div className="ui-palette-list" role="listbox" aria-label="可执行的命令">
              {results.map((command, position) => (
                <button
                  aria-selected={position === index}
                  className="ui-palette-option"
                  data-active={position === index || undefined}
                  key={command.id}
                  onClick={() => run(command)}
                  onMouseEnter={() => setIndex(position)}
                  role="option"
                  type="button"
                >
                  <span className="ui-palette-glyph">{command.glyph}</span>
                  <span className="ui-palette-copy">
                    <span className="ui-palette-label">{command.label}</span>
                    {command.hint ? <small>{command.hint}</small> : null}
                  </span>
                  <code className="ui-palette-kind">{command.kind}</code>
                </button>
              ))}
              {results.length ? null : <p className="ui-palette-empty">没有匹配的命令</p>}
            </div>
            <div className="ui-palette-foot">
              <span>↑↓ 选择</span>
              <span>↵ 执行</span>
              <span>⌘K 关闭</span>
            </div>
          </Dialog.Content>
        </Dialog.Overlay>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
