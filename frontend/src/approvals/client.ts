import { request } from "../api/request";
import type {
  ApprovalInstanceStatus,
  ApprovalProcessKey,
  ApprovalSubjectDetail,
  ApprovalSubjectItem,
  ApprovalSubjectStatus,
  ApprovalTaskDetail,
  ApprovalTaskPage,
  ApprovalTaskQuery,
  ApprovalTaskStatus,
  ApprovalTaskSummary,
  ApprovalTimelineEntry,
  ApprovalTransition,
  ProcurementApprovalSubjectSummary,
  UnsupportedApprovalSubject,
} from "./types";

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const DECIMAL_PATTERN = /^(0|[1-9][0-9]*)(?:\.([0-9]+))?$/;
const DATE_PATTERN = /^([0-9]{4})-([0-9]{2})-([0-9]{2})$/;
const DATETIME_PATTERN = /^([0-9]{4}-[0-9]{2}-[0-9]{2})T(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9](?:\.[0-9]+)?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])$/;

const TASK_STATUSES: readonly ApprovalTaskStatus[] = [
  "waiting", "pending", "approved", "rejected", "cancelled",
];
const INSTANCE_STATUSES: readonly ApprovalInstanceStatus[] = [
  "running", "approved", "rejected", "cancelled",
];
const SUBJECT_STATUSES: readonly ApprovalSubjectStatus[] = [
  "pending_manager", "pending_procurement", "approved", "rejected", "cancelled",
];
const CATEGORIES: readonly ApprovalSubjectItem["category"][] = [
  "office_supplies", "it_equipment", "software_service", "professional_service", "other",
];

function invalid(): never {
  throw new Error("approval_response_invalid");
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function closed(value: unknown, keys: readonly string[]): Record<string, unknown> {
  if (!isRecord(value)) invalid();
  const actual = Object.keys(value);
  if (
    actual.length !== keys.length
    || keys.some((key) => !Object.prototype.hasOwnProperty.call(value, key))
  ) invalid();
  return value;
}

function string(value: unknown): string {
  if (typeof value !== "string") invalid();
  return value;
}

function identifier(value: unknown): string {
  const result = string(value);
  if (!/^[a-z][a-z0-9_.-]{0,79}$/.test(result)) invalid();
  return result;
}

function nullableString(value: unknown): string | null {
  return value === null ? null : string(value);
}

function bool(value: unknown): boolean {
  if (typeof value !== "boolean") invalid();
  return value;
}

function integer(value: unknown, minimum = 0): number {
  if (
    typeof value !== "number"
    || value !== value
    || value % 1 !== 0
    || value > 9_007_199_254_740_991
    || value < -9_007_199_254_740_991
    || value < minimum
  ) invalid();
  return value;
}

function uuid(value: unknown): string {
  const result = string(value);
  if (!UUID_PATTERN.test(result)) invalid();
  return result;
}

function decimal(
  value: unknown,
  maximumIntegerDigits: number,
  maximumFractionDigits: number,
  positive = false,
): string {
  const result = string(value);
  const match = DECIMAL_PATTERN.exec(result);
  if (
    match === null
    || match[1].length > maximumIntegerDigits
    || (match[2]?.length ?? 0) > maximumFractionDigits
    || (positive && /^0(?:\.0+)?$/.test(result))
  ) invalid();
  return result;
}

function requestTotal(value: unknown): string {
  // Decimal(16,2), additionally bounded by the V1 total maximum 999999999999.99.
  return decimal(value, 12, 2);
}

function calendarDate(value: unknown): string {
  const result = string(value);
  const match = DATE_PATTERN.exec(result);
  if (match === null || match[1] === "0000") invalid();
  const probe = new Date(`${result}T00:00:00Z`);
  const timestamp = probe.getTime();
  if (timestamp !== timestamp || probe.toISOString().slice(0, 10) !== result) invalid();
  return result;
}

function dateTime(value: unknown): string {
  const result = string(value);
  const match = DATETIME_PATTERN.exec(result);
  if (match === null) invalid();
  calendarDate(match[1]);
  const timestamp = Date.parse(result);
  if (timestamp !== timestamp) invalid();
  return result;
}

function nullableDateTime(value: unknown): string | null {
  return value === null ? null : dateTime(value);
}

function enumValue<T extends string>(value: unknown, allowed: readonly T[]): T {
  const result = string(value);
  const match = allowed.find((candidate) => candidate === result);
  if (match === undefined) invalid();
  return match;
}

function parseSubjectSummary(value: unknown): ProcurementApprovalSubjectSummary {
  const summary = closed(value, ["request_number", "title", "total", "status"]);
  return {
    supported: true,
    request_number: string(summary.request_number),
    title: string(summary.title),
    total: requestTotal(summary.total),
    status: enumValue(summary.status, SUBJECT_STATUSES),
  };
}

function unsupportedSubject(
  processKey: ApprovalProcessKey,
  subjectType: string,
): UnsupportedApprovalSubject {
  return {
    supported: false,
    process_key: processKey,
    subject_type: subjectType,
  };
}

function parseTask(value: unknown): ApprovalTaskSummary {
  const task = closed(value, [
    "task_id", "instance_id", "process_key", "subject_type", "step_key", "step_label",
    "status", "submitted_at", "activated_at", "completed_at", "subject",
  ]);
  const processKey = identifier(task.process_key);
  const subjectType = identifier(task.subject_type);
  const subject = processKey === "procurement.request" && subjectType === "procurement_request"
    ? parseSubjectSummary(task.subject)
    : unsupportedSubject(processKey, subjectType);
  return {
    task_id: uuid(task.task_id),
    instance_id: uuid(task.instance_id),
    process_key: processKey,
    subject_type: subjectType,
    step_key: string(task.step_key),
    step_label: string(task.step_label),
    status: enumValue(task.status, TASK_STATUSES),
    submitted_at: dateTime(task.submitted_at),
    activated_at: nullableDateTime(task.activated_at),
    completed_at: nullableDateTime(task.completed_at),
    subject,
  };
}

function parsePage(value: unknown): ApprovalTaskPage {
  const page = closed(value, ["items", "offset", "limit", "total"]);
  if (!Array.isArray(page.items)) invalid();
  return {
    items: page.items.map(parseTask),
    offset: integer(page.offset),
    limit: integer(page.limit, 1),
    total: integer(page.total),
  };
}

function parseItem(value: unknown): ApprovalSubjectItem {
  const item = closed(value, [
    "category", "name", "specification", "quantity", "unit", "unit_price", "subtotal",
  ]);
  return {
    category: enumValue(item.category, CATEGORIES),
    name: string(item.name),
    specification: nullableString(item.specification),
    quantity: decimal(item.quantity, 10, 2, true),
    unit: string(item.unit),
    unit_price: decimal(item.unit_price, 12, 2),
    subtotal: decimal(item.subtotal, 14, 2),
  };
}

function parseTimeline(value: unknown): ApprovalTimelineEntry {
  const entry = closed(value, [
    "kind", "occurred_at", "step_key", "step_label", "action",
    "actor_display_name", "comment", "status",
  ]);
  return {
    kind: enumValue(entry.kind, ["submitted", "decision", "withdrawn", "completed"]),
    occurred_at: dateTime(entry.occurred_at),
    step_key: nullableString(entry.step_key),
    step_label: nullableString(entry.step_label),
    action: entry.action === null
      ? null
      : enumValue(entry.action, ["approve", "reject"] as const),
    actor_display_name: nullableString(entry.actor_display_name),
    comment: nullableString(entry.comment),
    status: enumValue(entry.status, [
      "submitted", "waiting", "pending", "approved", "rejected", "cancelled",
    ]),
  };
}

function parseSubject(value: unknown): ApprovalSubjectDetail {
  const subject = closed(value, [
    "summary", "purpose", "needed_by_date", "currency", "items", "applicant", "organization", "timeline",
  ]);
  if (!Array.isArray(subject.items) || !Array.isArray(subject.timeline)) invalid();
  const applicant = closed(subject.applicant, ["display_name"]);
  const organization = closed(subject.organization, ["display_name"]);
  if (subject.currency !== "CNY") invalid();
  return {
    supported: true,
    summary: subject.summary === null ? null : parseSubjectSummary(subject.summary),
    purpose: string(subject.purpose),
    needed_by_date: calendarDate(subject.needed_by_date),
    currency: "CNY",
    items: subject.items.map(parseItem),
    applicant: { display_name: string(applicant.display_name) },
    organization: { display_name: string(organization.display_name) },
    timeline: subject.timeline.map(parseTimeline),
  };
}

function parseDetail(value: unknown): ApprovalTaskDetail {
  const detail = closed(value, ["task", "subject"]);
  const task = parseTask(detail.task);
  if (!task.subject.supported) {
    return {
      task,
      subject: unsupportedSubject(task.process_key, task.subject_type),
    };
  }
  return { task, subject: parseSubject(detail.subject) };
}

function parseTransition(value: unknown): ApprovalTransition {
  const transition = closed(value, ["instance_id", "status", "current_step_key", "replayed"]);
  return {
    instance_id: uuid(transition.instance_id),
    status: enumValue(transition.status, INSTANCE_STATUSES),
    current_step_key: nullableString(transition.current_step_key),
    replayed: bool(transition.replayed),
  };
}

function post(body: unknown): RequestInit {
  return { method: "POST", body: JSON.stringify(body) };
}

async function jsonRequest<T>(
  path: string,
  parser: (value: unknown) => T,
  init?: RequestInit,
): Promise<T> {
  const response = await request(path, init);
  let value: unknown;
  try {
    value = await response.json();
  } catch (error) {
    if (error instanceof SyntaxError) invalid();
    throw error;
  }
  return parser(value);
}

function checkedUuid(value: string): string {
  if (!UUID_PATTERN.test(value)) invalid();
  return value;
}

export function newApprovalOperationId(): string {
  return crypto.randomUUID();
}

export function listApprovalTasks(query: ApprovalTaskQuery = {}): Promise<ApprovalTaskPage> {
  const params = new URLSearchParams();
  if (query.status !== undefined) params.set("status", query.status);
  if (query.processKey !== undefined) params.set("process_key", identifier(query.processKey));
  if (query.activatedFrom !== undefined) params.set("activated_from", dateTime(query.activatedFrom));
  if (query.activatedTo !== undefined) params.set("activated_to", dateTime(query.activatedTo));
  if (query.offset !== undefined) params.set("offset", String(query.offset));
  if (query.limit !== undefined) params.set("limit", String(query.limit));
  const suffix = params.size === 0 ? "" : `?${params.toString()}`;
  return jsonRequest(
    `/approvals/tasks${suffix}`,
    parsePage,
    query.signal === undefined ? undefined : { signal: query.signal },
  );
}

export function getApprovalTask(taskId: string, signal?: AbortSignal): Promise<ApprovalTaskDetail> {
  return jsonRequest(
    `/approvals/tasks/${encodeURIComponent(checkedUuid(taskId))}`,
    parseDetail,
    signal === undefined ? undefined : { signal },
  );
}

export function approveTask(
  taskId: string,
  clientOperationId: string,
  comment: string | null = null,
): Promise<ApprovalTransition> {
  return jsonRequest(
    `/approvals/tasks/${encodeURIComponent(checkedUuid(taskId))}/approve`,
    parseTransition,
    post({ client_operation_id: checkedUuid(clientOperationId), comment }),
  );
}

export function rejectTask(
  taskId: string,
  reason: string,
  clientOperationId: string,
): Promise<ApprovalTransition> {
  return jsonRequest(
    `/approvals/tasks/${encodeURIComponent(checkedUuid(taskId))}/reject`,
    parseTransition,
    post({ client_operation_id: checkedUuid(clientOperationId), reason }),
  );
}

export type { ApprovalProcessKey };
