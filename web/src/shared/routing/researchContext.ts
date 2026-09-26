/** A reloadable return path; never navigate to an arbitrary URL supplied in a query. */
export function researchReturnPath(value: string | null | undefined): string | null {
  if (!value?.startsWith("/news/market")) return null;
  try {
    const url = new URL(value, "https://tracefold.invalid");
    if (url.origin !== "https://tracefold.invalid") return null;
    if (!/^\/news\/market(?:\/[^/]+)?$/.test(url.pathname)) return null;
    return url.pathname + url.search;
  } catch {
    return null;
  }
}

export function withResearchReturn(path: string, returnPath: string | null): string {
  const safePath = researchReturnPath(returnPath);
  if (!safePath) return path;
  const [pathname, search = ""] = path.split("?");
  const params = new URLSearchParams(search);
  params.set("research_from", safePath);
  return `${pathname}?${params}`;
}
