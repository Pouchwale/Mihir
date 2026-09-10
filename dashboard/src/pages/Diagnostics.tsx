import { Diagnostics as Diag, api } from "../api";
import { Badge, ErrorBox, Empty, fmt, usePoll } from "../ui";

/** Is the problem on WATI's side or ours? Both directions, on one screen. */
export default function Diagnostics() {
  const { data, error, loading, reload } = usePoll<Diag>(() => api.diagnostics(), 10000);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-start gap-3">
        <div>
          <h1 className="text-xl font-semibold">Diagnostics</h1>
          <p className="text-sm text-slate-500">
            Everything WATI sent us and everything we sent WATI, including what was refused and why.
            If a customer's message never arrives, the answer is on this page.
          </p>
        </div>
        <button className="btn-ghost ml-auto" disabled={loading} onClick={reload}>{loading ? "Loading…" : "Refresh"}</button>
      </div>

      <ErrorBox msg={error} />

      {data && (
        <>
          <div className="card border-2 border-slate-300">
            <div className="font-medium">What this looks like right now</div>
            <div className="text-sm text-slate-700 mt-1">{data.verdict}</div>
            {data.wati_mocked && (
              <div className="text-xs text-amber-700 mt-1">
                WhatsApp is simulated (no WATI token), so nothing below actually left this server.
              </div>
            )}
          </div>

          <div className="card">
            <div className="font-medium mb-2">Incoming — what WATI sent us</div>
            {data.inbound.length === 0 ? (
              <Empty>WATI has never called this server. Nothing a customer sends can reach the bot yet.</Empty>
            ) : (
              <div className="overflow-auto">
                <table className="w-full text-sm">
                  <thead><tr><th className="th">Time</th><th className="th">From</th><th className="th">Result</th><th className="th">Customer</th><th className="th">Why</th></tr></thead>
                  <tbody>
                    {data.inbound.map((r, i) => (
                      <tr key={i}>
                        <td className="td whitespace-nowrap">{fmt(r.at)}</td>
                        <td className="td font-mono text-xs">{r.client_ip || "—"}</td>
                        <td className="td"><Badge tone={r.status === 200 ? (r.outcome === "queued" ? "green" : "slate") : "red"}>{r.status} {r.outcome}</Badge></td>
                        <td className="td font-mono text-xs">{r.phone || "—"}</td>
                        <td className="td text-xs">{r.reason || (r.event_type ? `event: ${r.event_type}` : "—")}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>

          <div className="card">
            <div className="font-medium mb-2">Outgoing — what we sent WATI</div>
            {data.outbound.length === 0 ? <Empty>Nothing sent yet.</Empty> : (
              <div className="overflow-auto">
                <table className="w-full text-sm">
                  <thead><tr><th className="th">Time</th><th className="th">To</th><th className="th">Type</th><th className="th">Result</th><th className="th">Message / error</th></tr></thead>
                  <tbody>
                    {data.outbound.map((r, i) => (
                      <tr key={i}>
                        <td className="td whitespace-nowrap">{fmt(r.at)}</td>
                        <td className="td font-mono text-xs">{r.phone}</td>
                        <td className="td">{r.kind}</td>
                        <td className="td">
                          <Badge tone={r.simulated ? "amber" : r.sent ? "green" : "red"}>
                            {r.simulated ? "simulated" : r.sent ? "sent" : "FAILED"}
                          </Badge>
                        </td>
                        <td className="td text-xs break-words">{r.error || r.text}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>

          {data.failed_queue.length > 0 && (
            <div className="card">
              <div className="font-medium mb-2">Messages that could not be handled</div>
              <div className="space-y-1 text-sm">
                {data.failed_queue.map((f, i) => (
                  <div key={i} className="flex gap-2"><span className="text-slate-400 whitespace-nowrap">{fmt(f.at)}</span>
                    <Badge tone="red">{f.status}</Badge><span className="font-mono text-xs">{f.phone}</span>
                    <span className="text-xs break-words">{f.error}</span></div>
                ))}
              </div>
            </div>
          )}

          {data.alerts.length > 0 && (
            <div className="card">
              <div className="font-medium mb-2">Recent alerts</div>
              <div className="space-y-1 text-sm">
                {data.alerts.map((a, i) => (
                  <div key={i} className="flex gap-2"><span className="text-slate-400 whitespace-nowrap">{fmt(a.at)}</span>
                    <Badge tone={a.level === "error" ? "red" : "amber"}>{a.level}</Badge>
                    <span><b>{a.title}</b> — <span className="text-xs">{a.detail}</span></span></div>
                ))}
              </div>
            </div>
          )}
        </>
      )}
    </div>
  );
}
