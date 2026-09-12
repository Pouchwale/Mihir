import { ReactNode, useEffect, useRef, useState } from "react";

export function fmt(dt: string | null | undefined): string {
  if (!dt) return "—";
  const d = new Date(dt.endsWith("Z") || dt.includes("+") ? dt : dt + "Z");
  if (isNaN(d.getTime())) return dt;
  return d.toLocaleString(undefined, { dateStyle: "short", timeStyle: "medium" });
}
export function ago(dt: string | null | undefined): string {
  if (!dt) return "never";
  const d = new Date(dt.endsWith("Z") || dt.includes("+") ? dt : dt + "Z").getTime();
  const s = Math.max(0, Math.round((Date.now() - d) / 1000));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}

export function usePoll<T>(fn: () => Promise<T>, ms: number, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const alive = useRef(true);
  const load = async () => {
    try {
      const d = await fn();
      if (alive.current) {
        setData(d);
        setError(null);
      }
    } catch (e) {
      if (alive.current) setError((e as Error).message);
    } finally {
      if (alive.current) setLoading(false);
    }
  };
  useEffect(() => {
    alive.current = true;
    load();
    const id = ms > 0 ? setInterval(load, ms) : undefined;
    return () => {
      alive.current = false;
      if (id) clearInterval(id);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  return { data, error, loading, reload: load };
}

export function Badge({ children, tone = "slate" }: { children: ReactNode; tone?: string }) {
  const map: Record<string, string> = {
    slate: "bg-slate-100 text-slate-700",
    green: "bg-emerald-100 text-emerald-700",
    red: "bg-rose-100 text-rose-700",
    amber: "bg-amber-100 text-amber-800",
    blue: "bg-blue-100 text-blue-700",
    violet: "bg-violet-100 text-violet-700",
  };
  return <span className={`badge ${map[tone] || map.slate}`}>{children}</span>;
}

export const STEP_TONE: Record<string, string> = { START: "slate", LANG: "slate", MENU: "blue", AWAIT_SO: "blue", CONFIRM: "amber", AWAIT_FG: "violet", DONE: "green", FAILED: "red" };
export const OUTCOME_TONE: Record<string, string> = {
  status_delivered: "green", welcome: "slate", ask_language: "slate", menu: "blue", contact: "blue", ask_so: "blue", ask_fg: "violet", confirm: "amber", not_found: "amber",
  verify_failed: "red", mismatch: "red", service_down: "red", rate_limited: "red", bye: "slate", custom: "blue",
  agent: "violet", with_agent: "violet", workflow: "blue", workflow_ai: "violet",
};

export function Stat({ label, value, sub, tone }: { label: string; value: ReactNode; sub?: ReactNode; tone?: string }) {
  return (
    <div className="card">
      <div className="text-xs uppercase tracking-wide text-slate-500">{label}</div>
      <div className={`text-2xl font-semibold mt-1 ${tone === "red" ? "text-rose-600" : tone === "amber" ? "text-amber-600" : tone === "green" ? "text-emerald-600" : ""}`}>{value}</div>
      {sub && <div className="text-xs text-slate-500 mt-1">{sub}</div>}
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="text-sm text-slate-500 py-8 text-center">{children}</div>;
}

export function ErrorBox({ msg }: { msg: string | null }) {
  if (!msg) return null;
  return <div className="rounded-lg bg-rose-50 border border-rose-200 text-rose-700 text-sm px-3 py-2">{msg}</div>;
}

/** Shows invisible differences between two strings (trailing spaces, case, NBSP). */
export function Diff({ a, b }: { a: string; b: string }) {
  const show = (s: string) =>
    s.split("").map((ch, i) => {
      const other = b === s ? a[i] : b[i];
      const same = other === ch;
      let disp = ch;
      let title = "";
      if (ch === " ") { disp = "␣"; title = "space"; }
      else if (ch === " ") { disp = "⍽"; title = "non-breaking space"; }
      else if (ch === "\t") { disp = "⇥"; title = "tab"; }
      return (
        <span key={i} title={title} className={same ? "" : "bg-rose-200 text-rose-900 rounded-sm"}>{disp}</span>
      );
    });
  return (
    <div className="font-mono text-xs space-y-0.5">
      <div><span className="text-slate-400 w-10 inline-block">Excel</span>{show(a)}</div>
      <div><span className="text-slate-400 w-10 inline-block">API</span>{show(b)}</div>
    </div>
  );
}
