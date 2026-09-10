import { useEffect, useRef, useState } from "react";
import { api, CustomerRow, MenuOptions, Msg, Selection, SessionRow } from "../api";
import MenuPreview from "../MenuPreview";
import { Badge, ErrorBox, OUTCOME_TONE, STEP_TONE, usePoll } from "../ui";

interface Bubble { dir: "in" | "out"; text: string; meta?: string; outcome?: string | null; options?: MenuOptions | null; tapped?: boolean }

const QUICK = ["hi", "English", "हिंदी", "ગુજરાતી", "order status", "menu", "change language", "contact us", "yes", "no", "45231", "SO 45240", "FG-2002", "PO PO-7777", "done", "garbage text"];

export default function Simulator() {
  const customers = usePoll(() => api.customers(), 0);
  // Whether a message really leaves this server depends on the WATI token, NOT on dev/prod - so ask.
  const overview = usePoll(() => api.overview(), 0);
  const live = overview.data ? !overview.data.health.wati_mocked : null;
  const [acknowledged, setAcknowledged] = useState<string | null>(null);
  const [phone, setPhone] = useState("");
  const [text, setText] = useState("");
  const [log, setLog] = useState<Bubble[]>([]);
  const [session, setSession] = useState<SessionRow | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const bottom = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!phone && customers.data && customers.data.length) setPhone(customers.data[0].phone);
  }, [customers.data, phone]);
  useEffect(() => { bottom.current?.scrollIntoView({ behavior: "smooth" }); }, [log]);

  const send = async (t: string, type: "text" | "audio" = "text", selection?: Selection) => {
    if (!phone) return;
    if (live && acknowledged !== phone) {
      const who = cust ? `${cust.name} (${phone})` : phone;
      if (!confirm(`WATI is live. This will send a real WhatsApp message to ${who}.

Continue?`)) return;
      setAcknowledged(phone);
    }
    setBusy(true);
    setErr(null);
    setLog((l) => [...l, { dir: "in", text: type === "audio" ? "🎤 (voice note)" : t, tapped: !!selection }]);
    try {
      const r = await api.simulate(phone, t, type, selection);
      if (r.queued.status !== "queued") {
        setLog((l) => [...l, { dir: "out", text: `(${r.queued.status}: ${r.queued.reason || ""})` }]);
      } else if (r.reply) {
        // one customer message can produce several bot messages (greeting, then the language question)
        const all: Msg[] = r.replies && r.replies.length ? r.replies : [r.reply];
        const meta = r.inbound?.transcript ? `transcript: "${r.inbound.transcript}"` : undefined;
        setLog((l) => [...l, ...all.map((m, i) => ({ dir: "out" as const, text: m.text || "", outcome: m.outcome, options: m.options, meta: i === 0 ? meta : undefined }))]);
      } else {
        setLog((l) => [...l, { dir: "out", text: `(no reply — queue ${r.queue_status}${r.queue_error ? `: ${r.queue_error}` : ""})` }]);
      }
      setSession(r.session);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
      setText("");
    }
  };

  const cust: CustomerRow | undefined = customers.data?.find((c) => c.phone === phone);
  const lastIdx = log.length - 1;

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-xl font-semibold">Conversation simulator</h1>
        <p className="text-sm text-slate-500">
          Sends a WATI-shaped webhook through the real pipeline (token → dedup → queue → processor). Tap the blue
          options exactly like a customer would on WhatsApp, or type.
        </p>
      </div>
      {live === false && (
        <div className="rounded-lg border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-900">
          WATI has no token, so nothing leaves this server — every reply below is simulated.
        </div>
      )}
      {live === true && (
        <div className="rounded-lg border-2 border-rose-400 bg-rose-50 px-3 py-2 text-sm text-rose-900">
          <b>WATI is LIVE.</b> Every reply below is really delivered on WhatsApp to the number selected on the left.
          Use your own number, not a customer's.
        </div>
      )}

      <ErrorBox msg={err} />
      <div className="grid lg:grid-cols-3 gap-4">
        <div className="card space-y-3">
          <label className="text-sm font-medium">Customer (waId)</label>
          <select className="input w-full" value={phone} onChange={(e) => { setPhone(e.target.value); setLog([]); setSession(null); }}>
            {customers.data?.map((c) => <option key={c.phone} value={c.phone}>{c.phone} — {c.name}</option>)}
            <option value="910000000000">910000000000 — (unknown number)</option>
          </select>
          <input className="input w-full font-mono" value={phone} onChange={(e) => setPhone(e.target.value)} placeholder="or type any 91XXXXXXXXXX" />
          {cust && (
            <div className="text-xs text-slate-600 bg-slate-50 rounded p-2">
              <div><b>Excel name:</b> <span className="font-mono">"{cust.name}"</span></div>
              <div><b>SO numbers that match byte-exactly:</b> {cust.so_numbers.length ? cust.so_numbers.join(", ") : "none"}</div>
            </div>
          )}
          <div className="text-sm font-medium">Session</div>
          {session ? (
            <div className="text-xs grid grid-cols-2 gap-1">
              <div>step</div><div><Badge tone={STEP_TONE[session.step]}>{session.step}</Badge></div>
              <div>so_no</div><div className="font-mono">{session.so_no || "—"}</div>
              <div>fg_code</div><div className="font-mono">{session.fg_code || "—"}</div>
              <div>pending</div><div className="font-mono">{session.pending_kind ? `${session.pending_kind}=${session.pending_value}` : "—"}</div>
              <div>attempts</div><div>{session.attempts}</div>
              <div>language</div><div>{session.language}{session.lang_chosen ? " (chosen)" : ""}</div>
            </div>
          ) : <div className="text-xs text-slate-500">no session yet</div>}
          <button className="btn-ghost w-full justify-center" onClick={async () => { if (phone) { await api.resetSession(phone); setSession(null); setLog([]); } }}>Reset session & clear</button>
          <div className="text-sm font-medium">Quick inputs (typed)</div>
          <div className="flex flex-wrap gap-1">
            {QUICK.map((q) => <button key={q} className="btn-ghost text-xs" disabled={busy} onClick={() => send(q)}>{q}</button>)}
            <button className="btn-ghost text-xs" disabled={busy} onClick={() => send("", "audio")}>🎤 voice note (fixture)</button>
          </div>
        </div>
        <div className="card lg:col-span-2 flex flex-col" style={{ minHeight: 520 }}>
          <div className="flex-1 overflow-auto space-y-2 p-2 bg-[#efeae2] rounded-lg">
            {log.length === 0 && <div className="text-center text-sm text-slate-500 pt-24">Say "hi" to start — the bot greets, asks for the language, then shows the main menu.</div>}
            {log.map((b, i) => (
              <div key={i} className={`max-w-[80%] rounded-lg px-3 py-2 text-sm shadow-sm ${b.dir === "in" ? "bg-[#d9fdd3] ml-auto" : "bg-white"}`}>
                <div className="whitespace-pre-wrap break-words">{b.tapped && <span className="text-slate-400 mr-1">👆</span>}{b.text}</div>
                {b.options && <MenuPreview options={b.options} onPick={(s) => send(s.title, "text", s)} disabled={busy || i !== lastIdx} />}
                {(b.meta || b.outcome) && (
                  <div className="text-[10px] text-slate-500 mt-1 flex gap-2 items-center">
                    {b.meta}
                    {b.outcome && <Badge tone={OUTCOME_TONE[b.outcome]}>{b.outcome}</Badge>}
                    {b.options && <span>{b.options.kind === "list" ? `list · ${b.options.items.length} rows` : `${b.options.items.length} buttons`}</span>}
                  </div>
                )}
              </div>
            ))}
            <div ref={bottom} />
          </div>
          <form className="flex gap-2 mt-3" onSubmit={(e) => { e.preventDefault(); if (text.trim()) send(text.trim()); }}>
            <input className="input flex-1" placeholder="Type a message as the customer…" value={text} onChange={(e) => setText(e.target.value)} disabled={busy} autoFocus />
            <button className="btn-primary" disabled={busy || !text.trim()}>{busy ? "…" : "Send"}</button>
          </form>
        </div>
      </div>
    </div>
  );
}
