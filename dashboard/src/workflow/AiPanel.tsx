import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { Sparkles, X } from "lucide-react";
import { api } from "../api";
import { AI_EXAMPLES, stageText, waitForJob } from "./ai";
import type { AiBuildResult, AiJob, AiStatus, WorkflowDoc } from "./types";

interface Props {
  wfKey: string;
  /** the workflow as it is on screen, unsaved edits included */
  doc: WorkflowDoc;
  /** the result previewed on the canvas, if any: a refinement builds on it */
  preview: AiBuildResult | null;
  /** text to start from, e.g. a review finding to fix */
  seed: { text: string; at: number } | null;
  /** a request is going out: the editor notes what the workflow looked like */
  onStart: () => void;
  onPreview: (r: AiBuildResult | null) => void;
  onApply: () => void;
  onClose: () => void;
}

/** Describe a workflow, or a change to one, and the AI assistant draws it on the canvas as a preview
 *  to apply, refine or discard. It sees the steps, never a customer. */
export default function AiPanel({ wfKey, doc, preview, seed, onStart, onPreview, onApply, onClose }: Props) {
  const [status, setStatus] = useState<AiStatus | null>(null);
  const [text, setText] = useState("");
  const [asked, setAsked] = useState<string[]>([]);
  const [job, setJob] = useState<AiJob | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    api.aiStatus().then(setStatus).catch(() => setStatus(null));
    return () => { alive.current = false; };
  }, []);
  useEffect(() => { if (seed) setText(seed.text); }, [seed]);

  const running = job?.status === "running";
  const noKey = status?.configured === false;
  const blank = doc.nodes.length <= 1 && !doc.nodes.some((n) => (n.text?.en ?? "").trim());

  const ask = async () => {
    const instruction = text.trim();
    if (!instruction || running || noKey) return;
    setErr(null);
    onStart();
    try {
      const started = await api.aiBuild(wfKey, instruction, preview ? preview.doc : doc, asked);
      setJob(started);
      const done = await waitForJob(started.id, setJob, () => alive.current);
      if (!done) return;
      if (done.status === "failed" || !done.result) {
        setErr(done.error || "The AI could not do that. Try again.");
        return;
      }
      setAsked((a) => [...a, instruction].slice(-6));
      setText("");
      onPreview(done.result);
    } catch (e) {
      setErr((e as Error).message);
    }
  };

  const fails = preview?.issues.filter((i) => i.level === "fail") ?? [];
  const warns = (preview?.issues.length ?? 0) - fails.length;

  return (
    <div className="p-4 space-y-4 text-sm">
      <div className="flex items-start gap-2">
        <span className="grid place-items-center w-7 h-7 rounded-md bg-violet-50 text-violet-600 shrink-0">
          <Sparkles className="w-4 h-4" />
        </span>
        <div className="flex-1">
          <div className="font-semibold text-slate-900">AI assistant</div>
          <div className="text-xs text-slate-500 mt-0.5">
            {blank ? "Describe the workflow and it is drawn for you." : "Say what to change and it is redrawn for you."}
          </div>
        </div>
        <button className="btn-ghost !h-8 !w-8 !px-0" onClick={onClose} aria-label="Close the AI assistant">
          <X className="w-4 h-4" />
        </button>
      </div>

      {noKey && (
        <div className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-900">
          No Groq API key yet. Add it in <Link className="underline" to="/settings">Settings</Link>, under AI
          assistant - the free plan is enough to start.
        </div>
      )}

      {preview && (
        <div className="rounded-lg border border-violet-200 bg-violet-50 p-3 space-y-2">
          <div className="font-medium text-violet-900">
            {preview.mode === "new" ? "Built" : "Changed"}: {preview.doc.nodes.length} steps ·{" "}
            {fails.length ? `${fails.length} to fix` : "ready to publish"}{warns ? ` · ${warns} to check` : ""}
          </div>
          {preview.summary && <div className="text-xs text-violet-900">{preview.summary}</div>}
          {preview.notes.length > 0 && (
            <ul className="text-xs text-amber-800 list-disc pl-4 space-y-0.5">
              {preview.notes.map((n, i) => <li key={i}>{n}</li>)}
            </ul>
          )}
          {preview.note && <div className="text-xs text-amber-800">{preview.note}</div>}
          {fails.length > 0 && (
            <ul className="text-xs text-rose-700 list-disc pl-4 space-y-0.5">
              {fails.slice(0, 5).map((i, n) => <li key={n}>{i.message}</li>)}
            </ul>
          )}
          <div className="text-[11px] text-violet-800">
            This is a preview on the canvas - nothing is saved yet.
            {preview.mode === "new" && preview.title ? ` Suggested name: “${preview.title}”.` : ""}
          </div>
          <div className="flex gap-2">
            <button className="btn-primary !h-8" onClick={onApply}>Apply</button>
            <button className="btn-ghost !h-8" onClick={() => onPreview(null)}>Discard</button>
          </div>
          <div className="text-[11px] text-slate-500">
            Want it different? Say what to change below and the preview is refined. Ctrl+Z undoes an applied change.
          </div>
        </div>
      )}

      <div className="space-y-2">
        <textarea className="input w-full" rows={6} value={text} disabled={running || noKey} aria-label="Tell the AI assistant"
          placeholder={blank && !preview
            ? "e.g. Greet new numbers, ask their language, then their name, company and city, and hand them to the Sales team."
            : "e.g. After the pouch list, ask how many pieces they need, and only accept a number."}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); void ask(); } }} />
        <div className="flex items-center gap-2">
          <button className="btn-primary !h-8" disabled={running || !text.trim() || noKey} onClick={() => void ask()}>
            <Sparkles className="w-4 h-4" /> {running ? "Working…" : preview ? "Refine" : blank ? "Build it" : "Change it"}
          </button>
          <span className="text-[11px] text-slate-400">Ctrl+Enter</span>
        </div>
        {running && (
          <div className="rounded-lg border border-slate-200 bg-slate-50 px-3 py-2 text-xs text-slate-700" role="status">
            {stageText(job)}
            {job && job.wait_seconds > 0 && (
              <div className="text-[11px] text-slate-500 mt-0.5">
                Groq&apos;s free plan allows about 8,000 tokens a minute, so a big workflow is built in parts.
              </div>
            )}
          </div>
        )}
        {err && <div className="rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-xs text-rose-800">{err}</div>}
      </div>

      {blank && !preview && !noKey && (
        <div className="space-y-1.5">
          <div className="text-xs font-medium text-slate-600">Or start from an idea</div>
          <div className="flex flex-wrap gap-1.5">
            {AI_EXAMPLES.map((x) => (
              <button key={x.label} type="button" disabled={running} onClick={() => setText(x.text)}
                className="rounded-full border border-violet-200 bg-violet-50 px-2 py-0.5 text-xs text-violet-800 hover:bg-violet-100">
                {x.label}
              </button>
            ))}
          </div>
        </div>
      )}

      {asked.length > 0 && (
        <div className="space-y-1">
          <div className="text-xs font-medium text-slate-600">Asked so far</div>
          <ul className="text-[11px] text-slate-500 list-disc pl-4 space-y-0.5">
            {asked.map((a, n) => <li key={n} className="line-clamp-2">{a}</li>)}
          </ul>
        </div>
      )}

      <div className="text-[11px] text-slate-500 border-t border-slate-100 pt-3">
        Write in English, Hindi or Gujarati. The assistant writes each step in all three languages, runs the same checks
        as Publish and fixes what they find. It sees your steps only - never customer numbers, names or orders - and
        nothing goes live until you publish.
      </div>
    </div>
  );
}
