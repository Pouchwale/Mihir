import { lazy, Suspense, useEffect, useState } from "react";
import { NavLink, Navigate, Route, Routes, useLocation } from "react-router-dom";
import {
  Activity, Bot, Database, FileText, FlaskConical, GitCompareArrows, LayoutDashboard, LogOut,
  MessageSquareText, MessagesSquare, PanelLeftClose, PanelLeftOpen, Rocket,
  Settings as SettingsIcon, Table2, UserPlus, Users, Workflow, type LucideIcon,
} from "lucide-react";
import { api, clearKey, getKey, setKey } from "./api";
import Data from "./pages/Data";
import Diagnostics from "./pages/Diagnostics";
import GoLive from "./pages/GoLive";
import Imports from "./pages/Imports";
import Messages from "./pages/Messages";
import Mismatches from "./pages/Mismatches";
import NewCustomers from "./pages/NewCustomers";
import Overview from "./pages/Overview";
import Sessions from "./pages/Sessions";
import Settings from "./pages/Settings";
import Simulator from "./pages/Simulator";
import Templates from "./pages/Templates";
import WaTemplates from "./pages/WaTemplates";
import Workflows from "./pages/Workflows";

// The editor pulls in the canvas library. Lazy, so it never lands in the main bundle and every other
// page loads exactly as fast as before.
const WorkflowEditor = lazy(() => import("./pages/WorkflowEditor"));

interface NavItem { to: string; label: string; icon: LucideIcon }
interface NavGroup { label?: string; items: NavItem[] }

/** Grouped the way WATI groups its own sidebar, so the layout is familiar to anyone who uses both. */
const NAV: NavGroup[] = [
  { items: [{ to: "/", label: "Overview", icon: LayoutDashboard }] },
  {
    label: "Chatbot",
    items: [
      { to: "/workflows", label: "Workflows", icon: Workflow },
      { to: "/templates", label: "Bot messages", icon: MessageSquareText },
      { to: "/simulator", label: "Test chat", icon: FlaskConical },
    ],
  },
  { label: "Templates", items: [{ to: "/wa-templates", label: "WhatsApp templates", icon: FileText }] },
  {
    label: "Conversations",
    items: [
      { to: "/messages", label: "Chat log", icon: MessagesSquare },
      { to: "/sessions", label: "Live sessions", icon: Users },
    ],
  },
  {
    label: "Data",
    items: [
      { to: "/imports", label: "Data sources", icon: Database },
      { to: "/data", label: "Browse data", icon: Table2 },
      { to: "/new-customers", label: "New customers", icon: UserPlus },
      { to: "/name-mismatches", label: "Name mismatches", icon: GitCompareArrows },
    ],
  },
  {
    label: "Setup",
    items: [
      { to: "/settings", label: "Settings", icon: SettingsIcon },
      { to: "/go-live", label: "Go live", icon: Rocket },
      { to: "/diagnostics", label: "Diagnostics", icon: Activity },
    ],
  },
];

const ALL = NAV.flatMap((g) => g.items.map((i) => ({ ...i, group: g.label })));

/** The menu entry the current address belongs to - the longest match, so /workflows/abc is Workflows. */
function here(pathname: string) {
  return ALL.filter((i) => (i.to === "/" ? pathname === "/" : pathname === i.to || pathname.startsWith(`${i.to}/`)))
    .sort((a, b) => b.to.length - a.to.length)[0];
}

type Mode = { mode: string; wati_mocked: boolean; orders_source: string };

function BrandMark() {
  return (
    <div className="grid place-items-center w-9 h-9 rounded-xl bg-brand-600 text-white shadow-sm shrink-0">
      <Bot className="w-5 h-5" strokeWidth={2} />
    </div>
  );
}

function ModePill({ mode }: { mode: Mode | null }) {
  if (!mode) return null;
  const detail = `Orders from: ${mode.orders_source}`;
  if (mode.mode === "dev") {
    return (
      <span className="badge bg-amber-50 text-amber-800 border border-amber-200" title={detail}>
        <span className="w-1.5 h-1.5 rounded-full bg-amber-500" />
        Dev mode · {mode.wati_mocked ? "WhatsApp simulated" : "WhatsApp LIVE"}
      </span>
    );
  }
  return mode.wati_mocked ? (
    <span className="badge bg-amber-50 text-amber-800 border border-amber-200" title={detail}>
      <span className="w-1.5 h-1.5 rounded-full bg-amber-500" /> WhatsApp not connected
    </span>
  ) : (
    <span className="badge bg-emerald-50 text-emerald-700 border border-emerald-200" title={detail}>
      <span className="w-1.5 h-1.5 rounded-full bg-emerald-500" /> Live
    </span>
  );
}

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
    } catch {
      clearKey();
      setErr("That admin key was not accepted.");
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="min-h-full flex items-center justify-center p-6">
      <form onSubmit={submit} className="card w-full max-w-sm !p-7 space-y-5">
        <div className="flex items-center gap-3">
          <BrandMark />
          <div>
            <div className="font-semibold text-slate-900 leading-tight">Order Status Bot</div>
            <div className="text-xs text-slate-500">WhatsApp automation</div>
          </div>
        </div>
        <div>
          <h1 className="text-lg font-semibold text-slate-900">Sign in</h1>
          <p className="text-sm text-slate-500">Enter the admin key set as ADMIN_KEY on the server.</p>
        </div>
        <input className="input w-full !h-10" type="password" placeholder="Admin key" value={key}
          onChange={(e) => setK(e.target.value)} autoFocus />
        {err && <div className="text-sm text-rose-600">{err}</div>}
        <button className="btn-primary w-full !h-10" disabled={busy || !key}>{busy ? "Checking…" : "Sign in"}</button>
      </form>
    </div>
  );
}

function Sidebar({ collapsed, onToggle }: { collapsed: boolean; onToggle: () => void }) {
  return (
    <aside className={`${collapsed ? "w-[68px]" : "w-60"} shrink-0 bg-white border-r border-slate-200 flex flex-col transition-[width] duration-200`}>
      <div className={`h-16 shrink-0 flex items-center gap-3 border-b border-slate-100 ${collapsed ? "justify-center" : "px-4"}`}>
        <BrandMark />
        {!collapsed && (
          <div className="min-w-0">
            <div className="font-semibold text-slate-900 leading-tight truncate">Order Status Bot</div>
            <div className="text-[11px] text-slate-500">WhatsApp automation</div>
          </div>
        )}
      </div>

      <nav className="flex-1 overflow-y-auto py-3 px-2.5 space-y-4">
        {NAV.map((g, gi) => (
          <div key={gi}>
            {g.label && !collapsed && (
              <div className="px-2.5 mb-1 text-[10.5px] font-semibold uppercase tracking-wider text-slate-400">{g.label}</div>
            )}
            {g.label && collapsed && <div className="mx-3 mb-2 border-t border-slate-100" />}
            <div className="space-y-0.5">
              {g.items.map(({ to, label, icon: Icon }) => (
                <NavLink key={to} to={to} end={to === "/"} title={collapsed ? label : undefined}
                  className={({ isActive }) =>
                    `flex items-center gap-3 rounded-lg text-sm transition-colors ${collapsed ? "justify-center h-10" : "px-2.5 h-9"} ${
                      isActive ? "bg-brand-50 text-brand-700 font-medium" : "text-slate-600 hover:bg-slate-100 hover:text-slate-900"}`}>
                  {({ isActive }) => (
                    <>
                      <Icon className={`w-[18px] h-[18px] shrink-0 ${isActive ? "text-brand-600" : "text-slate-400"}`} strokeWidth={2} />
                      {!collapsed && <span className="truncate">{label}</span>}
                    </>
                  )}
                </NavLink>
              ))}
            </div>
          </div>
        ))}
      </nav>

      <button onClick={onToggle} title={collapsed ? "Expand the menu" : "Collapse the menu"}
        className="h-11 shrink-0 flex items-center justify-center gap-2 border-t border-slate-100 text-xs text-slate-500 hover:text-slate-800 hover:bg-slate-50">
        {collapsed ? <PanelLeftOpen className="w-4 h-4" /> : <><PanelLeftClose className="w-4 h-4" /> Collapse</>}
      </button>
    </aside>
  );
}

export default function App() {
  const [authed, setAuthed] = useState<boolean | null>(null);
  const [mode, setMode] = useState<Mode | null>(null);
  const [collapsed, setCollapsed] = useState(() => {
    try { return localStorage.getItem("sidebar_collapsed") === "1"; } catch { return false; }
  });
  const { pathname } = useLocation();
  // The workflow editor takes the whole screen, like WATI's own builder: the menu folds to icons and
  // the page header gives way to the editor's toolbar.
  const editor = pathname.startsWith("/workflows/");
  const current = here(pathname);

  useEffect(() => {
    if (!getKey()) {
      setAuthed(false);
      return;
    }
    api.overview().then((o) => { setAuthed(true); setMode(o.health); }).catch(() => { clearKey(); setAuthed(false); });
  }, []);

  const toggle = () => setCollapsed((c) => {
    try { localStorage.setItem("sidebar_collapsed", c ? "0" : "1"); } catch { /* private mode */ }
    return !c;
  });

  if (authed === null) return <div className="p-8 text-slate-500">Loading…</div>;
  if (!authed) return <Login onOk={() => { setAuthed(true); api.overview().then((o) => setMode(o.health)).catch(() => {}); }} />;

  const routes = (
    <Routes>
      <Route path="/" element={<Overview />} />
      <Route path="/workflows" element={<Workflows />} />
      <Route path="/workflows/:key" element={
        <Suspense fallback={<div className="p-8 text-slate-500">Loading the editor…</div>}>
          <WorkflowEditor />
        </Suspense>} />
      <Route path="/templates" element={<Templates />} />
      <Route path="/simulator" element={<Simulator />} />
      <Route path="/wa-templates" element={<WaTemplates />} />
      <Route path="/messages" element={<Messages />} />
      <Route path="/sessions" element={<Sessions />} />
      <Route path="/imports" element={<Imports />} />
      <Route path="/data" element={<Data />} />
      <Route path="/new-customers" element={<NewCustomers />} />
      <Route path="/name-mismatches" element={<Mismatches />} />
      <Route path="/settings" element={<Settings />} />
      <Route path="/go-live" element={<GoLive />} />
      <Route path="/diagnostics" element={<Diagnostics />} />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );

  return (
    <div className="h-full flex">
      <Sidebar collapsed={collapsed || editor} onToggle={toggle} />
      <div className="flex-1 min-w-0 flex flex-col">
        {!editor && (
          <header className="h-16 shrink-0 bg-white border-b border-slate-200 flex items-center gap-4 px-6">
            <div className="text-sm min-w-0 truncate">
              {current?.group && <span className="text-slate-400">{current.group}<span className="mx-2">/</span></span>}
              <span className="font-medium text-slate-800">{current?.label ?? ""}</span>
            </div>
            <div className="ml-auto flex items-center gap-3">
              <ModePill mode={mode} />
              <button className="btn-ghost" onClick={() => { clearKey(); setAuthed(false); }}>
                <LogOut className="w-4 h-4" /> Sign out
              </button>
            </div>
          </header>
        )}
        <main className={`flex-1 min-h-0 ${editor ? "overflow-hidden" : "overflow-y-auto"}`}>
          {editor ? routes : <div className="max-w-7xl mx-auto px-6 py-6 space-y-5">{routes}</div>}
        </main>
      </div>
    </div>
  );
}
