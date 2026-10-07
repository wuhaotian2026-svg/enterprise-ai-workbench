import { ThumbsDown, ThumbsUp } from "lucide-react";
import { useState } from "react";

export function FeedbackControls({ onFeedback }: { onFeedback(helpful: boolean): Promise<void> }) {
  const [saved, setSaved] = useState(false);
  async function choose(value: boolean) { await onFeedback(value); setSaved(true); }
  return <div className="feedback-controls"><span>{saved ? "反馈已记录" : "这条回答有帮助吗？"}</span><div><button aria-label="有帮助" onClick={() => void choose(true)}><ThumbsUp aria-hidden="true" /></button><button aria-label="没有帮助" onClick={() => void choose(false)}><ThumbsDown aria-hidden="true" /></button></div></div>;
}
