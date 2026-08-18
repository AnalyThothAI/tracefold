export function searchWithOptionalPrefix(params: URLSearchParams): string {
  const search = params.toString();
  return search ? `?${search}` : "";
}
