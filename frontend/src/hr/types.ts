export type LeaveRequestStatus = "pending" | "approved" | "rejected" | "cancelled";

export type HrConversationSummary = {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
};

export type LeaveBalance = {
  account_id?: string;
  leave_type_code: "annual" | "compensatory" | string;
  leave_type_name: string;
  year: number;
  entitled: string;
  used: string;
  reserved: string;
  available: string;
};

export type LeaveRequest = {
  id: string;
  request_number: string;
  leave_type_code: string;
  leave_type_name: string;
  start_date: string;
  end_date: string;
  workday_count: string;
  reason?: string;
  status: LeaveRequestStatus;
  submitted_at: string;
  reviewed_at: string | null;
  rejection_reason: string | null;
  cancelled_at: string | null;
};

export type HrReviewRequest = LeaveRequest & {
  employee_number: string;
  employee_display_name: string;
  reviewer_user_id: string | null;
};

export type PolicyCitation = {
  number: number;
  chunk_id: string;
  document_name: string;
  page_number: number | null;
  evidence_snapshot: string;
};

export type BusinessFact = { label: string; value: unknown };

export type ConfirmationBlock = {
  type: "confirmation";
  confirmation_id: string;
  tool_name: string;
  preview: Record<string, unknown>;
  expires_at: string;
};

export type ExecutionResultBlock = {
  type: "execution_result";
  resource_type: string;
  resource_id: string;
  result: Record<string, unknown>;
};

export type ConfirmationTerminalBlock = {
  type: "confirmation_terminal";
  confirmation_id: string;
  tool_name: string;
  status: "cancelled" | "expired" | "executed";
};

export type HrTurnBlock =
  | { type: "text"; text: string }
  | { type: "policy_citations"; citations: PolicyCitation[] }
  | { type: "business_facts"; facts: BusinessFact[]; queried_at: string }
  | { type: "clarification"; missing_fields: string[]; suggestions: string[] }
  | {
      type: "assistant_draft";
      module_key: "hr" | "procurement";
      intent: string;
      status: "active" | "closed" | "cleared" | "expired";
      version: number;
      fields: Record<string, unknown>;
      pending_fields: string[];
      missing_fields: string[];
    }
  | ConfirmationBlock
  | ConfirmationTerminalBlock
  | ExecutionResultBlock
  | { type: "error"; code: string; retryable: boolean };

export type HrTurnResponse = {
  client_turn_id: string;
  text: string;
  blocks: HrTurnBlock[];
  model_calls: number;
  read_calls: number;
  write_proposals: number;
};

export type HrStoredTurn = HrTurnResponse & { created_at: string };

export type HrConversationDetail = HrConversationSummary & {
  turns: HrStoredTurn[];
};

export type ConfirmationCancelResult = {
  confirmation_id: string;
  status: "cancelled";
};
