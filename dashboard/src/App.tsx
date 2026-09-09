import { useEffect, useState } from "react";
import { NavLink, Navigate, Route, Routes } from "react-router-dom";
import { api, clearKey, getKey, setKey } from "./api";
import Overview from "./pages/Overview";
import Sessions from "./pages/Sessions";
import Messages from "./pages/Messages";
import Mismatches from "./pages/Mismatches";
import Imports from "./pages/Imports";
import Simulator from "./pages/Simulator";
import Settings from "./pages/Settings";
import Data from "./pages/Data";
import Templates from "./pages/Templates";
import GoLive from "./pages/GoLive";

const NAV = [
  ["/", "Overview"],
  ["/simulator", "Simulator"],
  ["/templates", "Messages"],
  ["/sessions", "Sessions"],
  ["/messages", "Chat log"],
  ["/mismatches", "Mismatches"],
  ["/imports", "Data sources"],
  ["/data", "Browse data"],
  ["/settings", "Settings"],
  ["/go-live", "Go live"],
] as const;

function Login({ onOk }: { onOk: () => void }) {
  const [key, setK] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setKey(key.trim());
    try {
      await api.overview();
      onOk();
    } catch (e2) {
      clearKey();
      setErr("Invalid admin key");
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="min-h-screen flex items-center justify-center p-6">
      <form onSubmit={submit} className="card w-full max-w-sm space-y-4">
        <div>
          <h1 className="text-lg font-semibold">Order Status Bot</h1>
          <p className="text-sm text-slate-500">Enter the admin key (ADMIN_KEY in backend .env).</p>
        </div>
        <input className="input w-full" type="password" placeholder="Admin key" value={key} onChange={(e) => setK(e.target.value)} autoFocus />
        {err && <div className="text-sm text-rose-600">{err}</div>}
        <button className="btn-primary w-full justify-center" disabled={busy || !key}>Sign in</button>
      </form>
    </div>
  );
}

export default function App() {
  const [authed, setAuthed] = useState<boolean | null>(null);
  const [mode, setMode] = useState<{ mode: string; wati_mocked: boolean; orders_source: string } | null>(null);

  useEffect(() => {
    if (!getKey()) {
      setAuthed(false);
      return;
    }
    api.overview().then((o) => { setAuthed(true); setMode(o.health); }).catch(() => { clearKey(); setAuthed(false); });
  }, []);

  if (authed === null) return <div className="p-8 text-slate-500">Loading…</div>;
  if (!authed) return <Login onOk={() => { setAuthed(true); api.overview().then((o) => setMode(o.health)).catch(() => {}); }} />;

  return (
    <div className="min-h-screen flex flex-col">
      {mode && mode.mode === "dev" && (
        <div className="bg-amber-400 text-amber-950 text-sm px-4 py-1.5 text-center font-medium">
          DEV MODE — WATI {mode.wati_mocked ? "mocked (no messages leave this server)" : "LIVE"} · orders source: {mode.orders_source} · dummy data
        </div>
      )}
      <header className="bg-white border-b border-slate-200">
        <div className="max-w-7xl mx-auto px-4 flex items-center gap-6 h-14">
          <div className="font-semibold text-slate-900">📦 Order Status Bot</div>
          <nav className="flex gap-1 overflow-x-auto">
            {NAV.map(([to, label]) => (
              <NavLink key={to} to={to} end={to === "/"}
                className={({ isActive }) => `px-3 py-1.5 rounded-lg text-sm whitespace-nowrap ${isActive ? "bg-brand-50 text-brand-700 font-medium" : "text-slate-600 hover:bg-slate-100"}`}>
                {label}
              </NavLink>
            ))}
          </nav>
          <div className="ml-auto">
            <button className="btn-ghost" onClick={() => { clearKey(); setAuthed(false); }}>Sign out</button>
          </div>
        </div>
      </header>
      <main className="flex-1 max-w-7xl w-full mx-auto p-4 space-y-4">
        <Routes>
          <Route path="/" element={<Overview />} />
          <Route path="/simulator" element={<Simulator />} />
          <Route path="/templates" element={<Templates />} />
          <Route path="/sessions" element={<Sessions />} />
          <Route path="/messages" element={<Messages />} />
          <Route path="/mismatches" element={<Mismatches />} />
          <Route path="/imports" element={<Imports />} />
          <Route path="/data" element={<Data />} />
          <Route path="/settings" element={<Settings />} />
          <Route path="/go-live" element={<GoLive />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </main>
    </div>
  );
}
