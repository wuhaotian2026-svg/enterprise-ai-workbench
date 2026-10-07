import {
  BookOpenText,
  BriefcaseBusiness,
  ChartNoAxesCombined,
  ClipboardList,
  FileCheck2,
  LibraryBig,
  Network,
  ShoppingCart,
  UserRoundCheck,
  type LucideIcon,
} from "lucide-react";
import type { ReactNode } from "react";
import type { CurrentUser } from "../api/client";
import { DocumentAdminPage } from "../documents/DocumentAdminPage";
import { HrAssistantPage } from "../hr/HrAssistantPage";
import { HrReviewPage } from "../hr/HrReviewPage";
import { MyLeaveRequestsPage } from "../hr/MyLeaveRequestsPage";
import { ApprovalCenterPage } from "../approvals/ApprovalCenterPage";
import { ProcurementPage } from "../procurement/ProcurementPage";
import { QuestionWorkspace } from "../questions/QuestionWorkspace";
import { OrganizationPage } from "./OrganizationPage";
import { AnalyticsDashboardPage } from "./AnalyticsDashboardPage";
import type { WorkbenchModuleKey } from "./types";

export type ModuleRenderContext = {
  user: CurrentUser;
  onLogout(): Promise<void> | void;
};

export type RegisteredModule = {
  key: WorkbenchModuleKey;
  icon: LucideIcon;
  render(context: ModuleRenderContext): ReactNode;
};

export const moduleRegistry = {
  knowledge: {
    key: "knowledge",
    icon: BookOpenText,
    render: ({ user, onLogout }) => (
      <QuestionWorkspace embedded username={user.username} onLogout={onLogout} />
    ),
  },
  "hr-assistant": {
    key: "hr-assistant",
    icon: BriefcaseBusiness,
    render: () => <HrAssistantPage />,
  },
  "my-requests": {
    key: "my-requests",
    icon: ClipboardList,
    render: () => <MyLeaveRequestsPage />,
  },
  "hr-review": {
    key: "hr-review",
    icon: UserRoundCheck,
    render: () => <HrReviewPage />,
  },
  "knowledge-admin": {
    key: "knowledge-admin",
    icon: LibraryBig,
    render: ({ user, onLogout }) => (
      <DocumentAdminPage embedded username={user.username} onLogout={onLogout} />
    ),
  },
  organization: {
    key: "organization",
    icon: Network,
    render: () => <OrganizationPage />,
  },
  analytics: {
    key: "analytics",
    icon: ChartNoAxesCombined,
    render: () => <AnalyticsDashboardPage />,
  },
  procurement: {
    key: "procurement",
    icon: ShoppingCart,
    render: () => <ProcurementPage />,
  },
  "approval-center": {
    key: "approval-center",
    icon: FileCheck2,
    render: () => <ApprovalCenterPage />,
  },
} satisfies Partial<Record<WorkbenchModuleKey, RegisteredModule>>;

export type RegisteredModuleKey = keyof typeof moduleRegistry;

export function isRegisteredModuleKey(key: string): key is RegisteredModuleKey {
  return Object.prototype.hasOwnProperty.call(moduleRegistry, key);
}
