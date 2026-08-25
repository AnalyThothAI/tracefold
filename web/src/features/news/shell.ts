/**
 * What the route shell may read from News.
 *
 * The shell needs a few numbers — 24 h intake for the sidebar, the review summary for the topbar — and must
 * not pull the route components in with it: `index.ts` exports pages, and importing that barrel from shell
 * chrome would make every route's code eager. This entrypoint carries hooks, pure helpers and types only.
 */
export { useNewsStatusWithToken } from "./api/newsQueries";
export type { NewsHealthLevel, NewsReview, NewsStatus } from "./api/newsQueries";
/*
 * The topbar health lamp is a frame control, but the words in it are News': which four stages exist, what
 * each is called, and what `warn` reads as in Chinese. The shell maps the server's `health` onto the frame's
 * structural prop with these, so neither the frame nor the route invents a second vocabulary (#207).
 */
export { HEALTH_ITEM_KEYS, healthItemEyebrow, healthLevelLabel } from "./model/newsLabels";
