import { CommandPalette, type Command } from "@shared/ui/CommandPalette";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

afterEach(() => cleanup());

/**
 * The palette adds no capability — every entry is something the console already offers somewhere else,
 * collapsed into one keystroke. So the contract here is only about *reaching* a command: the caller's order
 * is the order, matching is a plain substring, and running one closes the panel before it acts.
 */
function commands(run: (id: string) => void): Command[] {
  return [
    {
      glyph: "⌘",
      hint: "所有事件与它们的去向",
      id: "nav-feed",
      kind: "nav",
      label: "打开事件流",
      run: () => run("nav-feed"),
    },
    {
      glyph: "⌗",
      hint: "只看送达读者的事件",
      id: "filter-pushed",
      kind: "filter",
      label: "只看已推送",
      run: () => run("filter-pushed"),
    },
    {
      glyph: "✓",
      hint: "复制 CLI 命令，不写库",
      id: "label-missed",
      kind: "label",
      label: "把当前事件标为漏推",
      run: () => run("label-missed"),
    },
  ];
}

function renderPalette(onOpenChange = vi.fn()) {
  const ran: string[] = [];
  render(
    <CommandPalette commands={commands((id) => ran.push(id))} onOpenChange={onOpenChange} open />,
  );
  return { onOpenChange, ran };
}

describe("CommandPalette", () => {
  it("lists every command in the caller's order and puts the caret in the box", () => {
    renderPalette();

    expect(screen.getAllByRole("option").map((option) => option.textContent)).toEqual([
      "⌘打开事件流所有事件与它们的去向nav",
      "⌗只看已推送只看送达读者的事件filter",
      "✓把当前事件标为漏推复制 CLI 命令，不写库label",
    ]);
    expect(screen.getByRole("option", { selected: true })).toHaveTextContent("打开事件流");
  });

  it("filters on label, hint and kind together", () => {
    renderPalette();
    const input = screen.getByRole("textbox", { name: "命令面板" });

    fireEvent.change(input, { target: { value: "漏推" } });
    expect(screen.getAllByRole("option")).toHaveLength(1);

    // The kind is part of the haystack, so `filter` reaches the entries that filter.
    fireEvent.change(input, { target: { value: "filter" } });
    expect(screen.getByRole("option")).toHaveTextContent("只看已推送");

    fireEvent.change(input, { target: { value: "没有这个命令" } });
    expect(screen.queryAllByRole("option")).toHaveLength(0);
    expect(screen.getByText("没有匹配的命令")).toBeInTheDocument();
  });

  it("walks the list with the arrows and runs the selected command on Enter", () => {
    const { onOpenChange, ran } = renderPalette();
    const panel = screen.getByRole("dialog");

    fireEvent.keyDown(panel, { key: "ArrowDown" });
    fireEvent.keyDown(panel, { key: "ArrowDown" });
    expect(screen.getByRole("option", { selected: true })).toHaveTextContent("把当前事件标为漏推");

    // Past the end it stays on the last row rather than wrapping: a palette that loops runs the wrong thing.
    fireEvent.keyDown(panel, { key: "ArrowDown" });
    fireEvent.keyDown(panel, { key: "ArrowUp" });
    expect(screen.getByRole("option", { selected: true })).toHaveTextContent("只看已推送");

    fireEvent.keyDown(panel, { key: "Enter" });
    expect(ran).toEqual(["filter-pushed"]);
    // Closed before it acts, so a navigation does not leave the panel over the page it opened.
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it("runs a command on click", () => {
    const { ran } = renderPalette();

    fireEvent.click(screen.getByRole("option", { name: /把当前事件标为漏推/ }));

    expect(ran).toEqual(["label-missed"]);
  });
});
