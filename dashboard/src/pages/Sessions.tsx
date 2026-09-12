import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { X } from "lucide-react";
import { api, SessionDetail, type HandoverInfo } from "../api";
import MenuPreview from "../MenuPreview";
import { Badge, Empty, ErrorBox, OUTCOME_TONE, STEP_TONE, ago, fmt, usePoll } from "../ui";

export default function Sessions() {
  const { data, error, reload } = usePoll(() => api.sessions(), 5000);
  const [sel, setSel] = useState<string | null>(null);
  const people = usePoll(() => api.handovers(), 5000);
  const withPerson = (phone: string) => (people.data ?? []).some((p) => p.phone === phone);

  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold">Live sessions</h1>
      <ErrorBox msg={error} />
      <WithAPerson people={people.data ?? []} onChange={async () => { await people.reload(); await reload(); }} />
      <WorkflowRuns />
      <div className="card p-0 overflow-auto">
        {!data || data.length === 0 ? (
          <Empty>No sessions yet.</Empty>
        ) : (
          <table className="w-full">
            <thead><tr><th className="th">Phone</th><th className="th">Customer</th><th className="th">Step</th><th className="th">SO / FG</th><th className="th">Lang</th><th className="th">Updated</th><th className="th"></th></tr></thead>
            <tbody>
              {data.map((s) => (
                <tr key={s.phone} onClick={() => setSel(s.phone)} className="cursor-pointer hover:bg-slate-50"
                  title={`Open the chat with ${s.phone}`}>
                  <td className="td font-mono">{s.phone}</td>
                  <td className="td">{s.customer_name || <span className="text-rose-600">unknown</span>}</td>
                  <td className="td"><Badge tone={STEP_TONE[s.step]}>{s.step}</Badge>{s.pending_value && <span className="text-xs text-slate-500 ml-1">? {s.pending_value}</span>}</td>
                  <td className="td font-mono text-xs">{s.so_no || "—"}{s.fg_code ? ` / ${s.fg_code}` : ""}{s.attempts ? <span className="text-amber-600 ml-1">({s.attempts} tries)</span> : null}</td>
                  <td className="td">{s.language}</td>
                  <td className="td text-xs text-slate-500" title={fmt(s.updated_at)}>{ago(s.updated_at)}</td>
                  <td className="td text-right text-xs text-brand-700">Open chat</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {sel && (
        <ChatPanel phone={sel} withPerson={withPerson(sel)} onClose={() => setSel(null)}
          onChanged={async () => { await reload(); await people.reload(); }} />
      )}
    </div>
  );
}

/** One customer's conversation, opened the way an agent reads a chat rather than as a preview pane:
 *  the messages fill the room, and where the bot has got to sits beside them. Read-only - the bot
 *  answers by itself, and your team answers in WATI. */
function ChatPanel({ phone, withPerson, onClose, onChanged }: {
  phone: string;
  withPerson: boolean;
  onClose: () => void;
  onChanged: () => Promise<void>;
}) {
  const detail = usePoll<SessionDetail | null>(() => api.session(phone), 5000, [phone]);
  const [busy, setBusy] = useState(false);
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [sendErr, setSendErr] = useState<string | null>(null);
  const bottom = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  // Newest message in view, as a chat should open - not scrolled to a conversation's beginning.
  useEffect(() => {
    bottom.current?.scrollIntoView({ block: "end" });
  }, [detail.data?.messages.length]);

  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    try {
      await fn();
      await detail.reload();
      await onChanged();
    } finally {
      setBusy(false);
    }
  };

  const send = async () => {
    const text = draft.trim();
    if (!text || sending) return;
    setSending(true);
    setSendErr(null);
    try {
      await api.sessionReply(phone, text);
      setDraft("");
      await detail.reload();
    } catch (e) {
      setSendErr((e as Error).message);
    } finally {
      setSending(false);
    }
  };

  const session = detail.data?.session ?? null;
  const facts = Object.entries(session ?? {})
    .filter(([k, v]) => !["phone", "customer_name"].includes(k) && v !== null && v !== "");

  return (
    <div className="fixed inset-0 z-50 bg-black/40 flex items-center justify-center p-4 md:p-8" onClick={onClose}>
      <div role="dialog" aria-modal="true" aria-label={`Chat with ${phone}`}
        className="bg-white rounded-xl shadow-lg w-full max-w-5xl h-[86vh] flex flex-col overflow-hidden"
        onClick={(e) => e.stopPropagation()}>
        <div className="flex flex-wrap items-center gap-2 border-b border-slate-200 px-4 py-2.5">
          <div className="min-w-0">
            <div className="font-medium truncate">
              {detail.data?.customer?.name || <span className="text-rose-600">not in your customer list</span>}
            </div>
            <div className="font-mono text-xs text-slate-500">{phone}</div>
          </div>
          {session && <Badge tone={STEP_TONE[session.step]}>{session.step}</Badge>}
          {withPerson && <Badge tone="amber">a person has this chat</Badge>}
          <div className="ml-auto flex items-center gap-2">
            {withPerson ? (
              <button className="btn-ghost" disabled={busy} onClick={() => act(() => api.handBack(phone))}>
                Hand back to bot
              </button>
            ) : (
              <button className="btn-ghost" disabled={busy} title="The bot stays quiet for this number while your agent chats in WATI"
                onClick={() => act(() => api.handToPerson(phone))}>
                Hand to a person
              </button>
            )}
            <button className="btn-ghost" disabled={busy} title="Forget where they are; their next message starts a new conversation"
              onClick={() => act(() => api.resetSession(phone))}>
              Reset
            </button>
            <button className="btn-ghost !h-8 !w-8 !px-0" onClick={onClose} aria-label="Close the chat">
              <X className="w-4 h-4" />
            </button>
          </div>
        </div>

        <div className="flex-1 min-h-0 flex">
          <div className="flex-1 min-w-0 overflow-auto bg-[#efeae2] px-4 py-3 space-y-2">
            {!detail.data ? (
              <div className="text-sm text-slate-500">Loading…</div>
            ) : detail.data.messages.length === 0 ? (
              <Empty>No messages with this number yet.</Empty>
            ) : (
              detail.data.messages.map((m) => (
                <div key={m.id} className={`max-w-[75%] rounded-lg px-3 py-2 text-sm shadow-sm ${
                  m.direction === "in" ? "bg-white" : "bg-[#d9fdd3] ml-auto"}`}>
                  <div className="whitespace-pre-wrap break-words">{m.text}</div>
                  {m.transcript && <div className="mt-1 text-xs text-slate-500">🎤 {m.transcript}</div>}
                  {m.options && <MenuPreview options={m.options} compact />}
                  <div className="mt-1 flex items-center gap-2 text-[10px] text-slate-400">
                    {fmt(m.created_at)}
                    {m.outcome && <Badge tone={OUTCOME_TONE[m.outcome]}>{m.outcome}</Badge>}
                  </div>
                </div>
              ))
            )}
            <div ref={bottom} />
          </div>

          <aside className="hidden md:block w-64 shrink-0 overflow-auto border-l border-slate-200 p-3 space-y-2">
            <div className="text-xs font-medium text-slate-600">Where they are</div>
            {facts.length === 0 ? (
              <div className="text-xs text-slate-500">
                No conversation open — their next message starts a new one.
              </div>
            ) : (
              <div className="space-y-1.5">
                {facts.map(([k, v]) => (
                  <div key={k} className="rounded bg-slate-50 p-1.5 text-xs">
                    <div className="text-slate-400">{k}</div>
                    <div className="font-mono break-all">{String(v)}</div>
                  </div>
                ))}
              </div>
            )}
          </aside>
        </div>

        {sendErr && (
          <div className="border-t border-rose-200 bg-rose-50 px-4 py-1.5 text-[11px] text-rose-800">{sendErr}</div>
        )}
        {withPerson ? (
          <div className="border-t border-slate-200 p-3 space-y-1">
            <form className="flex items-end gap-2" onSubmit={(e) => { e.preventDefault(); void send(); }}>
              <textarea className="input flex-1 resize-none" rows={2} value={draft} disabled={sending}
                placeholder="Write your reply…" aria-label="Reply to this customer"
                onChange={(e) => setDraft(e.target.value)}
                onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); void send(); } }} />
              <button className="btn-primary" disabled={sending || !draft.trim()}>{sending ? "Sending…" : "Send"}</button>
            </form>
            <div className="text-[10px] text-slate-400">
              Goes to the customer on WhatsApp from your business number. Enter sends, Shift+Enter starts a new line.
              WhatsApp only allows a plain reply within 24 hours of their last message.
            </div>
          </div>
        ) : (
          <div className="border-t border-slate-200 px-4 py-2 text-[11px] text-slate-500">
            The bot is answering this chat, so there is nothing to type: it would talk over you. Click
            <b> Hand to a person</b> above to quieten it and reply from here. This refreshes every 5 seconds.
          </div>
        )}
      </div>
    </div>
  );
}

/** Customers a live workflow is talking to right now; the order-status bot's own sessions are below. */
function WorkflowRuns() {
  const { data, reload } = usePoll(() => api.workflowRuns(), 5000);
  if (!data || data.length === 0) return null;
  const end = async (phone: string) => {
    if (!confirm(`Let ${phone} out of the workflow? Their next message is answered as if they had just written in.`)) return;
    await api.workflowEndRun(phone);
    await reload();
  };
  return (
    <div className="card p-0 overflow-auto">
      <div className="px-4 pt-3 pb-1 font-medium text-sm">Inside a workflow now</div>
      <table className="w-full">
        <thead>
          <tr><th className="th">Phone</th><th className="th">Workflow</th><th className="th">At the step</th><th className="th">Started by</th><th className="th">Updated</th><th className="th"></th></tr>
        </thead>
        <tbody>
          {data.map((r) => (
            <tr key={r.phone}>
              <td className="td font-mono">{r.phone}</td>
              <td className="td">
                <Link className="text-brand-700 hover:underline" to={`/workflows/${r.workflow}`}>{r.title}</Link>
                <span className="text-xs text-slate-400 ml-1">v{r.version}</span>
              </td>
              <td className="td">{r.step || "—"}{r.status !== "active" && <span className="text-xs text-amber-700 ml-1">(in a Wait step)</span>}</td>
              <td className="td text-xs text-slate-600">{r.trigger || "—"}</td>
              <td className="td text-xs text-slate-500" title={fmt(r.updated_at)}>{ago(r.updated_at)}</td>
              <td className="td text-right"><button className="btn-ghost text-xs" onClick={() => end(r.phone)}>End</button></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** Chats a person has taken: the bot says nothing to these numbers until they are handed back. */
function WithAPerson({ people, onChange }: { people: HandoverInfo[]; onChange: () => Promise<void> }) {
  if (people.length === 0) return null;
  return (
    <div className="card p-0 overflow-auto">
      <div className="px-4 pt-3 pb-1">
        <div className="font-medium text-sm">With a person — the bot is quiet</div>
        <div className="text-xs text-slate-500">
          WATI does not tell the bot when an agent solves a chat, so hand it back here when you are done - or it comes
          back by itself after the silence set in Settings.
        </div>
      </div>
      <table className="w-full">
        <thead>
          <tr><th className="th">Phone</th><th className="th">Customer</th><th className="th">With</th><th className="th">Since</th><th className="th">Bot back by itself</th><th className="th"></th></tr>
        </thead>
        <tbody>
          {people.map((p) => (
            <tr key={p.phone}>
              <td className="td font-mono">{p.phone}</td>
              <td className="td">{p.customer_name || <span className="text-slate-400">—</span>}</td>
              <td className="td text-xs">{p.assignee || "—"}<div className="text-slate-400">{p.source}</div></td>
              <td className="td text-xs text-slate-500" title={fmt(p.started_at)}>{ago(p.started_at)}</td>
              <td className="td text-xs text-slate-500">{p.returns_at ? fmt(p.returns_at) : "only by hand"}</td>
              <td className="td text-right">
                <button className="btn-ghost text-xs" onClick={async () => { await api.handBack(p.phone); await onChange(); }}>
                  Hand back to bot
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
