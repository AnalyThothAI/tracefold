export type TargetRef = {
  target_type: "Asset" | "CexToken";
  target_id: string;
};

export function targetRefKey(ref: TargetRef): string {
  return `${ref.target_type}:${ref.target_id}`;
}
