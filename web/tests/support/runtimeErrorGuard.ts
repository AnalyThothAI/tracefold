type ConsoleError = typeof console.error;

export interface RuntimeErrorAllowance {
  match: string | RegExp;
  reason: string;
}

let active = false;
let consoleErrors: string[] = [];
let consoleErrorAllowances: RuntimeErrorAllowance[] = [];
let unhandledRejections: string[] = [];
let unhandledRejectionAllowances: RuntimeErrorAllowance[] = [];
let originalConsoleError: ConsoleError | undefined;

const captureUnhandledRejection = (reason: unknown) => {
  if (active) unhandledRejections.push(formatValue(reason));
};

function formatValue(value: unknown): string {
  if (typeof value === "string") return value;
  if (value instanceof Error) return value.stack ?? `${value.name}: ${value.message}`;
  try {
    return JSON.stringify(value) ?? String(value);
  } catch {
    return String(value);
  }
}

function validateAllowance(allowance: RuntimeErrorAllowance): void {
  if (!active)
    throw new Error("Runtime error allowlists are case-local and must be set inside a test");
  if (!allowance.reason.trim())
    throw new Error("Runtime error allowlists require a non-empty reason");
  if (typeof allowance.match === "string" && !allowance.match) {
    throw new Error("Runtime error allowlists require a non-empty match");
  }
}

function matchesAllowance(message: string, allowance: RuntimeErrorAllowance): boolean {
  if (typeof allowance.match === "string") return message.includes(allowance.match);
  allowance.match.lastIndex = 0;
  return allowance.match.test(message);
}

export function allowConsoleError(allowance: RuntimeErrorAllowance): void {
  validateAllowance(allowance);
  consoleErrorAllowances.push(allowance);
}

export function allowUnhandledRejection(allowance: RuntimeErrorAllowance): void {
  validateAllowance(allowance);
  unhandledRejectionAllowances.push(allowance);
}

export function installRuntimeErrorGuard(): void {
  if (originalConsoleError) return;
  originalConsoleError = console.error;
  console.error = (...values: unknown[]) => {
    if (active) consoleErrors.push(values.map(formatValue).join(" "));
    originalConsoleError?.apply(console, values);
  };
}

export function beginRuntimeErrorGuard(): void {
  active = true;
  consoleErrors = [];
  consoleErrorAllowances = [];
  unhandledRejections = [];
  unhandledRejectionAllowances = [];
  process.on("unhandledRejection", captureUnhandledRejection);
}

export async function finishRuntimeErrorGuard(): Promise<string[]> {
  // Node reports an unhandled rejection at the end of the current event-loop turn. Waiting for a native
  // immediate keeps the guard reliable even when a case has replaced the browser timer APIs.
  await new Promise<void>((resolve) => setImmediate(resolve));
  process.off("unhandledRejection", captureUnhandledRejection);
  const failures = consoleErrors
    .filter(
      (message) =>
        !consoleErrorAllowances.some((allowance) => matchesAllowance(message, allowance)),
    )
    .map((message) => `Unexpected console.error in test case:\n${message}`);
  failures.push(
    ...unhandledRejections
      .filter(
        (message) =>
          !unhandledRejectionAllowances.some((allowance) => matchesAllowance(message, allowance)),
      )
      .map((message) => `Unexpected unhandled rejection in test case:\n${message}`),
  );
  active = false;
  consoleErrors = [];
  consoleErrorAllowances = [];
  unhandledRejections = [];
  unhandledRejectionAllowances = [];
  return failures;
}

export function uninstallRuntimeErrorGuard(): void {
  if (originalConsoleError) console.error = originalConsoleError;
  originalConsoleError = undefined;
  process.off("unhandledRejection", captureUnhandledRejection);
  active = false;
  consoleErrors = [];
  consoleErrorAllowances = [];
  unhandledRejections = [];
  unhandledRejectionAllowances = [];
}
