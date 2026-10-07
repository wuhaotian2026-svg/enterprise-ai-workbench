import {
  AlertTriangle,
  ChevronRight,
  Network,
  Plus,
  ShieldCheck,
  UserRoundCog,
  X,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ApiError } from "../api/request";
import {
  createCapabilityGrant,
  createOrganizationUnit,
  listCapabilityGrants,
  listOrganizationEmployees,
  listOrganizationUnits,
  newWorkbenchOperationId,
  revokeCapabilityGrant,
  updateEmployeeAssignment,
  updateOrganizationUnit,
} from "./client";
import type {
  CapabilityGrant,
  EmployeeAssignment,
  OrganizationUnit,
} from "./types";

type ReadyData = {
  units: OrganizationUnit[];
  employees: EmployeeAssignment[];
  grants: CapabilityGrant[];
};

type LoadState =
  | { kind: "loading" }
  | ({ kind: "ready" | "empty" } & ReadyData)
  | { kind: "error"; code: string };

type FormError = string | null;
type DialogKind = "create" | "unit" | "employee" | "revoke";
type UnitMode = "view" | "edit";

const capabilityOptions = [
  ["knowledge.manage", "知识管理"],
  ["organization.manage", "组织管理"],
  ["analytics.view", "运营数据查看"],
  ["hr.leave.review", "请假审核"],
] as const;

function errorCode(error: unknown): string {
  return error instanceof ApiError ? error.code : "request_failed";
}

function errorMessage(code: string): string {
  const messages: Record<string, string> = {
    capability_required: "当前账号没有企业组织管理权限",
    organization_unit_cycle: "组织层级不能形成循环",
    organization_unit_not_empty: "该组织仍有在职员工，暂时不能停用",
    organization_unit_not_found: "组织信息已变化，请刷新后重试",
    employee_assignment_invalid: "员工归属信息已变化，请刷新后重试",
    manager_assignment_invalid: "直属经理必须有效且位于员工组织的管理子树",
    capability_grant_not_found: "授权信息已变化，请刷新后重试",
    capability_grant_conflict: "该授权与当前权限状态冲突",
    security_audit_operation_conflict: "本次操作标识与较早请求冲突，请刷新",
  };
  return messages[code] ?? "企业组织操作暂时未完成，请稍后重试";
}

function unitName(units: OrganizationUnit[], unitId: string | null): string {
  if (unitId === null) return "未分配";
  return units.find((unit) => unit.id === unitId)?.name ?? "组织已不可用";
}

function employeeName(
  employees: EmployeeAssignment[],
  userId: string,
): string {
  return employees.find((item) => item.user_id === userId)?.display_name
    ?? "用户已不可用";
}

function OrganizationTree({
  units,
  parentId,
  depth = 0,
  onSelect,
}: {
  units: OrganizationUnit[];
  parentId: string | null;
  depth?: number;
  onSelect(unit: OrganizationUnit, trigger: HTMLButtonElement): void;
}) {
  const children = units.filter((unit) => unit.parent_id === parentId);
  if (children.length === 0) return null;
  return (
    <ul className="organization-tree-level" data-depth={depth}>
      {children.map((unit) => (
        <li key={unit.id}>
          <button
            type="button"
            onClick={(event) => onSelect(unit, event.currentTarget)}
          >
            <ChevronRight aria-hidden="true" />
            <span>
              <strong>{unit.name}</strong>
              <small>{unit.code} · {unit.is_active ? "启用" : "已停用"}</small>
            </span>
          </button>
          <OrganizationTree
            units={units}
            parentId={unit.id}
            depth={depth + 1}
            onSelect={onSelect}
          />
        </li>
      ))}
    </ul>
  );
}

export function OrganizationPage() {
  const [state, setState] = useState<LoadState>({ kind: "loading" });
  const [reloadToken, setReloadToken] = useState(0);
  const [formError, setFormError] = useState<FormError>(null);
  const [busy, setBusy] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);
  const [createCode, setCreateCode] = useState("");
  const [createName, setCreateName] = useState("");
  const [createParent, setCreateParent] = useState<string | null>(null);
  const [createOperation, setCreateOperation] = useState<string | null>(null);
  const [selectedUnit, setSelectedUnit] = useState<OrganizationUnit | null>(null);
  const [unitMode, setUnitMode] = useState<UnitMode>("view");
  const [unitUpdateOperation, setUnitUpdateOperation] = useState<string | null>(null);
  const [unitDeactivateOperation, setUnitDeactivateOperation] = useState<string | null>(null);
  const [editEmployee, setEditEmployee] = useState<EmployeeAssignment | null>(null);
  const [employeeUnit, setEmployeeUnit] = useState("");
  const [employeeManager, setEmployeeManager] = useState("");
  const [assignmentOperation, setAssignmentOperation] = useState<string | null>(null);
  const [revokeGrant, setRevokeGrant] = useState<CapabilityGrant | null>(null);
  const [revokeConfirmed, setRevokeConfirmed] = useState(false);
  const [revokeOperation, setRevokeOperation] = useState<string | null>(null);
  const [grantUser, setGrantUser] = useState("");
  const [grantCapability, setGrantCapability] = useState("analytics.view");
  const [grantScope, setGrantScope] = useState<"global" | "unit_subtree">("global");
  const [grantUnit, setGrantUnit] = useState("");
  const [grantOperation, setGrantOperation] = useState<string | null>(null);
  const dialogRef = useRef<HTMLElement | null>(null);
  const initialFocusRef = useRef<HTMLElement | null>(null);
  const dialogTriggerRef = useRef<HTMLButtonElement | null>(null);
  const unitNameRef = useRef<HTMLInputElement | null>(null);

  const load = useCallback(async () => {
    setState({ kind: "loading" });
    setFormError(null);
    try {
      const [units, employees, grants] = await Promise.all([
        listOrganizationUnits(),
        listOrganizationEmployees(),
        listCapabilityGrants(),
      ]);
      setState({
        kind: units.length === 0 ? "empty" : "ready",
        units,
        employees,
        grants,
      });
    } catch (error) {
      setState({ kind: "error", code: errorCode(error) });
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load, reloadToken]);

  const data = state.kind === "ready" || state.kind === "empty" ? state : null;
  const activeUnits = useMemo(
    () => data?.units.filter((unit) => unit.is_active) ?? [],
    [data],
  );
  const dialogKind: DialogKind | null = createOpen
    ? "create"
    : selectedUnit !== null
      ? "unit"
      : editEmployee !== null
        ? "employee"
        : revokeGrant !== null
          ? "revoke"
          : null;

  useEffect(() => {
    if (dialogKind === null || dialogRef.current === null) return;
    const activeDialogKind = dialogKind;
    const dialog = dialogRef.current;
    const trigger = dialogTriggerRef.current;
    const focusable = () => Array.from(dialog.querySelectorAll<HTMLElement>(
      'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
    ));
    (initialFocusRef.current ?? focusable()[0] ?? dialog).focus();

    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        event.preventDefault();
        closeActiveDialog(activeDialogKind);
        return;
      }
      if (event.key !== "Tab") return;
      const elements = focusable();
      if (elements.length === 0) {
        event.preventDefault();
        dialog.focus();
        return;
      }
      const first = elements[0];
      const last = elements[elements.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    }

    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("keydown", handleKeyDown);
      if (trigger?.isConnected) trigger.focus();
    };
  }, [dialogKind]);

  async function submitCreate(event: React.FormEvent) {
    event.preventDefault();
    if (busy) return;
    const operation = createOperation ?? newWorkbenchOperationId();
    setCreateOperation(operation);
    setBusy(true);
    setFormError(null);
    try {
      await createOrganizationUnit({
        client_operation_id: operation,
        code: createCode.trim().toUpperCase(),
        name: createName.trim(),
        parent_id: createParent,
      });
      setCreateOpen(false);
      setCreateCode("");
      setCreateName("");
      setCreateParent(null);
      setCreateOperation(null);
      await load();
    } catch (error) {
      setFormError(errorMessage(errorCode(error)));
    } finally {
      setBusy(false);
    }
  }

  async function saveSelectedUnit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy || selectedUnit === null) return;
    const form = new FormData(event.currentTarget);
    const operation = unitUpdateOperation ?? newWorkbenchOperationId();
    setUnitUpdateOperation(operation);
    setBusy(true);
    setFormError(null);
    try {
      await updateOrganizationUnit(selectedUnit.id, {
        client_operation_id: operation,
        name: String(form.get("unit_name") ?? "").trim(),
        parent_id: String(form.get("unit_parent") ?? "") || null,
      });
      closeSelectedUnit();
      await load();
    } catch (error) {
      setFormError(errorMessage(errorCode(error)));
    } finally {
      setBusy(false);
    }
  }

  async function deactivateSelectedUnit() {
    if (busy || selectedUnit === null) return;
    const operation = unitDeactivateOperation ?? newWorkbenchOperationId();
    setUnitDeactivateOperation(operation);
    setBusy(true);
    setFormError(null);
    try {
      await updateOrganizationUnit(selectedUnit.id, {
        client_operation_id: operation,
        is_active: false,
      });
      closeSelectedUnit();
      await load();
    } catch (error) {
      setFormError(errorMessage(errorCode(error)));
    } finally {
      setBusy(false);
    }
  }

  function rememberDialogTrigger(trigger: HTMLButtonElement) {
    dialogTriggerRef.current = trigger;
  }

  function openCreate(trigger: HTMLButtonElement) {
    rememberDialogTrigger(trigger);
    setFormError(null);
    setCreateOpen(true);
  }

  function closeCreate() {
    setCreateOpen(false);
    setFormError(null);
  }

  function openSelectedUnit(
    unit: OrganizationUnit,
    trigger: HTMLButtonElement,
  ) {
    rememberDialogTrigger(trigger);
    setSelectedUnit(unit);
    setUnitMode("view");
    setUnitUpdateOperation(null);
    setUnitDeactivateOperation(null);
    setFormError(null);
  }

  function closeSelectedUnit() {
    setSelectedUnit(null);
    setUnitMode("view");
    setUnitUpdateOperation(null);
    setUnitDeactivateOperation(null);
    setFormError(null);
  }

  function editSelectedUnit() {
    setUnitMode("edit");
    setFormError(null);
    window.setTimeout(() => unitNameRef.current?.focus(), 0);
  }

  function openEmployee(
    employee: EmployeeAssignment,
    trigger: HTMLButtonElement,
  ) {
    rememberDialogTrigger(trigger);
    setEditEmployee(employee);
    setEmployeeUnit(employee.organization_unit_id ?? "");
    setEmployeeManager(employee.manager_employee_id ?? "");
    setAssignmentOperation(null);
    setFormError(null);
  }

  function closeEmployee() {
    setEditEmployee(null);
    setAssignmentOperation(null);
    setFormError(null);
  }

  async function saveEmployee(event: React.FormEvent) {
    event.preventDefault();
    if (busy || editEmployee === null) return;
    const operation = assignmentOperation ?? newWorkbenchOperationId();
    setAssignmentOperation(operation);
    setBusy(true);
    setFormError(null);
    try {
      await updateEmployeeAssignment(editEmployee.id, {
        client_operation_id: operation,
        organization_unit_id: employeeUnit || null,
        manager_employee_id: employeeManager || null,
      });
      setEditEmployee(null);
      setAssignmentOperation(null);
      await load();
    } catch (error) {
      setFormError(errorMessage(errorCode(error)));
    } finally {
      setBusy(false);
    }
  }

  function openRevoke(grant: CapabilityGrant, trigger: HTMLButtonElement) {
    rememberDialogTrigger(trigger);
    setRevokeGrant(grant);
    setRevokeConfirmed(false);
    setRevokeOperation(newWorkbenchOperationId());
    setFormError(null);
  }

  function closeRevoke() {
    setRevokeGrant(null);
    setRevokeConfirmed(false);
    setRevokeOperation(null);
    setFormError(null);
  }

  function closeActiveDialog(kind: DialogKind) {
    if (kind === "create") closeCreate();
    else if (kind === "unit") closeSelectedUnit();
    else if (kind === "employee") closeEmployee();
    else closeRevoke();
  }

  async function confirmRevoke() {
    if (busy || !revokeConfirmed || revokeGrant === null || revokeOperation === null) {
      return;
    }
    setBusy(true);
    setFormError(null);
    try {
      await revokeCapabilityGrant(revokeGrant.id, revokeOperation);
      setRevokeGrant(null);
      setRevokeConfirmed(false);
      setRevokeOperation(null);
      await load();
    } catch (error) {
      setFormError(errorMessage(errorCode(error)));
    } finally {
      setBusy(false);
    }
  }

  async function saveGrant(event: React.FormEvent) {
    event.preventDefault();
    if (busy || !grantUser) return;
    const operation = grantOperation ?? newWorkbenchOperationId();
    setGrantOperation(operation);
    setBusy(true);
    setFormError(null);
    try {
      await createCapabilityGrant({
        client_operation_id: operation,
        user_id: grantUser,
        capability: grantCapability,
        scope_kind: grantScope,
        organization_unit_id: grantScope === "unit_subtree" ? grantUnit || null : null,
      });
      setGrantOperation(null);
      await load();
    } catch (error) {
      setFormError(errorMessage(errorCode(error)));
    } finally {
      setBusy(false);
    }
  }

  if (state.kind === "loading") {
    return <div className="organization-loading" role="status">正在读取企业组织…</div>;
  }
  if (state.kind === "error") {
    return (
      <div className="organization-loading organization-load-error">
        <AlertTriangle aria-hidden="true" />
        <p role="alert">{errorMessage(state.code)}</p>
        <button type="button" onClick={() => setReloadToken((value) => value + 1)}>
          重试
        </button>
      </div>
    );
  }
  if (data === null) return null;

  return (
    <main className="organization-page">
      <header className="organization-title">
        <div>
          <span className="eyebrow">06 / ORGANIZATION CONTROL</span>
          <h1>企业组织</h1>
          <p>维护组织关系、员工归属和可审计的能力授权。</p>
        </div>
        <button type="button" onClick={(event) => openCreate(event.currentTarget)}>
          <Plus aria-hidden="true" />新增部门
        </button>
      </header>

      {formError && <p className="organization-error" role="alert">{formError}</p>}
      {state.kind === "empty" && (
        <section className="organization-empty">
          <Network aria-hidden="true" />
          <h2>尚未建立企业组织</h2>
          <p>从第一个根部门开始，随后再分配员工与权限范围。</p>
        </section>
      )}

      <div className="organization-layout">
        <section className="organization-tree-panel">
          <header><span>01</span><h2>组织结构</h2></header>
          <nav aria-label="组织树">
            <OrganizationTree
              units={data.units}
              parentId={null}
              onSelect={openSelectedUnit}
            />
          </nav>
        </section>

        <div className="organization-operations">
          <section className="organization-section">
            <header><UserRoundCog aria-hidden="true" /><h2>员工归属</h2><span>{data.employees.length}</span></header>
            <div className="organization-table" role="table" aria-label="员工归属列表">
              {data.employees.map((item) => (
                <div className="organization-row" role="row" key={item.id}>
                  <div><strong>{item.display_name}</strong><small>{item.employee_number}</small></div>
                  <span>{unitName(data.units, item.organization_unit_id)}</span>
                  <span>{item.is_active ? "在职" : "已离职"}</span>
                  <button type="button" onClick={(event) => openEmployee(item, event.currentTarget)} aria-label={`编辑 ${item.display_name}`}>编辑</button>
                </div>
              ))}
            </div>
          </section>

          <section className="organization-section grant-section">
            <header><ShieldCheck aria-hidden="true" /><h2>能力授权</h2><span>{data.grants.length}</span></header>
            <div className="organization-table" role="table" aria-label="能力授权列表">
              {data.grants.map((grant) => (
                <div className="organization-row grant-row" role="row" key={grant.id}>
                  <div><strong>{grant.capability}</strong><small>{employeeName(data.employees, grant.user_id)}</small></div>
                  <span>{grant.scope_kind === "global" ? "全企业" : `${unitName(data.units, grant.organization_unit_id)}及下级组织`}</span>
                  <span>{grant.is_active ? "生效中" : "已撤销"}</span>
                  {grant.is_active && <button type="button" onClick={(event) => openRevoke(grant, event.currentTarget)} aria-label={`撤销 ${grant.capability} 授权`}>撤销</button>}
                </div>
              ))}
            </div>
            <form className="grant-create" onSubmit={(event) => void saveGrant(event)}>
              <label>授权员工<select value={grantUser} onChange={(event) => { setGrantUser(event.target.value); setGrantOperation(null); }} required><option value="">请选择</option>{data.employees.map((item) => <option key={item.id} value={item.user_id}>{item.display_name}</option>)}</select></label>
              <label>能力<select value={grantCapability} onChange={(event) => { setGrantCapability(event.target.value); setGrantOperation(null); }}>{capabilityOptions.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
              <label>范围<select value={grantScope} onChange={(event) => { setGrantScope(event.target.value as "global" | "unit_subtree"); setGrantOperation(null); }}><option value="global">全企业</option><option value="unit_subtree">组织及下级</option></select></label>
              {grantScope === "unit_subtree" && <label>授权组织<select value={grantUnit} onChange={(event) => { setGrantUnit(event.target.value); setGrantOperation(null); }} required><option value="">请选择</option>{activeUnits.map((unit) => <option key={unit.id} value={unit.id}>{unit.name}</option>)}</select></label>}
              <button type="submit" disabled={busy || !grantUser}>添加授权</button>
            </form>
          </section>
        </div>
      </div>

      {createOpen && (
        <div className="organization-overlay" role="presentation">
          <form ref={(node) => { dialogRef.current = node; }} className="organization-dialog" role="dialog" aria-modal="true" aria-labelledby="organization-create-title" tabIndex={-1} onSubmit={(event) => void submitCreate(event)}>
            <button type="button" className="dialog-close" aria-label="关闭新增部门" onClick={closeCreate}><X aria-hidden="true" /></button>
            <span className="eyebrow">NEW UNIT</span><h2 id="organization-create-title">新增部门</h2>
            <label>部门编码<input ref={(node) => { initialFocusRef.current = node; }} aria-label="部门编码" value={createCode} pattern="[A-Z0-9_-]+" onChange={(event) => { setCreateCode(event.target.value.toUpperCase()); setCreateOperation(null); }} required /></label>
            <label>部门名称<input aria-label="部门名称" value={createName} onChange={(event) => { setCreateName(event.target.value); setCreateOperation(null); }} required /></label>
            <label>上级组织<select value={createParent ?? ""} onChange={(event) => { setCreateParent(event.target.value || null); setCreateOperation(null); }}><option value="">作为根组织</option>{activeUnits.map((unit) => <option key={unit.id} value={unit.id}>{unit.name}</option>)}</select></label>
            <button type="submit" disabled={busy}>保存部门</button>
          </form>
        </div>
      )}

      {selectedUnit && (
        <div className="organization-overlay" role="presentation">
          <section ref={(node) => { dialogRef.current = node; }} className="organization-dialog" role="dialog" aria-modal="true" aria-labelledby="organization-unit-title" tabIndex={-1}>
            <button ref={(node) => { initialFocusRef.current = node; }} type="button" className="dialog-close" aria-label={unitMode === "view" ? "关闭部门详情" : "关闭部门编辑"} onClick={closeSelectedUnit}><X aria-hidden="true" /></button>
            {unitMode === "view" ? <>
              <span className="eyebrow">UNIT / {selectedUnit.code}</span><h2 id="organization-unit-title">{selectedUnit.name}</h2>
              <dl className="organization-unit-details">
                <div><dt>部门名称</dt><dd>{selectedUnit.name}</dd></div>
                <div><dt>部门编码</dt><dd>{selectedUnit.code}</dd></div>
                <div><dt>上级组织</dt><dd>{selectedUnit.parent_id === null ? "无（根组织）" : unitName(data.units, selectedUnit.parent_id)}</dd></div>
                <div><dt>当前状态</dt><dd>{selectedUnit.is_active ? "启用" : "已停用"}</dd></div>
                <div><dt>直属员工</dt><dd>{data.employees.filter((item) => item.organization_unit_id === selectedUnit.id).length} 人</dd></div>
              </dl>
              <div className="dialog-actions organization-detail-actions"><button type="button" onClick={editSelectedUnit}>编辑部门</button></div>
            </> : <>
              <span className="eyebrow">EDIT UNIT / {selectedUnit.code}</span><h2 id="organization-unit-title">编辑部门</h2>
              <form className="organization-unit-form" onSubmit={(event) => void saveSelectedUnit(event)}>
                <label>部门名称<input ref={unitNameRef} name="unit_name" defaultValue={selectedUnit.name} onChange={() => setUnitUpdateOperation(null)} required /></label>
                <label>上级组织<select name="unit_parent" defaultValue={selectedUnit.parent_id ?? ""} onChange={() => setUnitUpdateOperation(null)}><option value="">作为根组织</option>{activeUnits.filter((unit) => unit.id !== selectedUnit.id).map((unit) => <option key={unit.id} value={unit.id}>{unit.name}</option>)}</select></label>
                <div className="dialog-actions"><button type="button" className="danger-action" onClick={() => void deactivateSelectedUnit()} disabled={busy || !selectedUnit.is_active}>停用部门</button><button type="submit" disabled={busy}>保存变更</button></div>
              </form>
            </>}
          </section>
        </div>
      )}

      {editEmployee && (
        <div className="organization-overlay" role="presentation">
          <form ref={(node) => { dialogRef.current = node; }} className="organization-dialog" role="dialog" aria-modal="true" aria-labelledby="organization-employee-title" tabIndex={-1} onSubmit={(event) => void saveEmployee(event)}>
            <button type="button" className="dialog-close" aria-label="关闭员工编辑" onClick={closeEmployee}><X aria-hidden="true" /></button>
            <span className="eyebrow">EMPLOYEE / {editEmployee.employee_number}</span><h2 id="organization-employee-title">{editEmployee.display_name}</h2>
            <label>主组织<select ref={(node) => { initialFocusRef.current = node; }} aria-label="主组织" value={employeeUnit} onChange={(event) => { setEmployeeUnit(event.target.value); setAssignmentOperation(null); }}><option value="">未分配</option>{activeUnits.map((unit) => <option key={unit.id} value={unit.id}>{unit.name}</option>)}</select></label>
            <label>直属经理<select aria-label="直属经理" value={employeeManager} onChange={(event) => { setEmployeeManager(event.target.value); setAssignmentOperation(null); }}><option value="">未设置</option>{data.employees.filter((item) => item.id !== editEmployee.id && item.is_active).map((item) => <option key={item.id} value={item.id}>{item.display_name}</option>)}</select></label>
            <button type="submit" disabled={busy}>保存员工归属</button>
          </form>
        </div>
      )}

      {revokeGrant && (
        <div className="organization-overlay" role="presentation">
          <section ref={(node) => { dialogRef.current = node; }} className="organization-dialog revoke-dialog" role="dialog" aria-modal="true" aria-labelledby="revoke-title" tabIndex={-1}>
            <button ref={(node) => { initialFocusRef.current = node; }} type="button" className="dialog-close" aria-label="关闭撤销授权" onClick={closeRevoke}><X aria-hidden="true" /></button>
            <AlertTriangle aria-hidden="true" /><h2 id="revoke-title">撤销授权</h2>
            <p>撤销后，该用户将立即失去对应范围内的 <strong>{revokeGrant.capability}</strong> 能力。</p>
            <label className="confirm-check"><input type="checkbox" checked={revokeConfirmed} onChange={(event) => setRevokeConfirmed(event.target.checked)} />我已确认撤销影响</label>
            <button type="button" onClick={() => void confirmRevoke()} disabled={busy || !revokeConfirmed}>确认撤销授权</button>
          </section>
        </div>
      )}
    </main>
  );
}
