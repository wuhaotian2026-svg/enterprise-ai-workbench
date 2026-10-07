import {
  Activity,
  AlertTriangle,
  Clock3,
  Database,
  GitBranch,
  RefreshCw,
  ShieldCheck,
  Wrench,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { listOrganizationUnits, loadAnalyticsDashboard } from "./client";
import type {
  AnalyticsDashboardData,
  AnalyticsRangeDays,
  MetricValue,
  OrganizationUnit,
} from "./types";

type LoadState =
  | { kind: "loading" }
  | { kind: "ready"; data: AnalyticsDashboardData }
  | { kind: "error" };

const ranges: AnalyticsRangeDays[] = [7, 30, 90];

const labels: Record<string, string> = {
  questions_submitted: "知识提问",
  leave_requests_submitted: "请假提交",
  tools_planned: "工具计划",
  leave_requests_pending: "当前待审批",
  hr_turns_submitted: "HR 对话提交",
  intents_resolved: "意图识别完成",
  write_tools_planned: "写工具计划",
  confirmations_shown: "展示确认",
  confirmations_confirmed: "用户已确认",
  leave_requests_reviewed: "请假已审核",
  evidence_answer_rate: "有引用回答率",
  clarification_rate: "需要补充信息率",
  abstention_rate: "严格拒答率",
  response_latency_p50_ms: "回答时延 P50",
  response_latency_p95_ms: "回答时延 P95",
  validation_failure_rate: "参数校验失败率",
  provider_failure_rate: "模型 Provider 失败率",
  read_latency_p50_ms: "读工具时延 P50",
  read_latency_p95_ms: "读工具时延 P95",
  leave_requests_processed: "窗口内已处理",
  review_duration_p50_ms: "审核耗时 P50",
  review_duration_p95_ms: "审核耗时 P95",
  leave_requests_pending_over_24h: "积压超过 24h",
};

function metricLabel(key: string): string {
  return labels[key] ?? key.replaceAll("_", " ");
}

function displayValue(key: string, metric: MetricValue): string {
  if (!metric.available || metric.value === null) return "暂无数据";
  if (key.endsWith("_rate")) return `${Math.round(metric.value * 1000) / 10}%`;
  if (key.endsWith("_ms")) {
    if (key.startsWith("review_duration_") && metric.value >= 3_600_000) {
      const hours = metric.value / 3_600_000;
      return `${Number.isInteger(hours) ? hours : hours.toFixed(1)} 小时`;
    }
    return `${Math.round(metric.value)} ms`;
  }
  return new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 1 }).format(metric.value);
}

function MetricCard({ metricKey, metric }: { metricKey: string; metric: MetricValue }) {
  return (
    <article className={metric.available ? "analytics-metric" : "analytics-metric unavailable"}>
      <header>
        <span>{metricLabel(metricKey)}</span>
        {metric.available ? <Activity aria-hidden="true" /> : <AlertTriangle aria-hidden="true" />}
      </header>
      <strong>{displayValue(metricKey, metric)}</strong>
      <div className="analytics-metric-meta">
        {metric.denominator !== null && (
          <span>{metric.numerator ?? 0} / {metric.denominator}</span>
        )}
        <span>样本 {metric.sample_size}</span>
        <span>口径 {metric.metric_version}</span>
      </div>
      {!metric.available && metric.sample_size > 0 && metric.sample_size < 5 && (
        <p>样本量 {metric.sample_size}，低于组织细分展示阈值</p>
      )}
    </article>
  );
}

function MetricSection({
  title,
  index,
  icon: Icon,
  metrics,
  keys,
}: {
  title: string;
  index: string;
  icon: typeof Database;
  metrics: Record<string, MetricValue>;
  keys: string[];
}) {
  return (
    <section className="analytics-section">
      <header>
        <span>{index}</span>
        <Icon aria-hidden="true" />
        <h2>{title}</h2>
      </header>
      <div className="analytics-section-grid">
        {keys.map((key) => metrics[key] && (
          <MetricCard key={key} metricKey={key} metric={metrics[key]} />
        ))}
      </div>
    </section>
  );
}

function Funnel({ metrics }: { metrics: Record<string, MetricValue> }) {
  const stages = [
    "hr_turns_submitted",
    "intents_resolved",
    "write_tools_planned",
    "confirmations_shown",
    "confirmations_confirmed",
    "leave_requests_submitted",
    "leave_requests_reviewed",
  ];
  return (
    <section className="analytics-funnel" aria-labelledby="hr-funnel-title">
      <header><GitBranch aria-hidden="true" /><h2 id="hr-funnel-title">HR 流程</h2><span>相邻阶段漏斗</span></header>
      <ol>
        {stages.map((key, index) => {
          const metric = metrics[key];
          if (!metric) return null;
          return (
            <li key={key}>
              <span>{String(index + 1).padStart(2, "0")}</span>
              <strong>{metricLabel(key)}</strong>
              <b>{displayValue(key, metric)}</b>
              <small>样本 {metric.sample_size} · 口径 {metric.metric_version}</small>
            </li>
          );
        })}
      </ol>
    </section>
  );
}

export function AnalyticsDashboardPage() {
  const [days, setDays] = useState<AnalyticsRangeDays>(7);
  const [organizationUnitId, setOrganizationUnitId] = useState<string | null>(null);
  const [organizations, setOrganizations] = useState<OrganizationUnit[] | null>(null);
  const [state, setState] = useState<LoadState>({ kind: "loading" });
  const [reloadToken, setReloadToken] = useState(0);
  const generation = useRef(0);

  useEffect(() => {
    let disposed = false;
    void listOrganizationUnits()
      .then((units) => {
        if (!disposed) setOrganizations(units.filter((unit) => unit.is_active));
      })
      .catch(() => {
        if (!disposed) setOrganizations([]);
      });
    return () => { disposed = true; };
  }, []);

  useEffect(() => {
    const requestGeneration = generation.current + 1;
    generation.current = requestGeneration;
    const controller = new AbortController();
    setState({ kind: "loading" });
    void loadAnalyticsDashboard({
      days,
      organizationUnitId,
      signal: controller.signal,
    })
      .then((data) => {
        if (generation.current === requestGeneration) {
          setState({ kind: "ready", data });
        }
      })
      .catch((error: unknown) => {
        if (
          generation.current === requestGeneration
          && !(error instanceof DOMException && error.name === "AbortError")
        ) {
          setState({ kind: "error" });
        }
      });
    return () => controller.abort();
  }, [days, organizationUnitId, reloadToken]);

  const data = state.kind === "ready" ? state.data : null;
  return (
    <main className="analytics-page">
      <header className="analytics-title">
        <div>
          <span className="eyebrow">07 / OPERATIONS CONTROL</span>
          <h1>运营驾驶舱</h1>
          <p>用固定口径观察知识质量、受控工具与人工审批，不对员工做绩效排名。</p>
        </div>
        <ShieldCheck aria-hidden="true" />
      </header>

      <section className="analytics-controls" aria-label="指标范围">
        <div className="analytics-range" role="group" aria-label="时间范围">
          {ranges.map((range) => (
            <button
              type="button"
              key={range}
              aria-pressed={days === range}
              onClick={() => setDays(range)}
            >
              最近 {range} 天
            </button>
          ))}
        </div>
        <label>
          组织范围
          <select
            aria-label="组织范围"
            value={organizationUnitId ?? ""}
            onChange={(event) => setOrganizationUnitId(event.target.value || null)}
          >
            <option value="">当前授权范围</option>
            {(organizations ?? []).map((unit) => (
              <option key={unit.id} value={unit.id}>{unit.name} · {unit.code}</option>
            ))}
          </select>
        </label>
        <p>
          <Clock3 aria-hidden="true" />
          {data === null
            ? `最近 ${days} 天`
            : `${data.overview.from.slice(0, 10)} — ${data.overview.to.slice(0, 10)}`}
        </p>
      </section>

      {state.kind === "loading" && (
        <div className="analytics-loading" role="status">正在汇总运营指标…</div>
      )}
      {state.kind === "error" && (
        <div className="analytics-loading analytics-load-error">
          <AlertTriangle aria-hidden="true" />
          <p role="alert">运营指标暂时无法读取，请检查网络后重试。</p>
          <button type="button" onClick={() => setReloadToken((value) => value + 1)}>
            <RefreshCw aria-hidden="true" />重试
          </button>
        </div>
      )}
      {data !== null && (
        <>
          <section className="analytics-overview" aria-label="核心概览">
            {Object.entries(data.overview.metrics).map(([key, metric]) => (
              <MetricCard key={key} metricKey={key} metric={metric} />
            ))}
          </section>

          <div className="analytics-detail-grid">
            <MetricSection
              index="01"
              title="知识质量"
              icon={Database}
              metrics={data.knowledge.metrics}
              keys={[
                "evidence_answer_rate",
                "clarification_rate",
                "abstention_rate",
                "response_latency_p50_ms",
                "response_latency_p95_ms",
              ]}
            />
            <MetricSection
              index="02"
              title="Tool Calling"
              icon={Wrench}
              metrics={data.tools.metrics}
              keys={["validation_failure_rate", "provider_failure_rate", "read_latency_p50_ms", "read_latency_p95_ms"]}
            />
          </div>

          <Funnel metrics={data.hrFunnel.metrics} />

          <MetricSection
            index="04"
            title="审批工作流"
            icon={Clock3}
            metrics={data.workflows.metrics}
            keys={["leave_requests_pending", "leave_requests_processed", "review_duration_p50_ms", "review_duration_p95_ms", "leave_requests_pending_over_24h"]}
          />
        </>
      )}
    </main>
  );
}
