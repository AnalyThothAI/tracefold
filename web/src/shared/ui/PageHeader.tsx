import type { ReactNode } from "react";

import "./PageHeader.css";

/** One title baseline across workspaces; optional context wraps without moving the page boundary. */
export function PageHeader({
  children,
  subtitle,
  title,
}: {
  children?: ReactNode;
  subtitle?: ReactNode;
  title: string;
}) {
  return (
    <header className="page-header">
      <div className="page-header-copy">
        <h1>{title}</h1>
        {subtitle ? <p>{subtitle}</p> : null}
      </div>
      {children ? <div className="page-header-aside">{children}</div> : null}
    </header>
  );
}
