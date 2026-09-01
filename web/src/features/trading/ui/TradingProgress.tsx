import type { ExecutionProgress } from "../model/executionProgress";

export function TradingProgress({ progress }: { progress: ExecutionProgress }) {
  return (
    <div className="trading-progress">
      <b className="trading-progress-label">{progress.label}</b>
      <ol aria-label="执行进度" className="trading-progress-steps">
        {progress.steps.map((step) => (
          <li data-tone={step.tone} key={step.label}>
            <span aria-hidden className="trading-progress-dot" />
            {step.label}
          </li>
        ))}
      </ol>
    </div>
  );
}
