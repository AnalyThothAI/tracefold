/**
 * What the route shell may read from News.
 *
 * The shell needs a few numbers — 24 h intake for the sidebar and route-context facts for the topbar — and must
 * not pull the route components in with it: `index.ts` exports pages, and importing that barrel from shell
 * chrome would make every route's code eager. This entrypoint carries hooks, pure helpers and types only.
 */
export { useNewsStatusWithToken } from "./api/newsQueries";
export type { NewsHealthLevel, NewsReview, NewsStatus } from "./api/newsQueries";
/*
 * The topbar health lamp is a frame control, but the words in it are News': which stages exist, what each is
 * called, what `warn` reads as in Chinese, and the server instrument snapshot. The shell maps those facts onto the frame's
 * structural prop with these, so neither the frame nor the route invents a second vocabulary (#207).
 */
export {
  HEALTH_ITEM_KEYS,
  healthItemTitle,
  healthLevelLabel,
  optionalDuration,
} from "./model/newsLabels";
