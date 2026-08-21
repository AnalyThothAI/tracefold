/**
 * What the route shell may read from News.
 *
 * The shell needs a few numbers — 24 h intake for the sidebar, the review summary for the topbar — plus the
 * label command the ⌘K palette hands over, and must not pull the route components in with it: `index.ts`
 * exports pages, and importing that barrel from shell chrome would make every route's code eager. This
 * entrypoint carries hooks, pure helpers and types only.
 */
export {
  NEWS_REVIEW_DEFAULT_HOURS,
  useNewsReviewWithToken,
  useNewsStatusWithToken,
} from "./api/newsQueries";
export type { NewsReview, NewsStatus } from "./api/newsQueries";
// The learning plane is the CLI: the palette copies the same command the detail page and the feed row do.
export { labelCommand } from "./model/newsLabels";
// #88: the topbar renders the review summary as one string, and the rule for that string belongs to News.
export { hitFigure } from "./model/newsPrice";
