import { useState } from "react";
import { Check, CheckStatus, Readiness, api } from "../api";
import { ErrorBox, ago, usePoll } from "../ui";

const TONE: Record<CheckStatus, { dot: string; card: string; label: string }> = {
  pass: { dot: "bg-emerald-500", card: "border-slate-200", label: "OK" },
  warn: { dot: "bg-amber-500", card: "border-amber-200 bg-amber-50/40", label: "Check" },
  fail: { dot: "bg-rose-500", card: "border-rose-300 bg-rose-50/50", label: "Fix" },
};

/** Everything that must be true before real customers can message the bot. */
export default function GoLive() {
  const { data, error, loading, reload } = usePoll<Readiness>(() => api.readiness(true), 0);
  const [busy, setBusy] = useState(false);

  const run = async () => {
    setBusy(true);
    try { await reload(); } finally { setBusy(false); }
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
        <button className="btn-primary ml-auto" disabled={busy || loading} onClick={run}>
          {busy || loading ? "Checking…" : "Run the checks again"}
        </button>
      </div>

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

          <div className="card text-sm space-y-1">
            <div className="font-medium">After every line above is green</div>
            <ol className="list-decimal ml-5 space-y-1 text-slate-600">
              <li>In WATI, open <b>Settings → Webhooks</b> and add the webhook address shown above for the event <b>Message received</b>.</li>
              <li>In WATI, switch <b>off</b> every chatbot, auto-reply and keyword action — otherwise WATI answers before this bot sees the message.</li>
              <li>Send "hi" to your WhatsApp business number from a phone that is in the customer list, and finish one order lookup.</li>
              <li>Watch it arrive under <b>Chat log</b>.</li>
            </ol>
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
