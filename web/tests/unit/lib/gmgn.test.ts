import { gmgnTokenUrl } from "@lib/gmgn";
import { describe, expect, it } from "vitest";

describe("gmgnTokenUrl", () => {
  it("maps only GMGN-supported canonical chains", () => {
    const evm = "0x514910771af9ca656af840dff83e8264ecf986ca";
    const sol = "So11111111111111111111111111111111111111112";

    expect(gmgnTokenUrl("solana", sol)).toBe(`https://gmgn.ai/sol/token/${sol}`);
    expect(gmgnTokenUrl("eip155:1", evm)).toBe(`https://gmgn.ai/eth/token/${evm}`);
    expect(gmgnTokenUrl("eip155:56", evm)).toBe(`https://gmgn.ai/bsc/token/${evm}`);
    expect(gmgnTokenUrl("eip155:8453", evm)).toBe(`https://gmgn.ai/base/token/${evm}`);
    expect(gmgnTokenUrl("robinhood", evm)).toBe(`https://gmgn.ai/robinhood/token/${evm}`);
    expect(gmgnTokenUrl("eip155:4663", evm)).toBeNull();
    expect(gmgnTokenUrl("constructor", evm)).toBeNull();
    expect(gmgnTokenUrl(null, evm)).toBeNull();
    expect(gmgnTokenUrl("eip155:1", null)).toBeNull();
  });
});
