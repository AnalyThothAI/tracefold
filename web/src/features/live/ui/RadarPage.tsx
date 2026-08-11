import type { ReactNode } from "react";

import "./live.css";

type RadarPageProps = {
  children: ReactNode;
};

export function RadarPage({ children }: RadarPageProps) {
  return (
    <div className="live-radar-page" data-page-archetype="scan" data-testid="radar-page">
      {children}
    </div>
  );
}
