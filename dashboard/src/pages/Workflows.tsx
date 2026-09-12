import { useEffect, useRef, useState, type ReactNode } from "react";
import { Link, useNavigate } from "react-router-dom";
import { ArrowDown, ArrowUp, Copy, Download, FileUp, Pencil, Sparkles, Trash2 } from "lucide-react";
import { api } from "../api";
import { ago, Empty, ErrorBox, usePoll } from "../ui";
import { downloadJson, readJsonFile } from "../workflow/files";
import { AI_EXAMPLES, stageText, waitForJob } from "../workflow/ai";
import {
  BUILTIN_ORDER_STATUS, type AiJob, type AiStatus, type RoutingOverview, type RoutingWorkflow, type WorkflowSummary,
} from "../workflow/types";

export default function Workflows() {
  const { data, error, loading, reload } = usePoll<WorkflowSummary[]>(() => api.workflows(), 0);
  const routing = usePoll<RoutingOverview>(() => api.workflowRouting(), 15000);
  const examples = usePoll(() => api.workflowExamples(), 0);
  const [title, setTitle] = useState("");
  const [from, setFrom] = useState("onboarding");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const nav = useNavigate();
  const [renaming, setRenaming] = useState<string | null>(null);
  const [newTitle, setNewTitle] = useState("");
  const renameCancelled = useRef(false);

  const refresh = async () => {
    await reload();
    await routing.reload();
  };

  const attempt = async (fn: () => Promise<unknown>) => {
    setErr(null);
    try {
      await fn();
    } catch (e) {
      setErr((e as Error).message);
    }
  };

  const create = async () => {
    if (!title.trim()) return;
    setBusy(true);
    await attempt(async () => {
      const w = await api.workflowCreate(title.trim(), from);
      nav(`/workflows/${w.key}`);
    });
    setBusy(false);
  };

  const importFile = async (file: File) => {
    setBusy(true);
    await attempt(async () => {
      const imported = await api.workflowImport(await readJsonFile(file));
      // the editor shows what had to change and what to check
      nav(`/workflows/${imported.key}`, { state: { report: imported.report } });
    });
    setBusy(false);
    if (fileInput.current) fileInput.current.value = "";
  };

  const toggleLive = (w: WorkflowSummary) => attempt(async () => {
    await api.workflowSetLive(w.key, !w.live);
    await refresh();
  });

  // Order decides which workflow answers when two could, so it is set by moving rows.
  const move = (i: number, by: -1 | 1) => attempt(async () => {
    if (!data) return;
    const keys = data.map((w) => w.key);
    const j = i + by;
    if (j < 0 || j >= keys.length) return;
    [keys[i], keys[j]] = [keys[j], keys[i]];
    await api.workflowReorder(keys);
    await refresh();
  });

  const saveRename = (key: string, old: string) => attempt(async () => {
    const t = newTitle.trim().slice(0, 120);
    setRenaming(null);
    if (renameCancelled.current || !t || t === old) { renameCancelled.current = false; return; }
    await api.workflowRename(key, t);
    await refresh();
  });

  const remove = (key: string, name: string) => attempt(async () => {
    if (!confirm(`Delete “${name}”? Every version of it goes too, and this cannot be undone.`)) return;
    await api.workflowDelete(key);
    await refresh();
  });

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-start gap-3">
        <div>
          <h1 className="text-xl font-semibold">Workflows</h1>
          <p className="text-sm text-slate-500">
            Conversations you draw, running next to the order-status bot. Publish one to try it on your test
            numbers, then switch it on for everyone.
          </p>
        </div>
        <div className="ml-auto">
          <input ref={fileInput} type="file" accept=".json,application/json" className="hidden"
            onChange={(e) => { const f = e.target.files?.[0]; if (f) void importFile(f); }} />
          <button className="btn-ghost" disabled={busy} onClick={() => fileInput.current?.click()}
            title="A chatbot exported from WATI, or a workflow exported from here">
            <FileUp className="w-4 h-4" /> Import JSON
          </button>
        </div>
      </div>

      <ErrorBox msg={error || err} />

      {routing.data && <RoutingCard r={routing.data} />}

      <div className="card space-y-2">
        <div className="font-medium text-sm">Start a new one</div>
        <div className="flex flex-wrap gap-2">
          <input className="input flex-1 min-w-48" placeholder="What is it for? e.g. New customer onboarding"
            value={title} onChange={(e) => setTitle(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && create()} />
          <select className="input" value={from} onChange={(e) => setFrom(e.target.value)}>
            <option value="">Empty canvas</option>
            {examples.data?.map((x) => <option key={x.key} value={x.key}>Start from: {x.title}</option>)}
          </select>
          <button className="btn-primary" disabled={busy || !title.trim()} onClick={create}>Create</button>
        </div>
        <div className="text-xs text-slate-500">
          Already built it in WATI? Export the chatbot from WATI as JSON and use <b>Import JSON</b> — every step,
          button and connection comes across as a draft, with a report of anything that had to change.
        </div>
      </div>

      <AiCreateCard onDone={(key, note) => nav(`/workflows/${key}`, { state: { aiNote: note } })} />

      {data && data.length === 0 && !loading && (
        <Empty>No workflows yet. Create one above, or import a chatbot exported from WATI.</Empty>
      )}

      {data && data.length > 0 && (
        <div className="card p-0 overflow-auto">
          <table className="w-full">
            <thead>
              <tr>
                <th className="th w-16" title="Higher answers first when two workflows could">Order</th>
                <th className="th">Workflow</th>
                <th className="th">Starts when</th>
                <th className="th" title="On: every customer. Off: only your test numbers (Settings).">Live for everyone</th>
                <th className="th">Version</th>
                <th className="th">Problems</th>
                <th className="th">Updated</th>
                <th className="th"></th>
              </tr>
            </thead>
            <tbody>
              {data.map((w, i) => (
                <tr key={w.key}>
                  <td className="td">
                    <div className="flex">
                      <button className="btn-ghost !h-7 !w-7 !px-0" disabled={i === 0} onClick={() => move(i, -1)}
                        aria-label={`Move ${w.title} up`}><ArrowUp className="w-3.5 h-3.5" /></button>
                      <button className="btn-ghost !h-7 !w-7 !px-0" disabled={i === data.length - 1} onClick={() => move(i, 1)}
                        aria-label={`Move ${w.title} down`}><ArrowDown className="w-3.5 h-3.5" /></button>
                    </div>
                  </td>
                  <td className="td">
                    {renaming === w.key ? (<div>
                      <input className="input !h-8 w-full min-w-[16rem] font-medium" autoFocus value={newTitle} maxLength={120}
                        onFocus={(e) => e.currentTarget.select()}
                        aria-label={`New name for ${w.title}`} onChange={(e) => setNewTitle(e.target.value)}
                        onBlur={() => void saveRename(w.key, w.title)}
                        onKeyDown={(e) => {
                          if (e.key === "Enter") (e.target as HTMLInputElement).blur();
                          if (e.key === "Escape") { renameCancelled.current = true; (e.target as HTMLInputElement).blur(); }
                        }} />
                      <div className="mt-0.5 text-[11px] text-slate-400">Enter to save · Esc to cancel</div>
                    </div>) : (
                      <Link className="font-medium text-brand-700 hover:underline" to={`/workflows/${w.key}`}>{w.title}</Link>
                    )}
                    {w.description && <div className="text-xs text-slate-500 line-clamp-1">{w.description}</div>}
                  </td>
                  <td className="td text-xs text-slate-600 max-w-64">
                    {w.starts?.length ? w.starts.join(" · ") : <span className="text-amber-700">Nothing yet — set it under Start</span>}
                  </td>
                  <td className="td">
                    {w.published_version ? (<span className="inline-flex items-center gap-2">
                      <button role="switch" aria-checked={!!w.live} onClick={() => toggleLive(w)}
                        aria-label={`${w.live ? "Switch off" : "Switch on"} ${w.title}`}
                        title={w.live ? "Answering every customer. Click to go back to test numbers only." : "Only your test numbers reach it. Click to switch it on for everyone."}
                        className={`relative inline-flex h-5 w-9 items-center rounded-full transition ${w.live ? "bg-emerald-500" : "bg-slate-300"}`}>
                        <span className={`inline-block h-4 w-4 rounded-full bg-white shadow transition ${w.live ? "translate-x-4" : "translate-x-0.5"}`} />
                      </button>
                      <span className="text-xs text-slate-500">{w.live ? "Everyone" : "Test numbers"}</span>
                    </span>) : (
                      <span className="text-xs text-slate-400" title="Publish it first">—</span>
                    )}
                  </td>
                  <td className="td">
                    {w.published_version ? (
                      <span className="badge bg-emerald-100 text-emerald-700">v{w.published_version}</span>
                    ) : (
                      <span className="badge bg-slate-100 text-slate-600">draft</span>
                    )}
                    {w.published_version && w.draft_version !== w.published_version && (
                      <div className="text-[11px] text-slate-500 mt-0.5">unpublished changes</div>
                    )}
                  </td>
                  <td className="td">
                    {w.issue_counts?.fail ? (
                      <span className="badge bg-rose-100 text-rose-700">{w.issue_counts.fail} to fix</span>
                    ) : w.issue_counts?.warn ? (
                      <span className="badge bg-amber-100 text-amber-800">{w.issue_counts.warn} to check</span>
                    ) : (
                      <span className="text-emerald-600 text-xs">ready</span>
                    )}
                  </td>
                  <td className="td text-slate-500">{w.updated_at ? ago(w.updated_at) : "—"}</td>
                  <td className="td">
                    <div className="flex justify-end gap-1">
                      <button className="btn-ghost !h-8 !w-8 !px-0" title="Rename" aria-label={`Rename ${w.title}`}
                        onClick={() => { renameCancelled.current = false; setRenaming(w.key); setNewTitle(w.title); }}>
                        <Pencil className="w-4 h-4" />
                      </button>
                      <button className="btn-ghost !h-8 !w-8 !px-0" title="Duplicate" aria-label={`Duplicate ${w.title}`}
                        onClick={() => attempt(async () => nav(`/workflows/${(await api.workflowDuplicate(w.key)).key}`))}>
                        <Copy className="w-4 h-4" />
                      </button>
                      <button className="btn-ghost !h-8 !w-8 !px-0" title="Export as a file" aria-label={`Export ${w.title}`}
                        onClick={() => attempt(async () => downloadJson(`${w.key}.json`, await api.workflowExport(w.key)))}>
                        <Download className="w-4 h-4" />
                      </button>
                      <button className="btn-ghost !h-8 !w-8 !px-0 text-rose-600" title="Delete" aria-label={`Delete ${w.title}`}
                        onClick={() => remove(w.key, w.title)}><Trash2 className="w-4 h-4" /></button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

/** The order every incoming message is checked in - the same order as backend workflow/runtime.py. */
function RoutingCard({ r }: { r: RoutingOverview }) {
  const byKey = new Map(r.workflows.map((w) => [w.key, w]));
  const inside = r.workflows.reduce((n, w) => n + w.active, 0);
  const starters = r.workflows.filter((w) => w.keywords.length || w.menu_label);
  const greeter = r.new_numbers ? byKey.get(r.new_numbers) : undefined;
  const handoffs = r.workflows.flatMap((w) => w.jumps_to.map((to) => ({ from: w, to })));

  return (
    <div className="card space-y-3 text-sm">
      <div>
        <div className="font-medium">How an incoming WhatsApp message is answered</div>
        <div className="text-xs text-slate-500">
          Checked in this order for every message; the order-status bot is always on. A published workflow that is
          not switched on answers only your test numbers
          {r.test_numbers.length ? ` (${r.test_numbers.join(", ")}).` : " - none are set yet; add your phone in Settings."}
        </div>
      </div>
      <ol className="space-y-3">
        <Step n={1} title="Already inside a workflow?">
          It carries on there{inside ? <> — <b>{inside}</b> {inside === 1 ? "customer is" : "customers are"} right now</> : null}.
          Sending “menu” or an order number lets a customer out.
          {handoffs.length > 0 && (
            <div className="text-xs text-slate-500 mt-1">
              Hand-offs: {handoffs.map(({ from, to }, n) => (
                <span key={`${from.key}-${to}`}>{n > 0 && " · "}“{from.title}” → {to === BUILTIN_ORDER_STATUS ? "order-status main menu" : `“${byKey.get(to)?.title ?? to}”`}</span>
              ))}
            </div>
          )}
        </Step>
        <Step n={2} title="A keyword, or a row a workflow added to the main menu?">
          {starters.length === 0 ? (
            <span className="text-slate-500">No live workflow starts on a keyword or a menu row yet.</span>
          ) : (
            <div className="flex flex-wrap gap-1.5 mt-1">
              {starters.flatMap((w) => [
                ...w.keywords.map((k) => (
                  <Chip key={`${w.key}-k-${k.text}`} to={w} word={`“${k.text}”${k.match === "contains" ? " anywhere" : ""}`} />
                )),
                ...(w.menu_label ? [<Chip key={`${w.key}-menu`} to={w} word={`Menu row “${w.menu_label}”`} />] : []),
              ])}
            </div>
          )}
        </Step>
        <Step n={3} title="A number that is not in your customer list?">
          {greeter ? (
            <>Greeted by <Link className="text-brand-700 hover:underline" to={`/workflows/${greeter.key}`}>{greeter.title}</Link>, once per conversation.</>
          ) : (
            <span className="text-slate-500">Gets the order-status bot's “not registered” reply.</span>
          )}
        </Step>
        <Step n={4} title="Everything else">
          The <b>order-status bot</b>. Its main menu shows:
          <div className="flex flex-wrap gap-1.5 mt-1">
            {(r.main_menu.en ?? []).map((t) => (
              <span key={t} className="rounded-md border border-slate-200 bg-slate-50 px-2 py-0.5 text-xs">{t}</span>
            ))}
          </div>
        </Step>
      </ol>
      {r.conflicts.length > 0 && (
        <div className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-900 space-y-0.5">
          {r.conflicts.map((c) => <div key={c}>⚠ {c}</div>)}
        </div>
      )}
    </div>
  );
}

function Step({ n, title, children }: { n: number; title: string; children: ReactNode }) {
  return (
    <li className="flex gap-3">
      <span className="grid place-items-center w-6 h-6 rounded-full bg-brand-50 text-brand-700 text-xs font-semibold shrink-0">{n}</span>
      <div className="min-w-0">
        <div className="font-medium text-slate-800">{title}</div>
        <div className="text-slate-600">{children}</div>
      </div>
    </li>
  );
}

function Chip({ word, to }: { word: string; to: RoutingWorkflow }) {
  return (
    <Link to={`/workflows/${to.key}`}
      className="rounded-full border border-brand-200 bg-brand-50 px-2 py-0.5 text-xs text-brand-800 hover:bg-brand-100">
      {word} → {to.title}{to.for_everyone ? "" : " (test numbers only)"}
    </Link>
  );
}

/** Describe a workflow and let the AI assistant draw it. It lands as a draft to check, test and publish. */
function AiCreateCard({ onDone }: { onDone: (key: string, note: string) => void }) {
  const [status, setStatus] = useState<AiStatus | null>(null);
  const [text, setText] = useState("");
  const [job, setJob] = useState<AiJob | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    api.aiStatus().then(setStatus).catch(() => setStatus(null));
    return () => { alive.current = false; };
  }, []);
  const running = job?.status === "running";

  const create = async () => {
    if (!text.trim() || running) return;
    setErr(null);
    try {
      const started = await api.aiCreate(text.trim());
      setJob(started);
      const done = await waitForJob(started.id, setJob, () => alive.current);
      if (!done) return;
      if (done.status === "failed" || !done.result?.key) {
        setErr(done.error || "The AI could not build it. Try again.");
        return;
      }
      const fails = done.result.issues.filter((i) => i.level === "fail").length;
      onDone(done.result.key, `Built by the AI assistant: ${done.result.doc.nodes.length} steps`
        + `${fails ? `, ${fails} to fix` : ""}. ${done.result.summary} Check it, test it, then publish.`);
    } catch (e) {
      setErr((e as Error).message);
    }
  };

  return (
    <div className="card space-y-2">
      <div className="flex items-center gap-2 font-medium text-sm">
        <Sparkles className="w-4 h-4 text-violet-600" /> Create with AI
      </div>
      {status && !status.configured ? (
        <div className="text-xs text-amber-900 bg-amber-50 border border-amber-200 rounded-lg px-3 py-2">
          Add a Groq API key in <Link className="underline" to="/settings">Settings</Link> (AI assistant) to describe a
          workflow in your own words and have it drawn for you.
        </div>
      ) : (
        <>
          <textarea className="input w-full" rows={3} value={text} disabled={running} aria-label="Describe the workflow"
            placeholder="Describe it in your own words - English, Hindi or Gujarati: who it is for, what it asks, where each answer goes."
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); void create(); } }} />
          <div className="flex flex-wrap gap-1.5">
            {AI_EXAMPLES.map((x) => (
              <button key={x.label} type="button" disabled={running} onClick={() => setText(x.text)}
                className="rounded-full border border-violet-200 bg-violet-50 px-2 py-0.5 text-xs text-violet-800 hover:bg-violet-100">
                {x.label}
              </button>
            ))}
          </div>
          <div className="flex items-center gap-2">
            <button className="btn-primary" disabled={running || !text.trim()} onClick={() => void create()}>
              <Sparkles className="w-4 h-4" /> {running ? "Building…" : "Create with AI"}
            </button>
            {running && <span className="text-xs text-slate-500">{stageText(job)}</span>}
          </div>
          {err && <div className="text-xs text-rose-700">{err}</div>}
          <div className="text-[11px] text-slate-500">
            It lands as a draft - nothing reaches customers until you publish. Only your description is sent to Groq,
            never customer data.
          </div>
        </>
      )}
    </div>
  );
}
