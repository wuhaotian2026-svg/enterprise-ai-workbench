import { request } from "../api/request";
import type {
  ApprovalInstanceStatus,
  JsonValue,
  ProcurementCategoryCode,
  ProcurementCommandTransition,
  ProcurementConfirmationCancellation,
  ProcurementConfirmationExecution,
  ProcurementConversationDetail,
  ProcurementConversationSummary,
  ProcurementRequestDetail,
  ProcurementRequestInput,
  ProcurementRequestPage,
  ProcurementRequestPreview,
  ProcurementRequestQuery,
  ProcurementRequestStatus,
  ProcurementRequestSummary,
  ProcurementStoredTurn,
  ProcurementSubmitResult,
  ProcurementSubjectItem,
  ProcurementSubjectSummary,
  ProcurementTimelineEntry,
  ProcurementTurnBlock,
  ProcurementTurnResponse,
} from "./types";

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const DECIMAL_PATTERN = /^(0|[1-9][0-9]*)(?:\.([0-9]+))?$/;
const DATE_PATTERN = /^([0-9]{4})-([0-9]{2})-([0-9]{2})$/;
const DATETIME_PATTERN = /^([0-9]{4}-[0-9]{2}-[0-9]{2})T(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9](?:\.[0-9]+)?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])$/;

const REQUEST_STATUSES: readonly ProcurementRequestStatus[] = [
  "pending_manager", "pending_procurement", "approved", "rejected", "cancelled",
];
const INSTANCE_STATUSES: readonly ApprovalInstanceStatus[] = [
  "running", "approved", "rejected", "cancelled",
];
const CATEGORIES: readonly ProcurementCategoryCode[] = [
  "office_supplies", "it_equipment", "software_service", "professional_service", "other",
];

function invalid(): never {
  throw new Error("procurement_response_invalid");
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

function nullableString(value: unknown): string | null {
  if (value === null) return null;
  return string(value);
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

function canonicalMoney(value: unknown, maximumIntegerDigits: number): string {
  const result = string(value);
  const match = /^(0|[1-9][0-9]*)\.([0-9]{2})$/.exec(result);
  if (match === null || match[1].length > maximumIntegerDigits) invalid();
  return result;
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

function enumValue<T extends string>(value: unknown, allowed: readonly T[]): T {
  const result = string(value);
  const match = allowed.find((candidate) => candidate === result);
  if (match === undefined) invalid();
  return match;
}

function jsonValue(value: unknown): JsonValue {
  if (value === null || typeof value === "boolean" || typeof value === "string") return value;
  if (typeof value === "number") {
    if (value !== value || value === Infinity || value === -Infinity) invalid();
    return value;
  }
  if (Array.isArray(value)) return value.map(jsonValue);
  if (isRecord(value)) {
    return Object.fromEntries(
      Object.entries(value).map(([key, item]) => [key, jsonValue(item)]),
    );
  }
  return invalid();
}

function jsonObject(value: unknown): { [key: string]: JsonValue } {
  if (!isRecord(value)) invalid();
  return Object.fromEntries(
    Object.entries(value).map(([key, item]) => [key, jsonValue(item)]),
  );
}

function requestStatus(value: unknown): ProcurementRequestStatus {
  return enumValue(value, REQUEST_STATUSES);
}

function parseSummary(value: unknown): ProcurementRequestSummary {
  const item = closed(value, [
    "id", "request_number", "title", "total", "status", "submitted_at",
  ]);
  return {
    id: uuid(item.id),
    request_number: string(item.request_number),
    title: string(item.title),
    total: requestTotal(item.total),
    status: requestStatus(item.status),
    submitted_at: dateTime(item.submitted_at),
  };
}

function parseSubmit(value: unknown): ProcurementSubmitResult {
  const item = closed(value, [
    "id", "request_number", "title", "total", "status", "submitted_at", "replayed",
  ]);
  return { ...parseSummary(Object.fromEntries(
    Object.entries(item).filter(([key]) => key !== "replayed"),
  )), replayed: bool(item.replayed) };
}

function parsePreview(value: unknown, itemCount: number): ProcurementRequestPreview {
  if (itemCount < 1 || itemCount > 50) invalid();
  const preview = closed(value, ["currency", "subtotals", "total"]);
  if (
    preview.currency !== "CNY"
    || !Array.isArray(preview.subtotals)
    || preview.subtotals.length !== itemCount
  ) invalid();
  return {
    currency: "CNY",
    subtotals: preview.subtotals.map((subtotal) => canonicalMoney(subtotal, 12)),
    total: canonicalMoney(preview.total, 12),
  };
}

function parsePage(value: unknown): ProcurementRequestPage {
  const page = closed(value, ["items", "offset", "limit", "total"]);
  if (!Array.isArray(page.items)) invalid();
  return {
    items: page.items.map(parseSummary),
    offset: integer(page.offset),
    limit: integer(page.limit, 1),
    total: integer(page.total),
  };
}

function parseSubjectSummary(value: unknown): ProcurementSubjectSummary {
  const summary = closed(value, ["request_number", "title", "total", "status"]);
  return {
    request_number: string(summary.request_number),
    title: string(summary.title),
    total: requestTotal(summary.total),
    status: requestStatus(summary.status),
  };
}

function parseSubjectItem(value: unknown): ProcurementSubjectItem {
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

function parseTimeline(value: unknown): ProcurementTimelineEntry {
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

function parseDetail(value: unknown): ProcurementRequestDetail {
  const detail = closed(value, [
    "id", "summary", "purpose", "needed_by_date", "currency", "items", "applicant", "organization", "timeline",
  ]);
  if (!Array.isArray(detail.items) || !Array.isArray(detail.timeline)) invalid();
  const applicant = closed(detail.applicant, ["display_name"]);
  const organization = closed(detail.organization, ["display_name"]);
  if (detail.currency !== "CNY") invalid();
  return {
    id: uuid(detail.id),
    summary: parseSubjectSummary(detail.summary),
    purpose: string(detail.purpose),
    needed_by_date: calendarDate(detail.needed_by_date),
    currency: "CNY",
    items: detail.items.map(parseSubjectItem),
    applicant: { display_name: string(applicant.display_name) },
    organization: { display_name: string(organization.display_name) },
    timeline: detail.timeline.map(parseTimeline),
  };
}

function parseTransition(value: unknown): ProcurementCommandTransition {
  const transition = closed(value, ["instance_id", "status", "current_step_key", "replayed"]);
  return {
    instance_id: uuid(transition.instance_id),
    status: enumValue(transition.status, INSTANCE_STATUSES),
    current_step_key: nullableString(transition.current_step_key),
    replayed: bool(transition.replayed),
  };
}

function parseConversationSummary(value: unknown): ProcurementConversationSummary {
  const conversation = closed(value, ["id", "title", "created_at", "updated_at"]);
  return {
    id: uuid(conversation.id),
    title: string(conversation.title),
    created_at: dateTime(conversation.created_at),
    updated_at: dateTime(conversation.updated_at),
  };
}

function parseTurnBlock(value: unknown): ProcurementTurnBlock {
  if (!isRecord(value)) invalid();
  switch (value.type) {
    case "text": {
      const block = closed(value, ["type", "text"]);
      return { type: "text", text: string(block.text) };
    }
    case "policy_citations": {
      const block = closed(value, ["type", "citations"]);
      if (!Array.isArray(block.citations)) invalid();
      return {
        type: "policy_citations",
        citations: block.citations.map((citationValue) => {
          const citation = closed(citationValue, [
            "number", "chunk_id", "document_name", "page_number", "evidence_snapshot",
          ]);
          return {
            number: integer(citation.number, 1),
            chunk_id: string(citation.chunk_id),
            document_name: string(citation.document_name),
            page_number: citation.page_number === null ? null : integer(citation.page_number, 1),
            evidence_snapshot: string(citation.evidence_snapshot),
          };
        }),
      };
    }
    case "policy_clarification": {
      const block = closed(value, ["type", "questions"]);
      if (!Array.isArray(block.questions) || block.questions.length < 1 || block.questions.length > 3) invalid();
      return { type: "policy_clarification", questions: block.questions.map(string) };
    }
    case "business_facts": {
      const block = closed(value, ["type", "facts", "queried_at"]);
      if (!Array.isArray(block.facts)) invalid();
      return {
        type: "business_facts",
        facts: block.facts.map((factValue) => {
          const fact = closed(factValue, ["label", "value"]);
          return { label: string(fact.label), value: jsonValue(fact.value) };
        }),
        queried_at: dateTime(block.queried_at),
      };
    }
    case "clarification": {
      const block = closed(value, ["type", "missing_fields", "suggestions"]);
      if (!Array.isArray(block.missing_fields) || !Array.isArray(block.suggestions)) invalid();
      return {
        type: "clarification",
        missing_fields: block.missing_fields.map(string),
        suggestions: block.suggestions.map(string),
      };
    }
    case "assistant_draft": {
      const block = closed(value, [
        "type", "module_key", "intent", "status", "version", "fields",
        "pending_fields", "missing_fields",
      ]);
      if (!Array.isArray(block.pending_fields) || !Array.isArray(block.missing_fields)) invalid();
      return {
        type: "assistant_draft",
        module_key: enumValue(block.module_key, ["hr", "procurement"]),
        intent: string(block.intent),
        status: enumValue(block.status, ["active", "closed", "cleared", "expired"]),
        version: integer(block.version, 1),
        fields: jsonObject(block.fields),
        pending_fields: block.pending_fields.map(string),
        missing_fields: block.missing_fields.map(string),
      };
    }
    case "confirmation": {
      const block = closed(value, ["type", "confirmation_id", "tool_name", "preview", "expires_at"]);
      return {
        type: "confirmation",
        confirmation_id: uuid(block.confirmation_id),
        tool_name: string(block.tool_name),
        preview: jsonObject(block.preview),
        expires_at: dateTime(block.expires_at),
      };
    }
    case "execution_result": {
      const block = closed(value, ["type", "resource_type", "resource_id", "result"]);
      return {
        type: "execution_result",
        resource_type: string(block.resource_type),
        resource_id: uuid(block.resource_id),
        result: jsonObject(block.result),
      };
    }
    case "error": {
      const block = closed(value, ["type", "code", "retryable"]);
      return { type: "error", code: string(block.code), retryable: bool(block.retryable) };
    }
    default:
      return invalid();
  }
}

function parseStoredTurn(value: unknown): ProcurementStoredTurn {
  const turn = closed(value, [
    "id", "client_turn_id", "role", "request_content", "text", "blocks", "created_at",
  ]);
  if (!Array.isArray(turn.blocks)) invalid();
  return {
    id: uuid(turn.id),
    client_turn_id: uuid(turn.client_turn_id),
    role: enumValue(turn.role, ["user", "assistant"]),
    request_content: string(turn.request_content),
    text: string(turn.text),
    blocks: turn.blocks.map(parseTurnBlock),
    created_at: dateTime(turn.created_at),
  };
}

function parseTurnResponse(value: unknown): ProcurementTurnResponse {
  const turn = closed(value, [
    "id", "client_turn_id", "role", "request_content", "text", "blocks", "created_at",
    "replayed",
  ]);
  return {
    ...parseStoredTurn(Object.fromEntries(
      Object.entries(turn).filter(([key]) => key !== "replayed"),
    )),
    replayed: bool(turn.replayed),
  };
}

function parseConversationDetail(value: unknown): ProcurementConversationDetail {
  const conversation = closed(value, ["id", "title", "created_at", "updated_at", "turns"]);
  if (!Array.isArray(conversation.turns)) invalid();
  return {
    ...parseConversationSummary(Object.fromEntries(
      Object.entries(conversation).filter(([key]) => key !== "turns"),
    )),
    turns: conversation.turns.map(parseStoredTurn),
  };
}

function parseExecution(value: unknown): ProcurementConfirmationExecution {
  const result = closed(value, ["type", "resource_type", "resource_id", "replayed"]);
  if (result.type !== "execution_result" || result.resource_type !== "procurement_request") invalid();
  return {
    type: "execution_result",
    resource_type: "procurement_request",
    resource_id: uuid(result.resource_id),
    replayed: bool(result.replayed),
  };
}

function parseCancellation(value: unknown): ProcurementConfirmationCancellation {
  const result = closed(value, ["confirmation_id", "status"]);
  if (result.status !== "cancelled") invalid();
  return { confirmation_id: uuid(result.confirmation_id), status: "cancelled" };
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

export function newProcurementOperationId(): string {
  return crypto.randomUUID();
}

export function listProcurementRequests(
  query: ProcurementRequestQuery = {},
): Promise<ProcurementRequestPage> {
  const params = new URLSearchParams();
  if (query.status !== undefined) params.set("status", query.status);
  if (query.submittedFrom !== undefined) params.set("submitted_from", calendarDate(query.submittedFrom));
  if (query.submittedTo !== undefined) params.set("submitted_to", calendarDate(query.submittedTo));
  if (query.offset !== undefined) params.set("offset", String(query.offset));
  if (query.limit !== undefined) params.set("limit", String(query.limit));
  const suffix = params.size === 0 ? "" : `?${params.toString()}`;
  return jsonRequest(
    `/procurement/requests${suffix}`,
    parsePage,
    query.signal === undefined ? undefined : { signal: query.signal },
  );
}

export function getProcurementRequest(
  requestId: string,
  signal?: AbortSignal,
): Promise<ProcurementRequestDetail> {
  return jsonRequest(
    `/procurement/requests/${encodeURIComponent(checkedUuid(requestId))}`,
    parseDetail,
    signal === undefined ? undefined : { signal },
  );
}

export function submitProcurementRequest(
  input: ProcurementRequestInput,
  clientOperationId: string,
): Promise<ProcurementSubmitResult> {
  return jsonRequest(
    "/procurement/requests",
    parseSubmit,
    post({ ...input, client_operation_id: checkedUuid(clientOperationId) }),
  );
}

export function previewProcurementRequest(
  input: ProcurementRequestInput,
  signal?: AbortSignal,
): Promise<ProcurementRequestPreview> {
  return jsonRequest(
    "/procurement/requests/preview",
    (value) => parsePreview(value, input.items.length),
    { ...post(input), ...(signal === undefined ? {} : { signal }) },
  );
}

export function withdrawProcurementRequest(
  requestId: string,
  clientOperationId: string,
): Promise<ProcurementCommandTransition> {
  return jsonRequest(
    `/procurement/requests/${encodeURIComponent(checkedUuid(requestId))}/withdraw`,
    parseTransition,
    post({ client_operation_id: checkedUuid(clientOperationId) }),
  );
}

export function createProcurementConversation(
  title?: string,
): Promise<ProcurementConversationSummary> {
  return jsonRequest(
    "/procurement/conversations",
    parseConversationSummary,
    post(title === undefined ? {} : { title }),
  );
}

export function listProcurementConversations(
  signal?: AbortSignal,
): Promise<ProcurementConversationSummary[]> {
  return jsonRequest(
    "/procurement/conversations",
    (value) => {
      if (!Array.isArray(value)) invalid();
      return value.map(parseConversationSummary);
    },
    signal === undefined ? undefined : { signal },
  );
}

export function getProcurementConversation(
  conversationId: string,
  signal?: AbortSignal,
): Promise<ProcurementConversationDetail> {
  return jsonRequest(
    `/procurement/conversations/${encodeURIComponent(checkedUuid(conversationId))}`,
    parseConversationDetail,
    signal === undefined ? undefined : { signal },
  );
}

export function sendProcurementTurn(
  conversationId: string,
  text: string,
  clientTurnId: string,
): Promise<ProcurementTurnResponse> {
  return jsonRequest(
    `/procurement/conversations/${encodeURIComponent(checkedUuid(conversationId))}/turns`,
    parseTurnResponse,
    post({ client_turn_id: checkedUuid(clientTurnId), text }),
  );
}

export function confirmProcurementSubmission(
  confirmationId: string,
  clientOperationId: string,
): Promise<ProcurementConfirmationExecution> {
  return jsonRequest(
    `/procurement/confirmations/${encodeURIComponent(checkedUuid(confirmationId))}/confirm`,
    parseExecution,
    post({ client_operation_id: checkedUuid(clientOperationId) }),
  );
}

export function cancelProcurementConfirmation(
  confirmationId: string,
): Promise<ProcurementConfirmationCancellation> {
  return jsonRequest(
    `/procurement/confirmations/${encodeURIComponent(checkedUuid(confirmationId))}/cancel`,
    parseCancellation,
    post({}),
  );
}
