import { request } from "../api/request";
import type {
  ConfirmationCancelResult,
  ExecutionResultBlock,
  HrConversationDetail,
  HrConversationSummary,
  HrReviewRequest,
  HrTurnResponse,
  LeaveRequest,
} from "./types";

async function json<T>(path: string, init?: RequestInit): Promise<T> {
  return await (await request(path, init)).json() as T;
}

export function newClientId(): string {
  return crypto.randomUUID();
}

export async function listConversations(): Promise<HrConversationSummary[]> {
  return await json("/hr/conversations");
}

export async function createConversation(title?: string): Promise<HrConversationSummary> {
  return await json("/hr/conversations", {
    method: "POST",
    body: JSON.stringify(title ? { title } : {}),
  });
}

export async function getConversation(id: string): Promise<HrConversationDetail> {
  return await json(`/hr/conversations/${encodeURIComponent(id)}`);
}

export async function archiveConversation(id: string): Promise<void> {
  await request(`/hr/conversations/${encodeURIComponent(id)}/archive`, {
    method: "POST",
    body: JSON.stringify({}),
  });
}

export async function sendTurn(
  conversationId: string,
  text: string,
  clientTurnId = newClientId(),
): Promise<HrTurnResponse> {
  return await json(`/hr/conversations/${encodeURIComponent(conversationId)}/turns`, {
    method: "POST",
    body: JSON.stringify({ client_turn_id: clientTurnId, text }),
  });
}

export async function confirmTool(
  confirmationId: string,
  clientOperationId: string,
): Promise<ExecutionResultBlock> {
  return await json(`/hr/confirmations/${encodeURIComponent(confirmationId)}/confirm`, {
    method: "POST",
    body: JSON.stringify({ client_operation_id: clientOperationId }),
  });
}

export async function cancelTool(confirmationId: string): Promise<ConfirmationCancelResult> {
  return await json(`/hr/confirmations/${encodeURIComponent(confirmationId)}/cancel`, {
    method: "POST",
    body: JSON.stringify({}),
  });
}

export async function listLeaveRequests(): Promise<LeaveRequest[]> {
  return await json("/hr/leave-requests");
}

export async function createCancelIntent(
  requestId: string,
  clientOperationId: string,
): Promise<import("./types").ConfirmationBlock> {
  return await json(`/hr/leave-requests/${encodeURIComponent(requestId)}/cancel-intent`, {
    method: "POST",
    body: JSON.stringify({ client_operation_id: clientOperationId }),
  });
}

export async function listReviewQueue(
  status: import("./types").LeaveRequestStatus = "pending",
): Promise<HrReviewRequest[]> {
  return await json(`/hr/review-queue?status=${encodeURIComponent(status)}`);
}

export async function getReviewDetail(requestId: string): Promise<HrReviewRequest> {
  return await json(`/hr/review-queue/${encodeURIComponent(requestId)}`);
}

export async function approveLeaveRequest(
  requestId: string,
  clientOperationId: string,
): Promise<LeaveRequest> {
  return await json(`/hr/review-queue/${encodeURIComponent(requestId)}/approve`, {
    method: "POST",
    body: JSON.stringify({ client_operation_id: clientOperationId }),
  });
}

export async function rejectLeaveRequest(
  requestId: string,
  clientOperationId: string,
  reason: string,
): Promise<LeaveRequest> {
  return await json(`/hr/review-queue/${encodeURIComponent(requestId)}/reject`, {
    method: "POST",
    body: JSON.stringify({ client_operation_id: clientOperationId, reason }),
  });
}
