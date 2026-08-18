import type {
  TokenCaseMetric,
  TokenCaseMarketView,
  TokenCaseViewModel,
  TokenCaseWindow,
} from "@shared/model/tokenCaseViewModel";
import { RouteBackLink } from "@shared/ui/RouteBackLink";
import { ExternalLink, Search } from "lucide-react";

import styles from "./TokenCaseHero.module.css";

type TokenCaseHeroProps = {
  hero: TokenCaseViewModel["hero"];
  market: TokenCaseMarketView;
  metrics: TokenCaseMetric[];
  route: TokenCaseViewModel["route"];
  target: TokenCaseViewModel["target"];
  onWindowChange: (window: TokenCaseWindow) => void;
};

const WINDOWS: TokenCaseWindow[] = ["5m", "1h", "4h", "24h"];

export function TokenCaseHero({
  hero,
  market,
  metrics,
  route,
  target,
  onWindowChange,
}: TokenCaseHeroProps) {
  const mark = target.symbol?.slice(0, 2).toUpperCase() ?? "?";

  return (
    <header className={styles.hero}>
      <div className={styles.topBar}>
        <RouteBackLink to={route.searchHref} label="返回" ariaLabel="返回 Search" />
        <div className={styles.controls}>
          <div className={styles.segmented} role="group" aria-label="case window">
            {WINDOWS.map((window) => (
              <button
                key={window}
                type="button"
                aria-pressed={route.window === window}
                onClick={() => onWindowChange(window)}
              >
                {window}
              </button>
            ))}
          </div>
        </div>
      </div>

      <div className={styles.mainGrid}>
        <div className={styles.identity}>
          <div className={styles.mark} aria-hidden="true">
            {hero.logoUrl ? <img src={hero.logoUrl} alt="" /> : <span>{mark}</span>}
          </div>
          <div className={styles.titleBlock}>
            <span className={styles.kicker}>token case file</span>
            <h1>{hero.title}</h1>
            <p>{hero.subtitle}</p>
            <div className={styles.metaRow}>
              {hero.contractLabel ? <code>{hero.contractLabel}</code> : null}
              <span>{target.shortId}</span>
            </div>
            <nav className={styles.actions} aria-label="Token links">
              {hero.actions.map((action) => (
                <a
                  key={`${action.label}-${action.href}`}
                  href={action.href}
                  data-tone={action.tone}
                  target="_blank"
                  rel="noreferrer"
                >
                  <ExternalLink aria-hidden />
                  <span>{action.label}</span>
                </a>
              ))}
              <a href={route.searchHref} data-tone="info">
                <Search aria-hidden />
                <span>Search</span>
              </a>
            </nav>
          </div>
        </div>

        <section className={styles.market} aria-labelledby="token-case-hero-market">
          <div className={styles.marketHeader}>
            <span className={styles.kicker}>market tape</span>
            <h2 id="token-case-hero-market">Live Market</h2>
          </div>
          <div className={styles.priceLine} data-tone={market.tone}>
            <b>{market.priceLabel}</b>
            <span>{market.provider ?? market.status}</span>
          </div>
          <dl className={styles.marketFacts}>
            <div>
              <dt>mcap</dt>
              <dd>{market.marketCapLabel}</dd>
            </div>
            <div>
              <dt>liq</dt>
              <dd>{market.liquidityLabel}</dd>
            </div>
            <div>
              <dt>vol 24h</dt>
              <dd>{market.volume24hLabel}</dd>
            </div>
            <div>
              <dt>OI</dt>
              <dd>{market.openInterestLabel}</dd>
            </div>
            <div>
              <dt>holders</dt>
              <dd>{market.holdersLabel}</dd>
            </div>
          </dl>
          <p>{market.observedAtLabel ?? market.emptyTitle ?? market.status}</p>
        </section>
      </div>

      <dl className={styles.metricGrid} aria-label="Token case metrics">
        {metrics.map((metric) => (
          <div key={metric.key} data-tone={metric.tone}>
            <dt>{metric.label}</dt>
            <dd>{metric.value}</dd>
            <dd className={styles.metricDetail}>{metric.detail}</dd>
          </div>
        ))}
      </dl>
    </header>
  );
}
