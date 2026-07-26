import {
  analysisLabel,
  phaseLabel,
  verificationLabel,
} from "@features/news/model/newsStoryViewModel";
import { describe, expect, it } from "vitest";

describe("News Story view model", () => {
  it("uses evidence-aware Chinese status labels", () => {
    expect(verificationLabel("corroborated")).toBe("多源核验");
    expect(verificationLabel("attributed")).toBe("仅有归因");
    expect(phaseLabel("breaking")).toBe("突发");
    expect(analysisLabel("unavailable")).toBe("AI 未配置");
  });
});
