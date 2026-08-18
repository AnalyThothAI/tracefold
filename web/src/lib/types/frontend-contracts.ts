export type ApiResponse<T> = {
  ok: boolean;
  data: T;
  error?: string | null;
};
