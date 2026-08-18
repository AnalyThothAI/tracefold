type AppEnv = {
  apiBaseUrl: string;
  mode: string;
};

function sameOrigin(): string {
  return window.location.origin;
}

export const env: AppEnv = {
  apiBaseUrl: import.meta.env.VITE_API_BASE_URL || sameOrigin(),
  mode: import.meta.env.MODE,
};
