export type ProcurementRequestStatus =
  | "pending_manager"
  | "pending_procurement"
  | "approved"
  | "rejected"
  | "cancelled";

export type ApprovalInstanceStatus =
  | "running"
  | "approved"
  | "rejected"
  | "cancelled";

export type ProcurementCategoryCode =
  | "office_supplies"
  | "it_equipment"
  | "software_service"
  | "professional_service"
  | "other";

export type ProcurementCurrencyCode = "CNY";

export type ProcurementRequestItemInput = {
  category_code: ProcurementCategoryCode;
  item_name: string;
  specification: string | null;
  quantity: string;
  unit: string;
  estimated_unit_price: string;
};

export type ProcurementRequestInput = {
  title: string;
  purpose: string;
  needed_by_date: string;
  currency: ProcurementCurrencyCode;
  items: ProcurementRequestItemInput[];
};

export type ProcurementRequestPreview = {
  currency: ProcurementCurrencyCode;
  subtotals: string[];
  total: string;
};

export type ProcurementRequestSummary = {
  id: string;
  request_number: string;
  title: string;
  total: string;
  status: ProcurementRequestStatus;
  submitted_at: string;
};

export type ProcurementSubmitResult = ProcurementRequestSummary & {
  replayed: boolean;
};

export type ProcurementRequestPage = {
  items: ProcurementRequestSummary[];
  offset: number;
  limit: number;
  total: number;
};

export type ProcurementSubjectSummary = {
  request_number: string;
  title: string;
  total: string;
  status: ProcurementRequestStatus;
};

export type ProcurementSubjectItem = {
  category: ProcurementCategoryCode;
  name: string;
  specification: string | null;
  quantity: string;
  unit: string;
  unit_price: string;
  subtotal: string;
};

export type ProcurementTimelineEntry = {
  kind: "submitted" | "decision" | "withdrawn" | "completed";
  occurred_at: string;
  step_key: string | null;
  step_label: string | null;
  action: "approve" | "reject" | null;
  actor_display_name: string | null;
  comment: string | null;
  status:
    | "submitted"
    | "waiting"
    | "pending"
    | "approved"
    | "rejected"
    | "cancelled";
};

export type ProcurementRequestDetail = {
  id: string;
  summary: ProcurementSubjectSummary;
  purpose: string;
  needed_by_date: string;
  currency: ProcurementCurrencyCode;
  items: ProcurementSubjectItem[];
  applicant: { display_name: string };
  organization: { display_name: string };
  timeline: ProcurementTimelineEntry[];
};

export type ProcurementRequestQuery = {
  status?: ProcurementRequestStatus;
  submittedFrom?: string;
  submittedTo?: string;
  offset?: number;
  limit?: number;
  signal?: AbortSignal;
};

export type ProcurementCommandTransition = {
  instance_id: string;
  status: ApprovalInstanceStatus;
  current_step_key: string | null;
  replayed: boolean;
};

export type ProcurementConversationSummary = {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
};

export type JsonValue =
  | null
  | boolean
  | number
  | string
  | JsonValue[]
  | { [key: string]: JsonValue };

export type ProcurementTurnBlock =
  | { type: "text"; text: string }
  | {
      type: "policy_citations";
      citations: Array<{
        number: number;
        chunk_id: string;
        document_name: string;
        page_number: number | null;
        evidence_snapshot: string;
      }>;
    }
  | { type: "policy_clarification"; questions: string[] }
  | {
      type: "business_facts";
      facts: Array<{ label: string; value: JsonValue }>;
      queried_at: string;
    }
  | { type: "clarification"; missing_fields: string[]; suggestions: string[] }
  | {
      type: "assistant_draft";
      module_key: "hr" | "procurement";
      intent: string;
      status: "active" | "closed" | "cleared" | "expired";
      version: number;
      fields: { [key: string]: JsonValue };
      pending_fields: string[];
      missing_fields: string[];
    }
  | {
      type: "confirmation";
      confirmation_id: string;
      tool_name: string;
      preview: { [key: string]: JsonValue };
      expires_at: string;
    }
  | {
      type: "execution_result";
      resource_type: string;
      resource_id: string;
      result: { [key: string]: JsonValue };
    }
  | { type: "error"; code: string; retryable: boolean };

export type ProcurementStoredTurn = {
  id: string;
  client_turn_id: string;
  role: "user" | "assistant";
  request_content?: string;
  text: string;
  blocks: ProcurementTurnBlock[];
  created_at: string;
};

export type ProcurementTurnResponse = ProcurementStoredTurn & { replayed: boolean };

export type ProcurementConversationDetail = ProcurementConversationSummary & {
  turns: ProcurementStoredTurn[];
};

export type ProcurementConfirmationExecution = {
  type: "execution_result";
  resource_type: "procurement_request";
  resource_id: string;
  replayed: boolean;
};

export type ProcurementConfirmationCancellation = {
  confirmation_id: string;
  status: "cancelled";
};
