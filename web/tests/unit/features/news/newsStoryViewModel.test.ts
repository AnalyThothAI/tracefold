import {
  analysisLabel,
  evidencePostureLabel,
  lifecycleLabel,
} from "@features/news/model/newsStoryViewModel";
import { describe, expect, it } from "vitest";

describe("News Story view model", () => {
  it("keeps evidence, lifecycle, and publication states separate", () => {
    expect(evidencePostureLabel("independently_corroborated")).toBe("独立多源佐证");
    expect(evidencePostureLabel("primary_source_confirmed")).toBe("一手材料确认");
    expect(evidencePostureLabel("contested")).toBe("证据有争议");
    expect(lifecycleLabel("developing")).toBe("发展中");
    expect(analysisLabel("unavailable")).toBe("尚无 AI 分析");
    expect(analysisLabel("insufficient")).toBe("证据不足");
  });
});
