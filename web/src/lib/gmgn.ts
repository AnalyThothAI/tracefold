export function gmgnTokenUrl(chain?: string | null, address?: string | null): string | null {
  const normalizedChain = chain?.trim().toLowerCase();
  const normalizedAddress = address?.trim();
  if (!normalizedChain || !normalizedAddress) {
    return null;
  }
  const chainSlug = GMGN_CHAIN_SLUGS.get(normalizedChain);
  if (!chainSlug) return null;
  return `https://gmgn.ai/${chainSlug}/token/${encodeURIComponent(normalizedAddress)}`;
}

const GMGN_CHAIN_SLUGS = new Map([
  ["eip155:1", "eth"],
  ["eip155:56", "bsc"],
  ["eip155:8453", "base"],
  ["robinhood", "robinhood"],
  ["solana", "sol"],
]);
