import { useState } from "react";
import { Check, CheckStatus, Readiness, api } from "../api";
import { ErrorBox, ago, usePoll } from "../ui";

/** What to tick in WATI -> Settings -> Webhooks, and why. Event names and eventType values are
 *  WATI's own (docs.wati.io/reference, "Webhook Events"), not this project's invention. */
const WATI_EVENTS: { event: string; type: string; on: boolean; why: string }[] = [
  { event: "Message received", type: "message", on: true,
    why: "Every message a customer sends, including taps on the bot's buttons and list rows. Without this the bot never hears anyone." },
  { event: "New contact message received", type: "newContactMessageReceived", on: true,
    why: "The first message from a number WATI has never seen. A first-time customer can arrive as this event alone, which is exactly the greeting the bot exists for. If WATI sends both, the second is discarded by message id." },
  { event: "Session message sent", type: "sessionMessageSent", on: false,
    why: "The bot's own outgoing replies coming back to it. Nothing to do with them." },
  { event: "Template message sent", type: "templateMessageSent", on: false,
    why: "Outgoing template messages. This bot replies inside the 24-hour session window and sends no templates." },
  { event: "Sent message delivered / read / replied", type: "sentMessageDELIVERED …", on: false,
    why: "Delivery receipts for messages already sent. Useful for analytics, not for answering a customer." },
  { event: "CTA URL clicked", type: "ctaUrlClicked", on: false,
    why: "Only fires for link buttons inside template messages. The bot's buttons are quick replies, which arrive in 'message' instead." },
];

const TONE: Record<CheckStatus, { dot: string; card: string; label: string }> = {
  pass: { dot: "bg-emerald-500", card: "border-slate-200", label: "OK" },
  warn: { dot: "bg-amber-500", card: "border-amber-200 bg-amber-50/40", label: "Check" },
  fail: { dot: "bg-rose-500", card: "border-rose-300 bg-rose-50/50", label: "Fix" },
};

/** Everything that must be true before real customers can message the bot. */
export default function GoLive() {
  const { data, error, loading, reload } = usePoll<Readiness>(() => api.readiness(true), 0);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);

  const run = async () => {
    setBusy(true);
    setMsg(null);
    try { await reload(); } finally { setBusy(false); }
  };

  const stale = data?.checks.find((c) => c.key === "env_fresh" && c.status !== "pass");

  const selfTest = async () => {
    setBusy(true);
    setMsg(null);
    try {
      const r = await api.webhookSelfTest();
      setMsg(`${r.ok ? "✅" : "❌"} ${r.detail}`);
    } catch (e) { setMsg((e as Error).message); } finally { setBusy(false); }
  };

  const groups: string[] = [];
  for (const c of data?.checks ?? []) if (!groups.includes(c.group)) groups.push(c.group);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-start gap-3">
        <div>
          <h1 className="text-xl font-semibold">Go live</h1>
          <p className="text-sm text-slate-500">
            One press checks everything a real customer needs: the WhatsApp connection, your order and customer data,
            the security settings and the server itself. Each problem below says exactly what to change and where.
          </p>
        </div>
        <div className="ml-auto flex flex-wrap items-center gap-2">
          <button className="btn-ghost" disabled={busy || loading} onClick={selfTest}
            title="Call our own webhook address exactly as WATI would">
            Test the webhook address
          </button>
          <button className="btn-primary" disabled={busy || loading} onClick={run}>
            {busy || loading ? "Checking…" : "Run the checks again"}
          </button>
        </div>
      </div>

      {msg && <div className="card text-sm bg-sky-50 border-sky-200">{msg}</div>}
      {stale && (
        <div className="card border-2 border-amber-300 bg-amber-50/60 text-sm">
          <b>backend/.env changed after the bot started.</b> The bot is still using the old values — that is why a new
          WATI token can look like it is being ignored. Restart the bot to pick them up (on a hosted service, redeploy).
        </div>
      )}

      <ErrorBox msg={error} />

      {data && (
        <>
          <div className={`card border-2 ${data.ready ? "border-emerald-300 bg-emerald-50/50" : "border-rose-300 bg-rose-50/50"}`}>
            <div className="flex flex-wrap items-center gap-3">
              <div className="text-3xl">{data.ready ? "✅" : "🚧"}</div>
              <div>
                <div className="text-lg font-semibold">
                  {data.ready ? "Ready to go live" : `${data.counts.fail} ${data.counts.fail === 1 ? "problem" : "problems"} to fix first`}
                </div>
                <div className="text-sm text-slate-600">
                  {data.counts.pass} passed · {data.counts.warn} worth checking · {data.counts.fail} must be fixed
                  {data.mode === "dev" && " · still in dev mode, so nothing is sent to WhatsApp"}
                </div>
              </div>
              <div className="ml-auto text-xs text-slate-500">checked {ago(data.checked_at)}</div>
            </div>
          </div>

          {groups.map((g) => (
            <div key={g} className="card space-y-2">
              <div className="font-medium">{g}</div>
              {data.checks.filter((c) => c.group === g).map((c) => <Row key={c.key} check={c} />)}
            </div>
          ))}

          <div className="card text-sm space-y-3">
            <div>
              <div className="font-medium">Which events to tick in WATI</div>
              <p className="text-slate-500">
                WATI → Settings → Webhooks, on the webhook address shown above. Tick both of these and nothing else.
                Ticking extra events is harmless — they are answered 200 and ignored — but they fill the log with
                traffic that is not a customer.
              </p>
            </div>
            <div className="overflow-x-auto">
              <table className="w-full text-left border-collapse">
                <thead className="text-xs uppercase tracking-wide text-slate-500">
                  <tr>
                    <th className="py-1 pr-3 font-medium">Event in WATI</th>
                    <th className="py-1 pr-3 font-medium">eventType</th>
                    <th className="py-1 font-medium">Why</th>
                  </tr>
                </thead>
                <tbody className="align-top">
                  {WATI_EVENTS.map((e) => (
                    <tr key={e.event} className="border-t border-black/5">
                      <td className="py-1.5 pr-3 whitespace-nowrap">
                        <span className={`mr-1.5 text-xs uppercase ${e.on ? "text-emerald-600" : "text-slate-400"}`}>
                          {e.on ? "tick" : "leave"}
                        </span>
                        {e.event}
                      </td>
                      <td className="py-1.5 pr-3 font-mono text-xs whitespace-nowrap">{e.type}</td>
                      <td className="py-1.5 text-slate-600">{e.why}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div>
              <div className="font-medium">Then</div>
              <ol className="list-decimal ml-5 space-y-1 text-slate-600">
                <li>In WATI, switch <b>off</b> every chatbot, auto-reply and keyword action — otherwise WATI answers before this bot sees the message.</li>
                <li>Send "hi" to your WhatsApp business number from a phone that is in the customer list, and finish one order lookup.</li>
                <li>Watch it arrive under <b>Chat log</b>.</li>
              </ol>
            </div>
          </div>
        </>
      )}
    </div>
  );
}

function Row({ check }: { check: Check }) {
  const tone = TONE[check.status];
  return (
    <div className={`rounded-lg border px-3 py-2 ${tone.card}`}>
      <div className="flex items-center gap-2 text-sm">
        <span className={`w-2 h-2 rounded-full shrink-0 ${tone.dot}`} />
        <span className="font-medium">{check.title}</span>
        <span className="text-slate-500 truncate">{check.detail}</span>
        {check.status !== "pass" && <span className="ml-auto text-xs uppercase tracking-wide text-slate-500 shrink-0">{tone.label}</span>}
      </div>
      {check.fix && (
        <div className="mt-1.5 text-xs text-slate-700 border-t border-black/5 pt-1.5">
          {check.where && <div className="text-slate-500 mb-0.5">Where: {check.where}</div>}
          <div className="whitespace-pre-wrap break-all font-mono leading-relaxed">{check.fix}</div>
        </div>
      )}
    </div>
  );
}
