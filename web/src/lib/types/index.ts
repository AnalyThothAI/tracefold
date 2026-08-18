import type { components } from "./openapi";

export type { components, operations, paths } from "./openapi";

export type OpenApiBootstrapData = components["schemas"]["BootstrapData"];
export type OpenApiStatusData = components["schemas"]["StatusData"];
export type BootstrapData = OpenApiBootstrapData;

// frontend-contracts: the response envelope is frontend-owned; every payload type comes from
// the generated OpenAPI mirror.
export type { ApiResponse } from "./frontend-contracts";
