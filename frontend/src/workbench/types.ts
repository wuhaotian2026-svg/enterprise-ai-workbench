export type WorkbenchModuleKey =
  | "knowledge"
  | "hr-assistant"
  | "my-requests"
  | "hr-review"
  | "knowledge-admin"
  | "organization"
  | "analytics"
  | "procurement"
  | "approval-center";

export type AllowedModule = {
  key: string;
  label: string;
  index: string;
};

export type ModuleListResponse = {
  modules: AllowedModule[];
  catalog_version: "2026-08-16" | "2026-08-23";
};

export type OrganizationUnit = {
  id: string;
  code: string;
  name: string;
  parent_id: string | null;
  is_active: boolean;
};

export type EmployeeAssignment = {
  id: string;
  user_id: string;
  employee_number: string;
  display_name: string;
  organization_unit_id: string | null;
  manager_employee_id: string | null;
  is_active: boolean;
};

export type CapabilityGrant = {
  id: string;
  user_id: string;
  capability: string;
  scope_kind: "global" | "unit_subtree";
  organization_unit_id: string | null;
  is_active: boolean;
};

export type OrganizationUnitCreate = {
  client_operation_id: string;
  code: string;
  name: string;
  parent_id: string | null;
};

export type OrganizationUnitUpdate = {
  client_operation_id: string;
  code?: string;
  name?: string;
  parent_id?: string | null;
  is_active?: boolean;
};

export type EmployeeAssignmentUpdate = {
  client_operation_id: string;
  organization_unit_id: string | null;
  manager_employee_id: string | null;
};

export type CapabilityGrantCreate = {
  client_operation_id: string;
  user_id: string;
  capability: string;
  scope_kind: "global" | "unit_subtree";
  organization_unit_id: string | null;
};

export type MetricValue = {
  numerator: number | null;
  denominator: number | null;
  value: number | null;
  available: boolean;
  sample_size: number;
  metric_version: "v1";
};

export type AnalyticsResponse = {
  from: string;
  to: string;
  organization_unit_id: string | null;
  metric_version: "v1";
  metrics: Record<string, MetricValue>;
};

export type AnalyticsDashboardData = {
  overview: AnalyticsResponse;
  knowledge: AnalyticsResponse;
  hrFunnel: AnalyticsResponse;
  tools: AnalyticsResponse;
  workflows: AnalyticsResponse;
};

export type AnalyticsRangeDays = 7 | 30 | 90;

export type AnalyticsQuery = {
  days: AnalyticsRangeDays;
  organizationUnitId: string | null;
  signal?: AbortSignal;
};
