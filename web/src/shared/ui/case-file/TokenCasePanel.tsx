import type { TokenCaseViewModel, TokenCaseWindow } from "@shared/model/tokenCaseViewModel";

import { TokenCaseDataGapsRail } from "./TokenCaseDataGapsRail";
import { TokenCaseHero } from "./TokenCaseHero";
import styles from "./TokenCasePanel.module.css";
import { TokenCaseTimeline } from "./TokenCaseTimeline";

export type TokenCasePanelProps = {
  vm: TokenCaseViewModel;
  onWindowChange: (window: TokenCaseWindow) => void;
  onLoadMorePosts: () => void;
};

export function TokenCasePanel({ vm, onWindowChange, onLoadMorePosts }: TokenCasePanelProps) {
  return (
    <section aria-label="Token case" className={styles.panel} data-page-archetype="case">
      <TokenCaseHero
        hero={vm.hero}
        market={vm.market}
        metrics={vm.metrics}
        route={vm.route}
        target={vm.target}
        onWindowChange={onWindowChange}
      />
      <div className={styles.workspace}>
        <div className={styles.mainColumn}>
          <TokenCaseTimeline timeline={vm.timeline} onLoadMorePosts={onLoadMorePosts} />
        </div>
        <div className={styles.sideRail} aria-label="Token case side rail">
          <TokenCaseDataGapsRail dataGaps={vm.dataGaps} />
        </div>
      </div>
    </section>
  );
}
