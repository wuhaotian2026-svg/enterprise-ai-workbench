import { AlertTriangle, Check, RotateCcw } from "lucide-react";
import { useEffect, useRef } from "react";

type SubmitConfirmation = {
  kind: "submit";
  title: string;
  total: string;
};

type WithdrawConfirmation = {
  kind: "withdraw";
  requestId: string;
  requestNumber: string;
  title: string;
  total: string;
};

export type ProcurementConfirmation = SubmitConfirmation | WithdrawConfirmation;

export function ProcurementConfirmationCard({
  confirmation,
  busy,
  ambiguous,
  onConfirm,
  onCancel,
}: {
  confirmation: ProcurementConfirmation;
  busy: boolean;
  ambiguous: boolean;
  onConfirm(): void;
  onCancel(): void;
}) {
  const withdrawing = confirmation.kind === "withdraw";
  const dialogRef = useRef<HTMLElement | null>(null);
  const safeButtonRef = useRef<HTMLButtonElement | null>(null);
  const onCancelRef = useRef(onCancel);
  onCancelRef.current = onCancel;

  useEffect(() => {
    const dialog = dialogRef.current;
    const trigger = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    if (!dialog) return;
    const focusable = () => Array.from(dialog.querySelectorAll<HTMLElement>(
      'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
    ));
    safeButtonRef.current?.focus();
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        onCancelRef.current();
        return;
      }
      if (event.key !== "Tab") return;
      const elements = focusable();
      if (elements.length === 0) return;
      const first = elements[0];
      const last = elements[elements.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("keydown", handleKeyDown);
      trigger?.focus();
    };
  }, []);

  return (
    <section
      ref={dialogRef}
      className="procurement-confirmation-card"
      role="dialog"
      aria-modal="true"
      aria-label={withdrawing ? "撤回采购申请确认" : "提交采购申请确认"}
    >
      <header>
        <span><AlertTriangle aria-hidden="true" />需要你的明确确认</span>
        <strong>{withdrawing ? "WITHDRAW" : "SUBMIT"}</strong>
      </header>
      <h2>{withdrawing ? "确认撤回当前申请？" : "确认提交采购申请？"}</h2>
      <dl>
        {withdrawing && <div><dt>申请编号</dt><dd>{confirmation.requestNumber}</dd></div>}
        {!withdrawing && <div><dt>申请编号</dt><dd>提交后由服务器生成</dd></div>}
        <div><dt>申请标题</dt><dd>{confirmation.title}</dd></div>
        <div><dt>权威总额</dt><dd>¥{confirmation.total}</dd></div>
        {!withdrawing && <div className="procurement-confirmation-route"><dt>审批路线</dt><dd>直属部门负责人审批 → 采购专员复核 → 完成</dd></div>}
      </dl>
      {ambiguous && <p role="alert">上次请求结果暂时不明确。使用同一操作号重试可安全查询或完成同一笔操作，不会创建新的业务意图。</p>}
      <div>
        <button ref={safeButtonRef} type="button" disabled={busy} onClick={onCancel}><RotateCcw aria-hidden="true" />返回修改</button>
        <button type="button" disabled={busy} onClick={onConfirm}><Check aria-hidden="true" />{busy ? "处理中…" : ambiguous ? "使用同一操作号重试" : withdrawing ? "确认撤回" : "确认提交"}</button>
      </div>
    </section>
  );
}
