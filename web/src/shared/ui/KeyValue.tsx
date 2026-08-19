import { cn } from "@lib/utils";
import type { ReactNode } from "react";

import "./KeyValue.css";

export function KeyValue({ children, className }: { children: ReactNode; className?: string }) {
  return <dl className={cn("ui-kv", className)}>{children}</dl>;
}

export function KeyValueRow({ k, v }: { k: string; v: ReactNode }) {
  return (
    <div className="ui-kv-row">
      <dt>{k}</dt>
      <dd>{v}</dd>
    </div>
  );
}
