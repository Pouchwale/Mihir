import { useState } from "react";
import { api } from "../api";
import MenuPreview from "../MenuPreview";
import { Badge, Empty, ErrorBox, OUTCOME_TONE, fmt, usePoll } from "../ui";

export default function Messages() {
  const [phone, setPhone] = useState("");
  const [q, setQ] = useState("");
  const [direction, setDirection] = useState("");
  const [outcome, setOutcome] = useState("");
  const [page, setPage] = useState(1);
  const { data, error } = usePoll(() => api.messages({ phone, q, direction, outcome, page, page_size: 50 }), 5000, [phone, q, direction, outcome, page]);
  const pages = data ? Math.max(1, Math.ceil(data.total / data.page_size)) : 1;

  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold">Chat log</h1>
      <div className="flex flex-wrap gap-2">
        <input className="input" placeholder="Phone" value={phone} onChange={(e) => { setPhone(e.target.value); setPage(1); }} />
        <input className="input flex-1 min-w-[200px]" placeholder="Search text / transcript" value={q} onChange={(e) => { setQ(e.target.value); setPage(1); }} />
        <select className="input" value={direction} onChange={(e) => { setDirection(e.target.value); setPage(1); }}>
          <option value="">In + out</option><option value="in">Inbound</option><option value="out">Outbound</option>
        </select>
        <select className="input" value={outcome} onChange={(e) => { setOutcome(e.target.value); setPage(1); }}>
          <option value="">Any outcome</option>
          {Object.keys(OUTCOME_TONE).map((o) => <option key={o} value={o}>{o}</option>)}
        </select>
      </div>
      <ErrorBox msg={error} />
      <div className="card p-0 overflow-auto">
        {!data || data.items.length === 0 ? (
          <Empty>No messages.</Empty>
        ) : (
          <table className="w-full">
            <thead><tr><th className="th">Time</th><th className="th">Phone</th><th className="th">Dir</th><th className="th">Type</th><th className="th">Text</th><th className="th">Outcome</th><th className="th">Step</th></tr></thead>
            <tbody>
              {data.items.map((m) => (
                <tr key={m.id} className="hover:bg-slate-50">
                  <td className="td text-xs text-slate-500 whitespace-nowrap">{fmt(m.created_at)}</td>
                  <td className="td font-mono text-xs">{m.phone}</td>
                  <td className="td"><Badge tone={m.direction === "in" ? "blue" : "green"}>{m.direction}</Badge></td>
                  <td className="td text-xs">{m.type}</td>
                  <td className="td max-w-xl"><div className="whitespace-pre-wrap break-words">{m.text}</div>{m.transcript && <div className="text-xs text-slate-500">🎤 {m.transcript}</div>}{m.options && <MenuPreview options={m.options} compact />}</td>
                  <td className="td">{m.outcome && <Badge tone={OUTCOME_TONE[m.outcome]}>{m.outcome}</Badge>}</td>
                  <td className="td text-xs">{m.step_after || ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
      <div className="flex items-center gap-2 text-sm">
        <button className="btn-ghost" disabled={page <= 1} onClick={() => setPage(page - 1)}>Prev</button>
        <span>Page {page} / {pages} · {data?.total ?? 0} messages</span>
        <button className="btn-ghost" disabled={page >= pages} onClick={() => setPage(page + 1)}>Next</button>
      </div>
    </div>
  );
}
