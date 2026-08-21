import type { Shortcut } from "@shared/ui/ShortcutsDialog";

/**
 * What the console binds. The list is the contract the `?` panel shows and the shell and feed implement
 * between them: the shell owns the palette, navigation and search, the feed owns the reading cursor.
 */
export const APP_SHORTCUTS: readonly Shortcut[] = [
  { what: "命令面板", keys: "⌘K" },
  { what: "上一条 / 下一条", keys: "K / J" },
  { what: "打开选中事件", keys: "Enter" },
  { what: "就地展开判定", keys: "Space" },
  { what: "返回事件流", keys: "Esc" },
  { what: "切换结局筛选", keys: "1 – 4" },
  { what: "聚焦搜索", keys: "/" },
  { what: "事件流 / 复盘 / 状态", keys: "G 然后 F / R / S" },
  { what: "本面板", keys: "?" },
];
