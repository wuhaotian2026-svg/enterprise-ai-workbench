export type ApprovalTaskStatus =
  | "waiting"
  | "pending"
  | "approved"
  | "rejected"
  | "cancelled";

export type ApprovalInstanceStatus =
  | "running"
  | "approved"
  | "rejected"
  | "cancelled";

export type ApprovalSubjectStatus =
  | "pending_manager"
  | "pending_procurement"
  | "approved"
  | "rejected"
  | "cancelled";

export type ApprovalSubjectType = string;
export type ApprovalProcessKey = string;

export type UnsupportedApprovalSubject = {
  supported: false;
  process_key: ApprovalProcessKey;
  subject_type: ApprovalSubjectType;
};

export type ProcurementApprovalSubjectSummary = {
  supported: true;
  request_number: string;
  title: string;
  total: string;
  status: ApprovalSubjectStatus;
};

export type ApprovalSubjectSummary =
  | ProcurementApprovalSubjectSummary
  | UnsupportedApprovalSubject;

export type ApprovalTaskSummary = {
  task_id: string;
  instance_id: string;
  process_key: string;
  subject_type: ApprovalSubjectType;
  step_key: string;
  step_label: string;
  status: ApprovalTaskStatus;
  submitted_at: string;
  activated_at: string | null;
  completed_at: string | null;
  subject: ApprovalSubjectSummary;
};

export type ApprovalSubjectItem = {
  category:
    | "office_supplies"
    | "it_equipment"
    | "software_service"
    | "professional_service"
    | "other";
  name: string;
  specification: string | null;
  quantity: string;
  unit: string;
  unit_price: string;
  subtotal: string;
};

export type ApprovalTimelineEntry = {
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

export type ProcurementApprovalSubjectDetail = {
  supported: true;
  summary: ProcurementApprovalSubjectSummary | null;
  purpose: string;
  needed_by_date: string;
  currency: "CNY";
  items: ApprovalSubjectItem[];
  applicant: { display_name: string };
  organization: { display_name: string };
  timeline: ApprovalTimelineEntry[];
};

export type ApprovalSubjectDetail =
  | ProcurementApprovalSubjectDetail
  | UnsupportedApprovalSubject;

export type ApprovalTaskDetail = {
  task: ApprovalTaskSummary;
  subject: ApprovalSubjectDetail;
};

export type ApprovalTaskPage = {
  items: ApprovalTaskSummary[];
  offset: number;
  limit: number;
  total: number;
};

export type ApprovalTaskQuery = {
  status?: ApprovalTaskStatus;
  processKey?: ApprovalProcessKey;
  activatedFrom?: string;
  activatedTo?: string;
  offset?: number;
  limit?: number;
  signal?: AbortSignal;
};

export type ApprovalTransition = {
  instance_id: string;
  status: ApprovalInstanceStatus;
  current_step_key: string | null;
  replayed: boolean;
};
