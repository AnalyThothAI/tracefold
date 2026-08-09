import { newsBriefPath, newsPath, newsSourcesPath, newsStatusPath } from "@shared/routing/paths";
import { Link } from "react-router-dom";

export type NewsSection = "brief" | "feed" | "sources" | "status" | "story";

export function NewsSectionTabs({ active }: { active: NewsSection }) {
  return (
    <nav aria-label="新闻视图" className="news-view-tabs">
      <Link aria-current={active === "feed" ? "page" : undefined} to={newsPath()}>
        全球新闻
      </Link>
      <Link aria-current={active === "brief" ? "page" : undefined} to={newsBriefPath()}>
        公共全球简报
      </Link>
      <Link aria-current={active === "status" ? "page" : undefined} to={newsStatusPath()}>
        状态
      </Link>
      <Link aria-current={active === "sources" ? "page" : undefined} to={newsSourcesPath()}>
        来源
      </Link>
    </nav>
  );
}
