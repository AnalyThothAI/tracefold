/**
 * What the route shell may read from News.
 *
 * The shell needs one number — how much arrived in the last 24 h, for the sidebar — and must not pull the
 * route components in with it: `index.ts` exports pages, and importing that barrel from shell chrome would
 * make every route's code eager. This entrypoint carries hooks and types only.
 */
export {
  NEWS_REVIEW_DEFAULT_HOURS,
  useNewsReviewWithToken,
  useNewsStatusWithToken,
} from "./api/newsQueries";
export type { NewsReview, NewsStatus } from "./api/newsQueries";
// #88: the topbar renders the review summary as one string, and the rule for that string belongs to News.
export { hitFigure } from "./model/newsPrice";
