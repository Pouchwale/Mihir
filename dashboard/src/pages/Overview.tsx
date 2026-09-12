import { useState } from "react";
import { Bar, BarChart, CartesianGrid, Cell, Legend, Line, LineChart, Pie, PieChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { api } from "../api";
import { Badge, ErrorBox, Stat, ago, fmt, usePoll } from "../ui";

const OUTCOME_COLORS: Record<string, string> = {
  status_delivered: "#059669", welcome: "#94a3b8", ask_language: "#64748b", menu: "#2563eb", contact: "#0891b2", ask_so: "#3b82f6", ask_fg: "#7c3aed", confirm: "#d97706",
  not_found: "#f59e0b", verify_failed: "#e11d48", mismatch: "#be123c", service_down: "#991b1b", rate_limited: "#6b7280", bye: "#a3a3a3", custom: "#0ea5e9",
};

export default function Overview() {
  const { data, error, reload } = usePoll(() => api.overview(), 5000);
  const [busy, setBusy] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);

  const run = async (label: string, fn: () => Promise<{ ok: boolean; accepted: number; rejected: number; error: string | null }>) => {
    setBusy(label);
    setMsg(null);
    try {
      const r = await fn();
      setMsg(`${label}: ${r.ok ? "OK" : "FAILED"} — accepted ${r.accepted}, rejected ${r.rejected}${r.error ? ` — ${r.error}` : ""}`);
      reload();
    } catch (e) {
      setMsg(`${label}: ${(e as Error).message}`);
    } finally {
      setBusy(null);
    }
  };

  if (error) return <ErrorBox msg={error} />;
  if (!data) return <div className="text-slate-500">Loading…</div>;
  const h = data.health;
  const c = data.counts;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <h1 className="text-xl font-semibold">Overview</h1>
        <div className="ml-auto flex gap-2">
          <button className="btn-ghost" disabled={!!busy} onClick={() => run("Import customers", () => api.importCustomers())}>{busy === "Import customers" ? "Importing…" : "Import customers now"}</button>
          <button className="btn-primary" disabled={!!busy} onClick={() => run("Refresh orders", () => api.refreshOrders())}>{busy === "Refresh orders" ? "Refreshing…" : "Refresh orders now"}</button>
        </div>
      </div>
      {msg && <div className="rounded-lg bg-slate-100 text-sm px-3 py-2">{msg}</div>}

      <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-7 gap-3">
        <Stat label="API / DB" value={h.db ? "Up" : "Down"} tone={h.db ? "green" : "red"} sub={`mode ${h.mode}`} />
        <Stat label="Customers" value={c.customers} sub={`sync ${ago(h.last_customer_sync)}`} />
        <Stat label="Orders cached" value={c.orders} sub={`${c.distinct_so} SO · ${ago(h.orders_fetched_at)}`} tone={h.orders_stale ? "amber" : undefined} />
        <Stat label="Active sessions" value={c.active_sessions} sub="within timeout window" />
        <Stat label="Name mismatches" value={c.mismatches} tone={c.mismatches ? "amber" : undefined} />
        <Stat label="Queue" value={c.queued} sub={c.failed_queue ? `${c.failed_queue} failed` : "no failures"} tone={c.failed_queue ? "red" : undefined} />
        <Stat label="Orders cache" value={h.orders_stale ? "STALE" : "Fresh"} tone={h.orders_stale ? "red" : "green"} sub={`refresh every ${data.config.order_refresh_minutes} min`} />
      </div>

      <div className="grid lg:grid-cols-3 gap-4">
        <div className="card lg:col-span-2">
          <div className="font-medium mb-2">Messages per day (last 14 days)</div>
          <div className="h-64">
            <ResponsiveContainer>
              <LineChart data={data.messages_per_day}>
                <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
                <XAxis dataKey="date" tick={{ fontSize: 11 }} />
                <YAxis allowDecimals={false} tick={{ fontSize: 11 }} />
                <Tooltip />
                <Legend />
                <Line type="monotone" dataKey="in" name="Inbound" stroke="#2563eb" strokeWidth={2} dot={false} />
                <Line type="monotone" dataKey="out" name="Outbound" stroke="#059669" strokeWidth={2} dot={false} />
              </LineChart>
            </ResponsiveContainer>
          </div>
        </div>
        <div className="card">
          <div className="font-medium mb-2">Reply outcomes (14 days)</div>
          <div className="h-64">
            {data.outcomes.length === 0 ? (
              <div className="text-sm text-slate-500 pt-20 text-center">No replies yet — try Test chat.</div>
            ) : (
              <ResponsiveContainer>
                <PieChart>
                  <Pie data={data.outcomes} dataKey="count" nameKey="outcome" innerRadius={50} outerRadius={85} paddingAngle={2}>
                    {data.outcomes.map((o) => (
                      <Cell key={o.outcome} fill={OUTCOME_COLORS[o.outcome] || "#94a3b8"} />
                    ))}
                  </Pie>
                  <Tooltip />
                  <Legend wrapperStyle={{ fontSize: 11 }} />
                </PieChart>
              </ResponsiveContainer>
            )}
          </div>
        </div>
      </div>

      <div className="grid lg:grid-cols-3 gap-4">
        <div className="card">
          <div className="font-medium mb-2">Outcome counts</div>
          <div className="h-48">
            <ResponsiveContainer>
              <BarChart data={data.outcomes} layout="vertical" margin={{ left: 40 }}>
                <XAxis type="number" allowDecimals={false} tick={{ fontSize: 11 }} />
                <YAxis type="category" dataKey="outcome" width={90} tick={{ fontSize: 11 }} />
                <Tooltip />
                <Bar dataKey="count">
                  {data.outcomes.map((o) => (
                    <Cell key={o.outcome} fill={OUTCOME_COLORS[o.outcome] || "#94a3b8"} />
                  ))}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          </div>
        </div>
        <div className="card">
          <div className="font-medium mb-2">Last sync runs</div>
          {(["customers", "orders"] as const).map((k) => {
            const r = data.last_runs[k];
            return (
              <div key={k} className="py-2 border-b border-slate-100 last:border-0 text-sm">
                <div className="flex items-center gap-2">
                  <span className="capitalize font-medium">{k}</span>
                  {r ? <Badge tone={r.ok ? "green" : "red"}>{r.ok ? "ok" : "failed"}</Badge> : <Badge>never</Badge>}
                  <span className="ml-auto text-slate-500 text-xs">{r ? fmt(r.finished_at) : ""}</span>
                </div>
                {r && <div className="text-xs text-slate-500 mt-1">{r.source} · {r.accepted} accepted, {r.rejected} rejected{r.error ? ` · ${r.error}` : ""}</div>}
              </div>
            );
          })}
          <div className="font-medium mt-4 mb-1">Scheduled jobs</div>
          {data.jobs.map((j) => (
            <div key={j.id} className="text-xs text-slate-600 flex gap-2 py-0.5">
              <span className="font-mono">{j.id}</span>
              <span className="ml-auto">{j.next_run ? `next ${ago(j.next_run).replace(" ago", "")}` : "—"}</span>
            </div>
          ))}
        </div>
        <div className="card">
          <div className="font-medium mb-2">Recent alerts</div>
          {data.alerts.length === 0 ? (
            <div className="text-sm text-slate-500">None since start.</div>
          ) : (
            <div className="space-y-2 max-h-72 overflow-auto">
              {[...data.alerts].reverse().map((a, i) => (
                <div key={i} className="text-xs border-l-2 pl-2" style={{ borderColor: a.level === "error" ? "#e11d48" : "#f59e0b" }}>
                  <div className="font-medium">{a.title}</div>
                  <div className="text-slate-500 whitespace-pre-wrap break-words">{a.detail.slice(0, 300)}</div>
                  <div className="text-slate-400">{fmt(a.at)}</div>
                </div>
              ))}
            </div>
          )}
          <div className="font-medium mt-4 mb-1">Integrations</div>
          <div className="flex flex-wrap gap-1">
            <Badge tone={data.config.wati_mocked ? "amber" : "green"}>WATI {data.config.wati_mocked ? "mocked" : "live"}</Badge>
            <Badge tone={data.config.openai ? (data.config.openai_paused ? "amber" : "green") : "slate"}>
              OpenAI {data.config.openai ? (data.config.openai_paused ? "failing — using regex" : "on") : "regex fallback"}
            </Badge>
            <Badge tone={data.config.groq ? "green" : "slate"}>Groq STT {data.config.groq ? "on" : "fixture"}</Badge>
            <Badge tone={data.config.dropbox ? "green" : "slate"}>Dropbox {data.config.dropbox ? "on" : "local file"}</Badge>
            <Badge tone="blue">orders: {String(data.config.orders_source)}</Badge>
          </div>
        </div>
      </div>
    </div>
  );
}
