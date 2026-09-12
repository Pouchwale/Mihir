import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useLocation, useParams } from "react-router-dom";
import {
  ArrowLeft, Check, ChevronDown, Download, Eye, Languages, LayoutGrid, Maximize2, Pencil, Play, Power, Redo2,
  Search, Sparkles, TriangleAlert, Undo2, Upload, X, Zap,
} from "lucide-react";
import { api, Lang } from "../api";
import { ago, ErrorBox } from "../ui";
import Canvas, { DRAG_TYPE, type CanvasApi } from "../workflow/Canvas";
import Inspector from "../workflow/Inspector";
import Simulator from "../workflow/Simulator";
import StartPanel from "../workflow/StartPanel";
import AiPanel from "../workflow/AiPanel";
import { TranslateContext, type FillMode } from "../workflow/translate";
import { downloadJson } from "../workflow/files";
import { NodeIcon, styleOf } from "../workflow/icons";
import { arrange, freeSpot } from "../workflow/layout";
import {
  LANGS, LANG_NAME, NODE_GROUPS, NODE_META, blankNode, getAt, isMultilingual, newNodeId, rewire, setAt,
  stringsOf,
  type AiBuildResult, type AiReview, type I18n, type ImportReport, type NodeKind, type WfIssue, type WfNode,
  type WorkflowDetail, type WorkflowDoc,
} from "../workflow/types";

const HISTORY_MAX = 100;
// Edits this close together (a burst of typing, a drag) come back as one undo step, not one per key.
const COALESCE_MS = 700;

export default function WorkflowEditor() {
  const { key = "" } = useParams();
  const location = useLocation();
  const [wf, setWf] = useState<WorkflowDetail | null>(null);
  const [doc, setDoc] = useState<WorkflowDoc | null>(null);
  const [issues, setIssues] = useState<WfIssue[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [lang, setLang] = useState<Lang>("en");
  const [err, setErr] = useState<string | null>(null);
  // a note handed over by the Workflows page, e.g. what Create with AI built
  const [msg, setMsg] = useState<string | null>(() => (location.state as { aiNote?: string } | null)?.aiNote ?? null);
  const [savedAt, setSavedAt] = useState<string | null>(null);
  const [dirty, setDirty] = useState(false);
  const [viewLive, setViewLive] = useState(false);
  const [testing, setTesting] = useState(false);
  const [publishing, setPublishing] = useState(false);
  const [showIssues, setShowIssues] = useState(true);
  const [q, setQ] = useState("");
  const [showStart, setShowStart] = useState(false);
  // the AI assistant: its panel, the result previewed on the canvas, and a review of the flow
  const [showAi, setShowAi] = useState(false);
  const [preview, setPreview] = useState<AiBuildResult | null>(null);
  const [aiSeed, setAiSeed] = useState<{ text: string; at: number } | null>(null);
  const [review, setReview] = useState<AiReview | null>(null);
  const [reviewing, setReviewing] = useState(false);
  const aiBase = useRef<WorkflowDoc | null>(null);  // the workflow when the AI was asked
  // what an import had to change and what to check - handed over by the Workflows page
  const [report, setReport] = useState<ImportReport | null>(
    () => (location.state as { report?: ImportReport } | null)?.report ?? null);
  const saveTimer = useRef<number | undefined>(undefined);
  const validateSeq = useRef(0);
  const canvas = useRef<CanvasApi | null>(null);
  const onReady = useCallback((a: CanvasApi) => { canvas.current = a; }, []);

  const docRef = useRef<WorkflowDoc | null>(null);
  docRef.current = doc;
  const past = useRef<WorkflowDoc[]>([]);
  const future = useRef<WorkflowDoc[]>([]);
  const lastPush = useRef(0);
  const [, rerender] = useState(0); // so Undo/Redo enable and disable as the history changes

  // Loaded once and owned locally. Deliberately NOT usePoll: a poll landing mid-edit would
  // overwrite whatever the owner had just typed.
  useEffect(() => {
    let alive = true;
    api.workflow(key)
      .then((w) => {
        if (!alive) return;
        setWf(w);
        setDoc(w.doc);
        setIssues(w.issues);
      })
      .catch((e) => alive && setErr((e as Error).message));
    return () => { alive = false; };
  }, [key]);

  // Leaving with an unsaved edit loses it; say so rather than silently dropping it.
  useEffect(() => {
    if (!dirty) return;
    const warn = (e: BeforeUnloadEvent) => { e.preventDefault(); e.returnValue = ""; };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);

  const validate = useCallback(async (next: WorkflowDoc) => {
    const seq = ++validateSeq.current;
    try {
      const r = await api.workflowValidate(next, key);
      if (seq === validateSeq.current) setIssues(r.issues);
    } catch { /* the save reports problems too */ }
  }, [key]);

  const save = useCallback(async (next: WorkflowDoc) => {
    try {
      const r = await api.workflowSaveDraft(key, next);
      setSavedAt(r.saved_at);
      setIssues(r.issues);
      setDirty(false);
      setWf((w) => (w ? { ...w, draft_version: r.version } : w));
    } catch (e) {
      setErr((e as Error).message);
    }
  }, [key]);

  /** Show a document, save it shortly, check it - without touching the undo history. */
  const apply = useCallback((next: WorkflowDoc) => {
    setDoc(next);
    setDirty(true);
    setMsg(null);
    window.clearTimeout(saveTimer.current);
    saveTimer.current = window.setTimeout(() => void save(next), 1000);
    void validate(next);
  }, [save, validate]);

  /** Every edit goes through here, so every edit can be undone. */
  const change = useCallback((next: WorkflowDoc) => {
    const cur = docRef.current;
    const now = Date.now();
    if (cur && now - lastPush.current > COALESCE_MS) {
      past.current = [...past.current.slice(-(HISTORY_MAX - 1)), cur];
    }
    future.current = [];
    lastPush.current = now;
    apply(next);
    rerender((n) => n + 1);
  }, [apply]);

  const undo = useCallback(() => {
    const cur = docRef.current;
    const prev = past.current[past.current.length - 1];
    if (!cur || !prev) return;
    past.current = past.current.slice(0, -1);
    future.current = [...future.current, cur];
    lastPush.current = 0;
    apply(prev);
    rerender((n) => n + 1);
  }, [apply]);

  const redo = useCallback(() => {
    const cur = docRef.current;
    const next = future.current[future.current.length - 1];
    if (!cur || !next) return;
    future.current = future.current.slice(0, -1);
    past.current = [...past.current, cur];
    lastPush.current = 0;
    apply(next);
    rerender((n) => n + 1);
  }, [apply]);

  const addNode = useCallback((kind: NodeKind, at?: { x: number; y: number }) => {
    const cur = docRef.current;
    if (!cur || viewLive || preview) return;
    // Clicked items go to the middle of what is on screen - not the top-left corner, where they
    // used to land on top of the steps already there.
    let want = at;
    if (!want) {
      const c = canvas.current?.centre() ?? { x: 200, y: 200 };
      want = { x: c.x - 144, y: c.y - 100 };
    }
    const spot = freeSpot(cur, want);
    const n = blankNode(kind, spot.x, spot.y);
    change({ ...cur, nodes: [...cur.nodes, n], start: cur.nodes.length ? cur.start : n.id });
    setSelected(n.id);
  }, [viewLive, preview, change]);

  const duplicateNode = useCallback((id: string) => {
    const cur = docRef.current;
    const src = cur?.nodes.find((n) => n.id === id);
    if (!cur || !src || viewLive) return;
    // a copy of the step itself, not its connections - wiring a copy the same way is rarely wanted
    const copy: WfNode = {
      ...(JSON.parse(JSON.stringify(src)) as WfNode),
      id: newNodeId(),
      title: `${src.title} (copy)`.slice(0, 60),
      x: (src.x ?? 0) + 40,
      y: (src.y ?? 0) + 40,
    };
    change({ ...cur, nodes: [...cur.nodes, copy] });
    setSelected(copy.id);
  }, [change, viewLive]);

  // Keyboard, as in WATI and every editor: Ctrl+Z / Ctrl+Y (or Ctrl+Shift+Z), Ctrl+D to duplicate.
  // Not while typing in a field, where Ctrl+Z must undo the typing instead.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null;
      if (t && (["INPUT", "TEXTAREA", "SELECT"].includes(t.tagName) || t.isContentEditable)) return;
      if (!(e.ctrlKey || e.metaKey) || viewLive || preview) return;
      const k = e.key.toLowerCase();
      if (k === "z" && !e.shiftKey) { e.preventDefault(); undo(); }
      else if ((k === "z" && e.shiftKey) || k === "y") { e.preventDefault(); redo(); }
      else if (k === "d" && selected) { e.preventDefault(); duplicateNode(selected); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [undo, redo, duplicateNode, selected, viewLive, preview]);

  const patchNode = (node: WfNode) => {
    if (!doc) return;
    const before = doc.nodes.find((n) => n.id === node.id);
    change({ ...doc, nodes: doc.nodes.map((n) => (n.id === node.id ? node : n)),
      edges: before ? rewire(doc.edges, before, node) : doc.edges });
  };

  const removeNode = (id: string) => {
    if (!doc) return;
    const nodes = doc.nodes.filter((n) => n.id !== id);
    change({
      ...doc,
      nodes,
      edges: doc.edges.filter((e) => e.from !== id && e.to !== id),
      start: doc.start === id ? (nodes[0]?.id ?? "") : doc.start,
    });
    setSelected(null);
  };

  const tidy = () => {
    if (!doc) return;
    change(arrange(doc));
    // Back to a readable view of the start, rather than shrinking the tidied flow to fit.
    window.setTimeout(() => canvas.current?.home(), 80);
  };

  const setMultilingual = (on: boolean) => {
    if (!doc) return;
    change({ ...doc, settings: { ...(doc.settings ?? {}), multilingual: on } });
    if (!on) setLang("en");
  };

  const exportFile = async () => {
    try {
      if (dirty && doc && !viewLive) {
        window.clearTimeout(saveTimer.current);
        await save(doc); // export what is on screen, not the last autosave
      }
      downloadJson(`${key}.json`, await api.workflowExport(key, viewLive ? "published" : "draft"));
    } catch (e) {
      setErr((e as Error).message);
    }
  };

  const publish = async () => {
    if (!doc) return;
    setPublishing(true);
    window.clearTimeout(saveTimer.current);
    await save(doc);
    try {
      const r = await api.workflowPublish(key);
      setMsg(wf?.live
        ? `Published as version ${r.published_version}. It is live, so customers meet this version from their next conversation.`
        : `Published as version ${r.published_version}. Only your test numbers reach it - try it on your phone, then click Testing to switch it on for everyone.`);
      setWf(await api.workflow(key));
    } catch (e) {
      const status = (e as { status?: number }).status;
      setErr(status === 422 ? "Fix the problems listed at the bottom, then publish again." : (e as Error).message);
    } finally {
      setPublishing(false);
    }
  };

  // ---- translation: English into Hindi and Gujarati with a free translator ----
  const lastAuto = useRef(new Map<string, string>());  // what auto-translate last wrote, per text and language
  const [translating, setTranslating] = useState<string | null>(null);

  /** Translate these texts and write them into the latest document as one undoable change. */
  const fillTexts = useCallback(async (targets: { nodeId: string; path: string }[], mode: FillMode, busyTag: string) => {
    const cur = docRef.current;
    if (!cur) return 0;
    const englishOf = (t: { nodeId: string; path: string }) =>
      (((getAt(cur.nodes.find((n) => n.id === t.nodeId), t.path) as I18n | undefined)?.en) ?? "").trim();
    const wanted = targets.map((t) => ({ ...t, en: englishOf(t) })).filter((t) => t.en);
    if (!wanted.length) return 0;
    setTranslating(busyTag);
    try {
      const texts = [...new Set(wanted.map((t) => t.en))];
      const done: Record<"hi" | "gu", Record<string, string>> = { hi: {}, gu: {} };
      for (let i = 0; i < texts.length; i += 100) {
        const chunk = texts.slice(i, i + 100);
        const r = await api.workflowTranslate(chunk);
        for (const lg of ["hi", "gu"] as const) {
          chunk.forEach((t, j) => { const got = r.translations[lg]?.[j]; if (got) done[lg][t] = got; });
        }
      }
      const latest = docRef.current;
      if (!latest) return 0;
      let filled = 0;
      const nodes = latest.nodes.map((n) => {
        let node = n;
        for (const t of wanted.filter((w) => w.nodeId === n.id)) {
          const v: I18n = { ...((getAt(node, t.path) as I18n | undefined) ?? {}) };
          if ((v.en ?? "").trim() !== t.en) continue;  // the English changed while we waited; its next blur redoes it
          let touched = false;
          for (const lg of ["hi", "gu"] as const) {
            const next = done[lg][t.en];
            if (!next) continue;
            const key = `${n.id}|${t.path}|${lg}`;
            const had = (v[lg] ?? "").trim();
            // Never over what the owner typed themselves - only empty, or what auto-translate wrote last.
            if (mode === "overwrite" || !had || had === lastAuto.current.get(key)) {
              if (had !== next) { v[lg] = next; touched = true; filled += 1; }
              lastAuto.current.set(key, next);
            }
          }
          if (touched) node = setAt(node, t.path, v);
        }
        return node;
      });
      if (filled) change({ ...latest, nodes });
      return filled;
    } catch (e) {
      setErr(`Translate: ${(e as Error).message}`);
      return 0;
    } finally {
      setTranslating(null);
    }
  }, [change]);

  const missingIn = (d: WorkflowDoc, nodeId?: string) => stringsOf(d).filter((s) => (!nodeId || s.nodeId === nodeId)
    && (s.value.en ?? "").trim() && (!(s.value.hi ?? "").trim() || !(s.value.gu ?? "").trim()));

  const translateAll = async () => {
    const cur = docRef.current;
    if (!cur) return;
    const todo = missingIn(cur);
    if (!todo.length) { setMsg("Every text already has its Hindi and Gujarati."); return; }
    const filled = await fillTexts(todo, "empty", "*");
    if (filled) setMsg(`Filled ${filled} Hindi and Gujarati texts. Read them through - a machine can misread a short button.`);
  };

  const autoTranslate = doc?.settings?.auto_translate !== false;
  const translateApi = useMemo(() => ({
    enabled: !!doc && isMultilingual(doc) && !viewLive,
    auto: autoTranslate,
    setAuto: (on: boolean) => {
      const cur = docRef.current;
      if (cur) change({ ...cur, settings: { ...(cur.settings ?? {}), auto_translate: on } });
    },
    fill: async (nodeId: string, path: string, mode: FillMode) => { await fillTexts([{ nodeId, path }], mode, `${nodeId}|${path}`); },
    fillStep: async (nodeId: string) => {
      const cur = docRef.current;
      if (cur) await fillTexts(missingIn(cur, nodeId), "empty", `${nodeId}|*`);
    },
    busy: translating,
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }), [doc, viewLive, autoTranslate, change, fillTexts, translating]);

  const rename = async (title: string) => {
    try {
      await api.workflowRename(key, title);
      setWf((w) => (w ? { ...w, title } : w));
    } catch (e) {
      setErr((e as Error).message);
    }
  };

  // A step and the Start settings share the right-hand panel; opening one closes the other.
  const select = useCallback((id: string | null) => {
    if (preview) return;  // an AI preview is looked at, not edited: apply it first
    setSelected(id);
    if (id) setShowStart(false);
  }, [preview]);

  const toggleLive = async () => {
    if (!wf) return;
    try {
      const r = await api.workflowSetLive(key, !wf.live);
      setWf({ ...wf, live: r.live, enabled: r.live });
      setMsg(r.live ? "Live: every customer can reach this workflow now."
        : "Back to testing: only your test numbers reach it. Anyone else inside it is let out at their next message.");
    } catch (e) {
      setErr((e as Error).message);
    }
  };

  // ---- the AI assistant ----
  const showPreview = (r: AiBuildResult | null) => {
    setPreview(r ? { ...r, doc: arrange(r.doc) } : null);
    setSelected(null);
    window.setTimeout(() => canvas.current?.home(), 80);
  };

  const applyPreview = () => {
    const cur = docRef.current;
    if (!preview || !cur) return;
    if (aiBase.current && aiBase.current !== cur && !confirm(
      "You changed the workflow while the AI was working. Applying replaces those changes with the AI's version. Apply anyway?")) return;
    change(preview.doc);
    setMsg(`Applied the AI's ${preview.mode === "new" ? "workflow" : "changes"} - Ctrl+Z undoes it. Test it, then publish.`
      + (preview.mode === "new" && preview.title && preview.title !== wf?.title
        ? ` Suggested name: “${preview.title}” (click the name to rename).` : ""));
    setPreview(null);
    setReview(null);
    window.setTimeout(() => canvas.current?.home(), 80);
  };

  const runReview = async () => {
    const cur = docRef.current;
    if (!cur) return;
    setReviewing(true);
    try {
      setReview(await api.aiReview(key, cur));
      setShowIssues(true);
    } catch (e) {
      setErr(`AI review: ${(e as Error).message}`);
    } finally {
      setReviewing(false);
    }
  };

  const fixWithAi = (text: string) => {
    setAiSeed({ text, at: Date.now() });
    setShowAi(true);
    setShowStart(false);
    setSelected(null);
  };

  const palette = useMemo(() => {
    const term = q.trim().toLowerCase();
    return NODE_GROUPS.map((group) => ({
      group,
      items: NODE_META.filter((m) => m.group === group && (!term || `${m.title} ${m.blurb}`.toLowerCase().includes(term))),
    })).filter((g) => g.items.length);
  }, [q]);

  if (err && !doc) return <div className="p-6"><ErrorBox msg={err} /></div>;
  if (!doc || !wf) return <div className="p-8 text-slate-500">Loading the workflow…</div>;

  const shown = preview ? preview.doc : viewLive && wf.published_doc ? wf.published_doc : doc;
  const locked = viewLive || !!preview;
  const multi = isMultilingual(shown);
  const viewLang: Lang = multi ? lang : "en";
  const barIssues = preview ? preview.issues : issues;  // while previewing, the checks of the preview itself
  const fails = barIssues.filter((i) => i.level === "fail").length;
  const warns = barIssues.length - fails;
  const node = shown.nodes.find((n) => n.id === selected) ?? null;
  const changed = dirty || wf.draft_version !== wf.published_version;
  const iconBtn = "btn-ghost !h-8 !w-8 !px-0";

  return (
    <div className="h-full flex flex-col bg-white">
      {/* ---------- toolbar ---------- */}
      <div className="h-14 shrink-0 border-b border-slate-200 flex items-center gap-2 px-4">
        <Link to="/workflows" className={iconBtn} title="Back to workflows" aria-label="Back to workflows">
          <ArrowLeft className="w-4 h-4" />
        </Link>
        <div className="min-w-0 flex-1 max-w-[16rem] mr-2">
          <EditableTitle title={wf.title} onSave={rename} />
          <div className="text-[11px] text-slate-500 leading-tight truncate">
            {wf.published_version
              ? changed ? `Published v${wf.published_version} · you have unpublished changes` : `Published v${wf.published_version}`
              : "Draft · never published"}
          </div>
        </div>

        <button className={iconBtn} onClick={undo} disabled={locked || !past.current.length}
          title="Undo (Ctrl+Z)" aria-label="Undo"><Undo2 className="w-4 h-4" /></button>
        <button className={iconBtn} onClick={redo} disabled={locked || !future.current.length}
          title="Redo (Ctrl+Y)" aria-label="Redo"><Redo2 className="w-4 h-4" /></button>

        <div className="ml-auto flex items-center gap-2">
          <select className="input !h-8 text-xs" aria-label="Languages" value={multi ? "3" : "1"} disabled={locked}
            onChange={(e) => setMultilingual(e.target.value === "3")}
            title="Three languages per step, or one text per step with a separate path per language (the WATI way)">
            <option value="3">3 languages per step</option>
            <option value="1">1 text per step</option>
          </select>
          {multi && (
            <div className="flex rounded-lg border border-slate-300 overflow-hidden h-8">
              {LANGS.map((lg) => (
                <button key={lg} onClick={() => setLang(lg)}
                  className={`px-2.5 text-xs ${lang === lg ? "bg-brand-600 text-white" : "bg-white text-slate-600 hover:bg-slate-50"}`}>
                  {LANG_NAME[lg]}
                </button>
              ))}
            </div>
          )}
          {multi && !locked && (
            <button className="btn-ghost !h-8" onClick={translateAll} disabled={translating !== null}
              title="Fill every missing Hindi and Gujarati text - with the AI when a Groq key is set, else a free translator">
              <Languages className="w-4 h-4" /> {translating === "*" ? "Translating…" : "Translate"}
            </button>
          )}
          <span className="text-xs text-slate-400 w-20 text-right">
            {dirty ? "Saving…" : savedAt ? `Saved ${ago(savedAt)}` : ""}
          </span>
          {wf.published_doc && (
            <button className={viewLive ? "btn-primary !h-8" : "btn-ghost !h-8"}
              disabled={!!preview} onClick={() => { setViewLive((v) => !v); setSelected(null); }} title="See exactly what is published">
              <Eye className="w-4 h-4" /> Published
            </button>
          )}
          <button className={iconBtn} onClick={tidy} disabled={locked}
            title="Arrange: lay the steps out neatly, left to right" aria-label="Arrange">
            <LayoutGrid className="w-4 h-4" />
          </button>
          <button className={iconBtn} onClick={() => canvas.current?.fitAll()} title="Fit: show every step" aria-label="Fit">
            <Maximize2 className="w-4 h-4" />
          </button>
          <button className={iconBtn} onClick={exportFile} title="Export this workflow as a file" aria-label="Export">
            <Download className="w-4 h-4" />
          </button>
          {wf.published_version && (
            <button onClick={toggleLive} disabled={viewLive}
              className={`btn-ghost !h-8 ${wf.live ? "!border-emerald-300 !bg-emerald-50 !text-emerald-700" : "!border-amber-300 !bg-amber-50 !text-amber-800"}`}
              title={wf.live ? "Answering every customer. Click to go back to your test numbers only."
                : "Answering only your test numbers (Settings). Click to switch it on for everyone."}>
              <Power className="w-4 h-4" /> {wf.live ? "Live" : "Testing"}
            </button>
          )}
          <button className={showAi ? "btn-primary !h-8" : "btn-ghost !h-8"} disabled={viewLive}
            onClick={() => { setShowAi((v) => !v); setShowStart(false); setSelected(null); }}
            title="Describe a workflow, or a change to it, and the AI assistant draws it">
            <Sparkles className="w-4 h-4" /> AI
          </button>
          <button className={showStart ? "btn-primary !h-8" : "btn-ghost !h-8"}
            onClick={() => { setShowStart((v) => !v); setSelected(null); }}
            title="How customers reach this workflow: keywords, a main-menu row, new numbers">
            <Zap className="w-4 h-4" /> Start
          </button>
          <button className="btn-ghost !h-8" onClick={() => setTesting(true)}>
            <Play className="w-4 h-4" /> Test
          </button>
          <button className="btn-primary !h-8" disabled={fails > 0 || publishing || !!preview} onClick={publish}
            title={fails > 0 ? "Fix the problems at the bottom first" : "Freeze this as a new version"}>
            <Upload className="w-4 h-4" /> {publishing ? "Publishing…" : "Publish"}
          </button>
        </div>
      </div>

      {report && (
        <div className="border-b border-sky-200 bg-sky-50 px-4 py-2 text-xs text-sky-950">
          <div className="flex items-start gap-2">
            <div className="flex-1 space-y-1">
              <div>
                <b>Imported {report.source === "wati" ? "from WATI" : "from a file"}:</b> {report.steps} steps and{" "}
                {report.connections} connections, every id kept. Problems to fix are listed at the bottom.
              </div>
              {report.warnings.length > 0 && (
                <ul className="list-disc ml-4 text-amber-900 space-y-0.5">
                  {report.warnings.map((w, n) => <li key={n}>{w}</li>)}
                </ul>
              )}
              {report.adapted.length > 0 && (
                <details>
                  <summary className="cursor-pointer text-sky-800">{report.adapted.length} {report.adapted.length === 1 ? "thing was" : "things were"} adjusted to fit</summary>
                  <ul className="list-disc ml-4 mt-1 space-y-0.5">{report.adapted.map((a, n) => <li key={n}>{a}</li>)}</ul>
                </details>
              )}
            </div>
            <button className={iconBtn} onClick={() => setReport(null)} aria-label="Dismiss the import report">
              <X className="w-4 h-4" />
            </button>
          </div>
        </div>
      )}

      {(msg || err) && (
        <div className={`px-4 py-2 text-xs border-b ${err ? "bg-rose-50 border-rose-200 text-rose-800" : "bg-brand-50 border-brand-200 text-brand-800"}`}>
          {err || msg}
          <button className="ml-3 underline" onClick={() => { setErr(null); setMsg(null); }}>dismiss</button>
        </div>
      )}

      <div className="flex-1 flex min-h-0">
        {/* ---------- palette ---------- */}
        <aside className="w-56 shrink-0 border-r border-slate-200 bg-slate-50/70 flex flex-col">
          <div className="p-3 border-b border-slate-200">
            <div className="relative">
              <Search className="w-4 h-4 text-slate-400 absolute left-2.5 top-1/2 -translate-y-1/2" />
              <input className="input w-full !pl-8 !h-8" placeholder="Find a step" value={q}
                onChange={(e) => setQ(e.target.value)} />
            </div>
            <div className="text-[11px] text-slate-500 mt-2">Click to add, or drag onto the canvas.</div>
          </div>
          <div className="flex-1 overflow-y-auto p-3 space-y-4">
            {palette.map(({ group, items }) => (
              <div key={group}>
                <div className="px-1 mb-1.5 text-[10.5px] font-semibold uppercase tracking-wider text-slate-400">{group}</div>
                <div className="space-y-1.5">
                  {items.map((m) => (
                    <button key={m.kind} draggable={!locked} disabled={locked}
                      onDragStart={(e) => { e.dataTransfer.setData(DRAG_TYPE, m.kind); e.dataTransfer.effectAllowed = "move"; }}
                      onClick={() => addNode(m.kind)}
                      title={m.blurb}
                      className="w-full flex items-center gap-2.5 rounded-lg border border-slate-200 bg-white px-2.5 py-1.5 text-left transition hover:border-brand-300 hover:shadow-card disabled:opacity-50 cursor-grab active:cursor-grabbing">
                      <span className={`grid place-items-center w-7 h-7 rounded-md shrink-0 ${styleOf(m.kind).chip}`}>
                        <NodeIcon kind={m.kind} className="w-4 h-4" />
                      </span>
                      <span className="text-[13px] font-medium text-slate-700">{m.title}</span>
                    </button>
                  ))}
                </div>
              </div>
            ))}
          </div>
        </aside>

        {/* ---------- canvas ---------- */}
        <div className="flex-1 min-w-0 relative bg-canvas">
          {viewLive && !preview && (
            <div className="absolute top-3 left-1/2 -translate-x-1/2 z-10 badge bg-emerald-600 text-white !px-3 !py-1 shadow-card">
              Viewing the published version — read only
            </div>
          )}
          {preview && (
            <div className="absolute top-3 left-1/2 -translate-x-1/2 z-10 flex items-center gap-2 rounded-full bg-violet-600 text-white pl-3 pr-1 py-1 shadow-card text-xs whitespace-nowrap">
              <Sparkles className="w-3.5 h-3.5" /> AI preview — nothing is saved yet
              <button className="rounded-full bg-white text-violet-700 px-2.5 py-0.5 font-medium hover:bg-violet-50" onClick={applyPreview}>Apply</button>
              <button className="rounded-full px-2 py-0.5 hover:bg-violet-500" onClick={() => showPreview(null)}>Discard</button>
            </div>
          )}
          {!node && !locked && (
            <div className="absolute top-3 left-3 z-10 pointer-events-none text-[11px] text-slate-500 bg-white/90 border border-slate-200 rounded-lg px-2.5 py-1.5 shadow-card">
              Select a step to edit it. A hollow red dot is an exit that leads nowhere.
            </div>
          )}
          <Canvas doc={shown} lang={viewLang} issues={preview ? preview.issues : viewLive ? [] : issues} selected={selected}
            readOnly={locked} onSelect={select} onChange={change}
            onDropNode={(kind, at) => addNode(kind, at)} onReady={onReady} />
        </div>

        {/* ---------- properties: only when a step is selected, so the canvas gets the room otherwise ---------- */}
        {node ? (
          <aside className="w-[380px] shrink-0 border-l border-slate-200 bg-white overflow-y-auto">
            <TranslateContext.Provider value={translateApi}>
              <Inspector doc={shown} node={node} lang={viewLang} issues={issues} readOnly={locked}
                onChange={patchNode} onMakeStart={(id) => change({ ...doc, start: id })} onDelete={removeNode}
                onDuplicate={duplicateNode} />
            </TranslateContext.Provider>
          </aside>
        ) : showStart ? (
          <aside className="w-[380px] shrink-0 border-l border-slate-200 bg-white overflow-y-auto">
            <StartPanel doc={shown} wfKey={key} live={!!wf.live} readOnly={locked} onChange={change} onClose={() => setShowStart(false)} />
          </aside>
        ) : null}
        {/* kept mounted while a step or Start is open, so a build in progress is not lost */}
        {showAi && (
          <aside className={`w-[380px] shrink-0 border-l border-slate-200 bg-white overflow-y-auto ${node || showStart ? "hidden" : ""}`}>
            <AiPanel wfKey={key} doc={doc} preview={preview} seed={aiSeed}
              onStart={() => { aiBase.current = docRef.current; }}
              onPreview={showPreview} onApply={applyPreview} onClose={() => setShowAi(false)} />
          </aside>
        )}
      </div>

      {/* ---------- problems ---------- */}
      <div className="shrink-0 border-t border-slate-200 bg-white">
        <div className="flex items-center">
        <button className="flex-1 min-w-0 flex items-center gap-2 px-4 h-9 text-xs hover:bg-slate-50" onClick={() => setShowIssues((v) => !v)}>
          {fails > 0
            ? <TriangleAlert className="w-4 h-4 text-rose-600" />
            : warns > 0 ? <TriangleAlert className="w-4 h-4 text-amber-500" /> : <Check className="w-4 h-4 text-emerald-600" />}
          <span className={`font-medium ${fails ? "text-rose-700" : "text-slate-700"}`}>
            {fails > 0 ? `${fails} ${fails === 1 ? "problem" : "problems"} to fix before publishing` : "Ready to publish"}
          </span>
          {warns > 0 && <span className="text-slate-500">· {warns} worth checking</span>}
          {(barIssues.length > 0 || review) && <ChevronDown className={`w-4 h-4 ml-auto text-slate-400 transition-transform ${showIssues ? "rotate-180" : ""}`} />}
        </button>
        {!locked && (
          <button className="btn-ghost !h-7 text-xs mr-3 shrink-0" onClick={() => void runReview()} disabled={reviewing}
            title="Ask the AI what a customer would stumble on - beyond these automatic checks">
            <Sparkles className="w-3.5 h-3.5" /> {reviewing ? "Reviewing…" : "Review with AI"}
          </button>
        )}
        </div>
        {showIssues && (barIssues.length > 0 || review) && (
          <div className="max-h-48 overflow-y-auto px-4 pb-2 space-y-0.5">
            {barIssues.map((i, n) => (
              <button key={n} className="flex items-start gap-2 w-full text-left text-[12px] py-0.5 hover:underline"
                onClick={() => {
                  if (i.node_id) { select(i.node_id); canvas.current?.focus(i.node_id); }
                  else if (i.field.startsWith("settings")) { setSelected(null); setShowStart(true); }
                }}>
                <span className={`mt-1.5 w-1.5 h-1.5 rounded-full shrink-0 ${i.level === "fail" ? "bg-rose-500" : "bg-amber-400"}`} />
                <span className="text-slate-700">{i.message}</span>
              </button>
            ))}
            {review && (
              <div className="mt-2 rounded-lg border border-violet-200 bg-violet-50/60 px-3 py-2 space-y-1">
                <div className="flex items-center gap-2 text-[12px] font-medium text-violet-900">
                  <Sparkles className="w-3.5 h-3.5 shrink-0" /> AI review{review.summary ? `: ${review.summary}` : ""}
                  <button className="ml-auto text-violet-700 hover:underline font-normal" onClick={() => setReview(null)}>dismiss</button>
                </div>
                {review.findings.length === 0 && (
                  <div className="text-[12px] text-violet-900">Nothing to add beyond the checks above.</div>
                )}
                {review.findings.map((f, n) => (
                  <div key={n} className="flex items-start gap-2 text-[12px]">
                    <span className={`mt-1.5 w-1.5 h-1.5 rounded-full shrink-0 ${f.severity === "problem" ? "bg-rose-400" : "bg-violet-400"}`} />
                    <button className="flex-1 text-left text-slate-700 enabled:hover:underline" disabled={!f.step_id}
                      onClick={() => { if (f.step_id) { select(f.step_id); canvas.current?.focus(f.step_id); } }}>
                      {f.problem}{f.suggestion && <span className="text-slate-500"> — {f.suggestion}</span>}
                    </button>
                    <button className="shrink-0 text-violet-700 hover:underline"
                      onClick={() => fixWithAi(`${f.step_id ? `In step "${f.step_id}": ` : ""}${f.problem} ${f.suggestion}`.trim())}>
                      Fix with AI
                    </button>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
      </div>

      {testing && <Simulator wfKey={key} title={wf.title} hasPublished={!!wf.published_doc} onClose={() => setTesting(false)} />}
    </div>
  );
}

/** The workflow's name, renamed in place: click, type, Enter. Its key - and every link to it - stays. */
function EditableTitle({ title, onSave }: { title: string; onSave: (t: string) => Promise<void> }) {
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState(title);
  const cancelled = useRef(false);
  useEffect(() => { if (!editing) setValue(title); }, [title, editing]);
  const commit = async () => {
    const t = value.trim().slice(0, 120);
    setEditing(false);
    if (!cancelled.current && t && t !== title) await onSave(t);
    cancelled.current = false;
  };
  if (editing) {
    return (
      <input className="input !h-7 w-full text-sm font-semibold" value={value} autoFocus maxLength={120}
        aria-label="Workflow name" title="Enter to save, Esc to cancel" onFocus={(e) => e.currentTarget.select()}
        onChange={(e) => setValue(e.target.value)} onBlur={() => void commit()}
        onKeyDown={(e) => {
          if (e.key === "Enter") (e.target as HTMLInputElement).blur();
          if (e.key === "Escape") { cancelled.current = true; (e.target as HTMLInputElement).blur(); }
        }} />
    );
  }
  return (
    <button className="group -mx-1 flex w-full min-w-0 items-center gap-1 rounded px-1 font-semibold leading-tight text-slate-900 hover:bg-slate-100"
      title="Click to rename" aria-label={`Rename ${title}`} onClick={() => setEditing(true)}>
      <span className="truncate">{title}</span>
      <Pencil className="h-3.5 w-3.5 shrink-0 text-slate-500 group-hover:text-brand-700" aria-hidden />
    </button>
  );
}
