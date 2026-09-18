import { useState } from "react";
import { Loader2, Send } from "lucide-react";
import { Streamdown } from "streamdown";

import { ApiError } from "@/lib/api";
import type { ChatMessage } from "@/lib/types";
import { useRecoveryChat, useRecoveryPlans } from "../lib/queries";

interface RecoveryChatPanelProps {
  planId: string;
  facilityId?: string;
}

/**
 * Per-plan grounded Q&A. The backend is stateless -- no conversation id, no
 * history kept server-side -- so this transcript is purely client state and
 * does not survive a page refresh; every question resends `planId`.
 */
export function RecoveryChatPanel({ planId, facilityId }: RecoveryChatPanelProps) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [question, setQuestion] = useState("");
  const chat = useRecoveryChat();
  const plans = useRecoveryPlans(facilityId);

  function handleSubmit() {
    const trimmed = question.trim();
    // Guards the Enter-key path too, which a disabled button cannot reach.
    if (!trimmed || chat.isPending) return;

    setMessages((prev) => [
      ...prev,
      { id: crypto.randomUUID(), role: "user", content: trimmed },
    ]);
    setQuestion("");

    chat.mutate(
      { planId, question: trimmed, facilityId },
      {
        onSuccess: (response) => {
          setMessages((prev) => [
            ...prev,
            { id: crypto.randomUUID(), role: "assistant", content: response.answer },
          ]);
        },
        onError: (error) => {
          // A 401 already fires the app-wide session-expired redirect; a
          // bubble here would just flash before that navigation happens.
          if (error instanceof ApiError && error.status === 401) return;
          const detail =
            error instanceof ApiError ? error.message : "Could not reach the assistant.";
          setMessages((prev) => [
            ...prev,
            { id: crypto.randomUUID(), role: "assistant", content: "", error: detail },
          ]);
        },
      },
    );
  }

  // A 404 here means the plan_id this page is showing has gone stale against
  // the horizon it was generated for, not that the conversation was invalid
  // -- so the transcript stays, and the fix on offer is refreshing the plan.
  const planWentStale = chat.error instanceof ApiError && chat.error.status === 404;

  return (
    <section className="panel">
      <div className="panel-title">
        <h2>Ask about this plan</h2>
        <span className="hint">grounded in this plan's own numbers</span>
      </div>

      {messages.length > 0 && (
        <div className="chat-transcript" role="log" aria-live="polite">
          {messages.map((message) => (
            <div
              key={message.id}
              className={`chat-bubble ${message.role}${message.error ? " error" : ""}`}
            >
              {message.error ? (
                message.error
              ) : message.role === "assistant" ? (
                <Streamdown controls={false}>{message.content}</Streamdown>
              ) : (
                message.content
              )}
            </div>
          ))}
          {chat.isPending && (
            <div className="chat-bubble assistant pending">Thinking…</div>
          )}
        </div>
      )}

      {planWentStale && (
        <p className="chat-stale-notice">
          This plan may have changed.{" "}
          <button className="row-link" onClick={() => plans.refetch()}>
            Refresh plans
          </button>
        </p>
      )}

      <div className="chat-input-row">
        <textarea
          value={question}
          maxLength={1000}
          disabled={chat.isPending}
          placeholder="Ask about this plan…"
          onChange={(event) => setQuestion(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey) {
              event.preventDefault();
              handleSubmit();
            }
          }}
        />
        <button
          className="btn"
          disabled={chat.isPending || !question.trim()}
          onClick={handleSubmit}
        >
          {chat.isPending ? <Loader2 className="spin" size={16} /> : <Send size={16} />}
        </button>
      </div>
      <span className="chat-counter">{question.length}/1000</span>
    </section>
  );
}
