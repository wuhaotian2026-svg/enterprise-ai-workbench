import { request } from "../api/request";
import type {
  AllowedModule,
  AnalyticsDashboardData,
  AnalyticsQuery,
  AnalyticsResponse,
  CapabilityGrant,
  CapabilityGrantCreate,
  EmployeeAssignment,
  EmployeeAssignmentUpdate,
  MetricValue,
  ModuleListResponse,
  OrganizationUnit,
  OrganizationUnitCreate,
  OrganizationUnitUpdate,
} from "./types";

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function parseModule(value: unknown): AllowedModule {
  if (
    !isRecord(value)
    || typeof value.key !== "string"
    || typeof value.label !== "string"
    || typeof value.index !== "string"
  ) {
    throw new Error("workbench_module_response_invalid");
  }
  return { key: value.key, label: value.label, index: value.index };
}

function parseModuleList(value: unknown): ModuleListResponse {
  if (
    !isRecord(value)
    || (value.catalog_version !== "2026-08-16"
      && value.catalog_version !== "2026-08-23")
    || !Array.isArray(value.modules)
  ) {
    throw new Error("workbench_module_response_invalid");
  }
  return {
    catalog_version: value.catalog_version,
    modules: value.modules.map(parseModule),
  };
}

export async function listWorkbenchModules(): Promise<ModuleListResponse> {
  const response = await request("/workbench/modules");
  return parseModuleList(await response.json() as unknown);
}

function stringField(value: Record<string, unknown>, field: string): string {
  const result = value[field];
  if (typeof result !== "string") throw new Error("organization_response_invalid");
  return result;
}

function nullableStringField(
  value: Record<string, unknown>,
  field: string,
): string | null {
  const result = value[field];
  if (result !== null && typeof result !== "string") {
    throw new Error("organization_response_invalid");
  }
  return result;
}

function booleanField(value: Record<string, unknown>, field: string): boolean {
  const result = value[field];
  if (typeof result !== "boolean") throw new Error("organization_response_invalid");
  return result;
}

function parseUnit(value: unknown): OrganizationUnit {
  if (!isRecord(value)) throw new Error("organization_response_invalid");
  return {
    id: stringField(value, "id"),
    code: stringField(value, "code"),
    name: stringField(value, "name"),
    parent_id: nullableStringField(value, "parent_id"),
    is_active: booleanField(value, "is_active"),
  };
}

function parseEmployee(value: unknown): EmployeeAssignment {
  if (!isRecord(value)) throw new Error("organization_response_invalid");
  return {
    id: stringField(value, "id"),
    user_id: stringField(value, "user_id"),
    employee_number: stringField(value, "employee_number"),
    display_name: stringField(value, "display_name"),
    organization_unit_id: nullableStringField(value, "organization_unit_id"),
    manager_employee_id: nullableStringField(value, "manager_employee_id"),
    is_active: booleanField(value, "is_active"),
  };
}

function parseGrant(value: unknown): CapabilityGrant {
  if (!isRecord(value)) throw new Error("organization_response_invalid");
  const scopeKind = stringField(value, "scope_kind");
  if (scopeKind !== "global" && scopeKind !== "unit_subtree") {
    throw new Error("organization_response_invalid");
  }
  return {
    id: stringField(value, "id"),
    user_id: stringField(value, "user_id"),
    capability: stringField(value, "capability"),
    scope_kind: scopeKind,
    organization_unit_id: nullableStringField(value, "organization_unit_id"),
    is_active: booleanField(value, "is_active"),
  };
}

function parseList<T>(value: unknown, parser: (item: unknown) => T): T[] {
  if (!Array.isArray(value)) throw new Error("organization_response_invalid");
  return value.map(parser);
}

async function jsonRequest<T>(
  path: string,
  parser: (value: unknown) => T,
  init?: RequestInit,
): Promise<T> {
  const response = await request(path, init);
  return parser(await response.json() as unknown);
}

function post(body: unknown): RequestInit {
  return { method: "POST", body: JSON.stringify(body) };
}

export function newWorkbenchOperationId(): string {
  return crypto.randomUUID();
}

export async function recordModuleOpened(moduleKey: string): Promise<void> {
  await request(
    "/analytics/ui-events",
    post({
      event_id: crypto.randomUUID(),
      event_name: "workbench_module_opened",
      dimensions: {
        entry_source: "navigation",
        module_key: moduleKey,
      },
    }),
  );
}

export function listOrganizationUnits(): Promise<OrganizationUnit[]> {
  return jsonRequest("/organization/units", (value) => parseList(value, parseUnit));
}

export function listOrganizationEmployees(): Promise<EmployeeAssignment[]> {
  return jsonRequest(
    "/organization/employees",
    (value) => parseList(value, parseEmployee),
  );
}

export function listCapabilityGrants(): Promise<CapabilityGrant[]> {
  return jsonRequest(
    "/organization/capability-grants",
    (value) => parseList(value, parseGrant),
  );
}

export function createOrganizationUnit(
  payload: OrganizationUnitCreate,
): Promise<OrganizationUnit> {
  return jsonRequest("/organization/units", parseUnit, post(payload));
}

export function updateOrganizationUnit(
  unitId: string,
  payload: OrganizationUnitUpdate,
): Promise<OrganizationUnit> {
  return jsonRequest(
    `/organization/units/${encodeURIComponent(unitId)}/update`,
    parseUnit,
    post(payload),
  );
}

export function updateEmployeeAssignment(
  employeeId: string,
  payload: EmployeeAssignmentUpdate,
): Promise<EmployeeAssignment> {
  return jsonRequest(
    `/organization/employees/${encodeURIComponent(employeeId)}/assignment`,
    parseEmployee,
    post(payload),
  );
}

export function createCapabilityGrant(
  payload: CapabilityGrantCreate,
): Promise<CapabilityGrant> {
  return jsonRequest("/organization/capability-grants", parseGrant, post(payload));
}

export function revokeCapabilityGrant(
  grantId: string,
  operationId: string,
): Promise<CapabilityGrant> {
  return jsonRequest(
    `/organization/capability-grants/${encodeURIComponent(grantId)}/revoke`,
    parseGrant,
    post({ client_operation_id: operationId }),
  );
}

function nullableNumberField(
  value: Record<string, unknown>,
  field: string,
): number | null {
  const result = value[field];
  if (result !== null && (typeof result !== "number" || !Number.isFinite(result))) {
    throw new Error("analytics_response_invalid");
  }
  return result;
}

function parseMetric(value: unknown): MetricValue {
  if (!isRecord(value)) throw new Error("analytics_response_invalid");
  const sampleSize = value.sample_size;
  if (
    typeof value.available !== "boolean"
    || typeof sampleSize !== "number"
    || !Number.isInteger(sampleSize)
    || sampleSize < 0
    || value.metric_version !== "v1"
  ) {
    throw new Error("analytics_response_invalid");
  }
  return {
    numerator: nullableNumberField(value, "numerator"),
    denominator: nullableNumberField(value, "denominator"),
    value: nullableNumberField(value, "value"),
    available: value.available,
    sample_size: sampleSize,
    metric_version: value.metric_version,
  };
}

function parseAnalyticsResponse(value: unknown): AnalyticsResponse {
  if (
    !isRecord(value)
    || typeof value.from !== "string"
    || typeof value.to !== "string"
    || (value.organization_unit_id !== null
      && typeof value.organization_unit_id !== "string")
    || value.metric_version !== "v1"
    || !isRecord(value.metrics)
  ) {
    throw new Error("analytics_response_invalid");
  }
  return {
    from: value.from,
    to: value.to,
    organization_unit_id: value.organization_unit_id,
    metric_version: value.metric_version,
    metrics: Object.fromEntries(
      Object.entries(value.metrics).map(([key, item]) => [key, parseMetric(item)]),
    ),
  };
}

function analyticsParams(query: AnalyticsQuery): string {
  const end = new Date();
  const start = new Date(end.getTime() - query.days * 24 * 60 * 60 * 1000);
  const params = new URLSearchParams({
    from: start.toISOString(),
    to: end.toISOString(),
  });
  if (query.organizationUnitId !== null) {
    params.set("organization_unit_id", query.organizationUnitId);
  }
  return params.toString();
}

async function getAnalytics(
  endpoint: "overview" | "knowledge" | "hr-funnel" | "tools" | "workflows",
  query: AnalyticsQuery,
): Promise<AnalyticsResponse> {
  const response = await request(
    `/analytics/${endpoint}?${analyticsParams(query)}`,
    { signal: query.signal },
  );
  return parseAnalyticsResponse(await response.json() as unknown);
}

export async function loadAnalyticsDashboard(
  query: AnalyticsQuery,
): Promise<AnalyticsDashboardData> {
  const [overview, knowledge, hrFunnel, tools, workflows] = await Promise.all([
    getAnalytics("overview", query),
    getAnalytics("knowledge", query),
    getAnalytics("hr-funnel", query),
    getAnalytics("tools", query),
    getAnalytics("workflows", query),
  ]);
  return { overview, knowledge, hrFunnel, tools, workflows };
}
