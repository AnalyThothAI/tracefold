import { newsPath, newsStatusPath } from "@shared/routing/paths";
import { Link } from "react-router-dom";

export type NewsSection = "event" | "feed" | "status";

export function NewsSectionTabs({ active }: { active: NewsSection }) {
  return (
    <nav aria-label="新闻视图" className="news-view-tabs">
      <Link aria-current={active === "feed" ? "page" : undefined} to={newsPath()}>
        事件流
      </Link>
      <Link aria-current={active === "status" ? "page" : undefined} to={newsStatusPath()}>
        状态
      </Link>
    </nav>
  );
}
