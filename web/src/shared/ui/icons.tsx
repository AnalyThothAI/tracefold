import type { LucideProps } from "lucide-react";
import { forwardRef, type ReactNode } from "react";

/**
 * The product's own nouns, drawn to lucide's specification (#207).
 *
 * Generic actions — Search, RefreshCw, PanelLeft, Chevron, Check, X, ExternalLink, SlidersHorizontal —
 * stay on lucide and are never redrawn here: they are already at this spec, and a second hand-drawn copy
 * only introduces drift. What lucide cannot supply is the vocabulary this console is read in. A newspaper
 * and a heartbeat say "news" and "activity"; the destinations are 事件流 and OI 来源与准入审计, and neither glyph
 * draws the word it stands for.
 *
 * Every icon here is on the same 24 grid with a 2px round-capped stroke and takes its colour from
 * `currentColor` alone, so at the 14px the sidebar and `IconButton` render them at, stroke density matches
 * the lucide glyphs beside them and no CSS has to change. Colour has exactly three states, all inherited:
 * `--text-subtle` at rest, `--accent-primary` when current, `--text-faint` when disabled. **Never red or
 * green** — those two hues belong to market direction (红 = 利多 / 绿 = 利空) and an icon that borrowed one
 * would state a market opinion the pipeline never formed. Only the favicon and the sidebar brand mark are
 * allowed a filled shape; everything below is open stroke, because a solid glyph at 14px outweighs the
 * heading beside it.
 *
 * Each icon is its own `forwardRef` with lucide's exact signature, so `appNavigation.ts` keeps
 * `icon: LucideIcon` unchanged and either family can fill that field.
 */
const TracefoldIcon = forwardRef<SVGSVGElement, LucideProps & { children: ReactNode }>(
  (
    { absoluteStrokeWidth, children, color = "currentColor", size = 24, strokeWidth = 2, ...props },
    ref,
  ) => (
    <svg
      fill="none"
      height={size}
      ref={ref}
      stroke={color}
      strokeLinecap="round"
      strokeLinejoin="round"
      // lucide's own contract: `absoluteStrokeWidth` keeps the drawn stroke at `strokeWidth` px whatever the
      // box is scaled to, instead of scaling with it.
      strokeWidth={absoluteStrokeWidth ? (Number(strokeWidth) * 24) / Number(size) : strokeWidth}
      viewBox="0 0 24 24"
      width={size}
      xmlns="http://www.w3.org/2000/svg"
      {...props}
    >
      {children}
    </svg>
  ),
);
TracefoldIcon.displayName = "TracefoldIcon";

/** 事件流. A time rail on the left and three shortening rows: the list's own shape. */
export const EventStreamIcon = forwardRef<SVGSVGElement, LucideProps>((props, ref) => (
  <TracefoldIcon ref={ref} {...props}>
    <path d="M4.5 4.5v15" />
    <path d="M9 8h10.5" />
    <path d="M9 12h8" />
    <path d="M9 16h5" />
  </TracefoldIcon>
));
EventStreamIcon.displayName = "EventStreamIcon";

/**
 * OI 来源与准入审计. A trace with one spike in it: the provider emits a frame when its own trigger fires, and this
 * page is where a reader checks that the trace itself parsed, passed the gates and occupied a push slot.
 *
 * A flat line would say "monitoring"; the spike is the frame. The glyph never leans up or down as a price
 * would — open interest rising is not price rising (#104).
 */
export const TelemetryPulseIcon = forwardRef<SVGSVGElement, LucideProps>((props, ref) => (
  <TracefoldIcon ref={ref} {...props}>
    <path d="M3 12h4l2.5-6 4 12L16 12h5" />
  </TracefoldIcon>
));
TelemetryPulseIcon.displayName = "TelemetryPulseIcon";

/**
 * 交易. Two arrows, one down and one up, side by side (#207 PR-W4).
 *
 * Direction-neutral by construction: the destination is the Signal lane itself, not a position, and a
 * navigation icon that leaned bullish would be a market call on a link. Like every icon here it is
 * `currentColor` only, so it takes the sidebar's three states and never red or green.
 */
export const TradeFlowIcon = forwardRef<SVGSVGElement, LucideProps>((props, ref) => (
  <TracefoldIcon ref={ref} {...props}>
    <path d="M8 3.5v13" />
    <path d="M4.5 13 8 16.5 11.5 13" />
    <path d="M16 20.5v-13" />
    <path d="M12.5 11 16 7.5 19.5 11" />
  </TracefoldIcon>
));
TradeFlowIcon.displayName = "TradeFlowIcon";

/**
 * 鲸鱼占比. One slice cut out of the whole. The wedge is filled because it *is* the share; it takes
 * `currentColor` like every stroke here, so it never carries a hue of its own.
 */
export const WhaleShareIcon = forwardRef<SVGSVGElement, LucideProps>((props, ref) => (
  <TracefoldIcon ref={ref} {...props}>
    <circle cx="12" cy="12" r="8.5" />
    <path d="M12 12V3.5A8.5 8.5 0 0 1 20.5 12z" fill="currentColor" stroke="none" />
  </TracefoldIcon>
));
WhaleShareIcon.displayName = "WhaleShareIcon";

/** 4h 窗口. The hand stops at four: the window is four hours and only the opening ranks inside it push. */
export const WindowClockIcon = forwardRef<SVGSVGElement, LucideProps>((props, ref) => (
  <TracefoldIcon ref={ref} {...props}>
    <circle cx="12" cy="12" r="8.5" />
    <path d="M12 7v5l4 2.5" />
  </TracefoldIcon>
));
WindowClockIcon.displayName = "WindowClockIcon";

/** 阈值. A line and a gate on it: what the operator changes is a number in `news.oi`, not code. */
export const ThresholdIcon = forwardRef<SVGSVGElement, LucideProps>((props, ref) => (
  <TracefoldIcon ref={ref} {...props}>
    <path d="M3.5 15.5h17" />
    <rect height="9" rx="1.3" width="4.4" x="12.8" y="11" />
  </TracefoldIcon>
));
ThresholdIcon.displayName = "ThresholdIcon";

/**
 * The brand mark: the stem of a T folded into a rising trace. The only filled shape in the console besides
 * the favicon, which is this same path on the same indigo tile — one product, one face.
 */
export function BrandMark({ className, size = 26 }: { className?: string; size?: number }) {
  return (
    <svg
      aria-hidden
      className={className}
      height={size}
      viewBox="0 0 24 24"
      width={size}
      xmlns="http://www.w3.org/2000/svg"
    >
      <rect fill="var(--accent-primary)" height="24" rx="5.5" width="24" />
      <path
        d="M5.5 7h13M12 7v8.5l6.5-6"
        fill="none"
        stroke="var(--surface-panel)"
        strokeLinecap="round"
        strokeLinejoin="round"
        strokeWidth="3"
      />
    </svg>
  );
}
