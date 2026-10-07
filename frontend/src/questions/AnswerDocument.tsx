import { AlertTriangle, CircleHelp, ShieldQuestion } from "lucide-react";
import type { QuestionDetail } from "../api/client";
import { CitationList } from "./CitationList";
import { FeedbackControls } from "./FeedbackControls";

const refusalCopy: Record<string, string> = {
  corpus_unavailable: "当前没有可用的制度资料，因此无法确认答案。",
  conflicting_evidence: "检索到的制度证据存在冲突，因此无法确认答案。",
  low_confidence: "现有证据的可信度不足，因此无法确认答案。",
  citation_validation_failed: "回答与引用未能相互验证，因此无法确认答案。",
  no_evidence: "现有制度中没有找到足够证据，因此无法确认答案。",
};

export function AnswerDocument({ question, onFeedback }: { question: QuestionDetail; onFeedback(answerId: string, helpful: boolean): Promise<void> }) {
  const answer = question.answer;
  if (!answer) return <section className="answer-state" aria-live="polite"><p>问题正在处理中，请稍候。</p></section>;
  if (answer.status === "abstained") return <article className="answer-document abstention"><span className="answer-kicker"><ShieldQuestion aria-hidden="true" />STRICT ABSTENTION</span><h2>无法从现有制度确认</h2><p>{refusalCopy[answer.refusal_reason ?? ""] ?? refusalCopy.no_evidence}</p><div className="escalation-note"><AlertTriangle aria-hidden="true" /><span>请咨询人力资源或行政负责人，并提供具体业务场景。</span></div></article>;
  if (answer.status === "needs_clarification") return <article className="answer-document clarification"><span className="answer-kicker"><CircleHelp aria-hidden="true" />NEEDS CLARIFICATION / 需要补充信息</span><h1>{question.text}</h1><div className="answer-body">{answer.text}</div><section className="clarification-questions" aria-labelledby="clarification-title"><h2 id="clarification-title">请补充以下事实</h2><ol>{answer.clarification.questions.map(item => <li key={item}>{item}</li>)}</ol><p>请在下方重新描述完整场景。</p></section>{answer.citations.length > 0 && <CitationList citations={answer.citations} />}<FeedbackControls onFeedback={helpful => onFeedback(answer.id, helpful)} /></article>;
  return <article className="answer-document"><span className="answer-kicker">SUPPORTED ANSWER / 证据充分</span><h1>{question.text}</h1><div className="answer-body">{answer.text}</div>{answer.citations.length > 0 && <CitationList citations={answer.citations} />}<FeedbackControls onFeedback={helpful => onFeedback(answer.id, helpful)} /></article>;
}
