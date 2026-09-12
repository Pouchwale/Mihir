import { useEffect, useState } from "react";
import { Plus, Trash2, X } from "lucide-react";
import { api } from "../api";
import {
  IDLE_MINUTES_DEFAULT, LANGS, LANG_NAME, LIMITS, triggersOf,
  type KeywordTrigger, type RoutingOverview, type Triggers, type WorkflowDoc, type WorkflowSettings,
} from "./types";

interface Props {
  doc: WorkflowDoc;
  wfKey: string;
  /** switched on for everyone (otherwise only the test numbers reach it) */
  live: boolean;
  readOnly: boolean;
  onChange: (doc: WorkflowDoc) => void;
  onClose: () => void;
}

/** How customers reach a workflow - WATI's chatbot triggers, plus who may reach it while testing.
 *  Stored in the document's settings, so it is drafted, published and rolled back with the steps. */
export default function StartPanel({ doc, wfKey, live, readOnly, onChange, onClose }: Props) {
  const settings = doc.settings ?? {};
  const t = triggersOf(doc);
  const [routing, setRouting] = useState<RoutingOverview | null>(null);
  useEffect(() => {
    api.workflowRouting().then(setRouting).catch(() => setRouting(null));
  }, []);

  const set = (patch: Partial<WorkflowSettings>) => onChange({ ...doc, settings: { ...settings, ...patch } });
  const setTriggers = (patch: Partial<Triggers>) => set({ triggers: { ...t, ...patch } });
  const setKeyword = (i: number, patch: Partial<KeywordTrigger>) =>
    setTriggers({ keywords: t.keywords.map((k, n) => (n === i ? { ...k, ...patch } : k)) });

  const reachedFrom = (routing?.workflows ?? []).filter((w) => w.key !== wfKey && w.jumps_to.includes(wfKey));
  const menuRows = [...(routing?.main_menu.en ?? []).filter((r) => r !== t.menu.label.en), ...(t.menu.enabled && t.menu.label.en ? [t.menu.label.en] : [])];

  return (
    <div className="p-4 space-y-5 text-sm">
      <div className="flex items-start gap-2">
        <div className="flex-1">
          <div className="font-semibold text-slate-900">How customers reach this workflow</div>
          <div className="text-xs text-slate-500 mt-0.5">
            Changes here go live when you publish. The order-status bot keeps answering every message nothing
            here claims.
          </div>
        </div>
        <button className="btn-ghost !h-8 !w-8 !px-0" onClick={onClose} aria-label="Close Start settings"><X className="w-4 h-4" /></button>
      </div>

      <fieldset disabled={readOnly} className="space-y-5 min-w-0">
        {/* ---- keywords ---- */}
        <section className="space-y-2">
          <div className="font-medium text-slate-800">Keywords</div>
          {t.keywords.length === 0 && <div className="text-xs text-slate-500">None yet.</div>}
          {t.keywords.map((k, i) => (
            <div key={i} className="flex gap-1.5">
              <input className="input flex-1 min-w-0" value={k.text} placeholder="e.g. sample" aria-label={`Keyword ${i + 1}`}
                onChange={(e) => setKeyword(i, { text: e.target.value })} />
              <select className="input w-32 text-xs" value={k.match} aria-label={`How keyword ${i + 1} matches`}
                onChange={(e) => setKeyword(i, { match: e.target.value as KeywordTrigger["match"] })}>
                <option value="exact">Whole message</option>
                <option value="contains">Anywhere</option>
                <option value="similar">Similar spelling</option>
              </select>
              <button className="btn-ghost !h-9 !w-9 !px-0 text-rose-600" aria-label={`Remove keyword ${i + 1}`}
                onClick={() => setTriggers({ keywords: t.keywords.filter((_, n) => n !== i) })}>
                <Trash2 className="w-4 h-4" />
              </button>
            </div>
          ))}
          <button className="btn-ghost text-xs" onClick={() => setTriggers({ keywords: [...t.keywords, { text: "", match: "exact" }] })}>
            <Plus className="w-3.5 h-3.5" /> Add a keyword
          </button>
          <div className="text-[11px] text-slate-500">
            <b>Whole message</b>: the customer sends exactly this (“sample”). <b>Anywhere</b>: it appears in what they
            write (“can I get a sample kit”). <b>Similar spelling</b>: also catches small typos (“samlpe kit”). A message with an SO, PO or item number never starts a workflow, so
            order lookups keep working.
          </div>
        </section>

        {/* ---- main menu ---- */}
        <section className="space-y-2">
          <label className="flex items-center gap-2 font-medium text-slate-800">
            <input type="checkbox" checked={t.menu.enabled}
              onChange={(e) => setTriggers({ menu: { ...t.menu, enabled: e.target.checked } })} />
            A row in the order-status bot's main menu
          </label>
          {t.menu.enabled && (
            <div className="space-y-1.5 pl-6">
              {LANGS.map((lg) => {
                const v = t.menu.label[lg] ?? "";
                return (
                  <label key={lg} className="block">
                    <span className="flex justify-between text-[11px] text-slate-500">
                      <span>{LANG_NAME[lg]}{lg === "en" ? " (required)" : " (blank shows the English)"}</span>
                      <span className={v.length > LIMITS.rowTitle ? "text-rose-600 font-medium" : ""}>{v.length}/{LIMITS.rowTitle}</span>
                    </span>
                    <input className="input w-full" value={v} placeholder={lg === "en" ? "e.g. Sample kit" : ""}
                      onChange={(e) => setTriggers({ menu: { ...t.menu, label: { ...t.menu.label, [lg]: e.target.value } } })} />
                  </label>
                );
              })}
              {menuRows.length > 0 && (
                <div className="rounded-lg border border-slate-200 bg-slate-50 p-2">
                  <div className="text-[11px] text-slate-500 mb-1">The main menu will show (as a list, once it has more than 3 rows):</div>
                  <div className="flex flex-wrap gap-1">
                    {menuRows.map((r) => (
                      <span key={r} className={`rounded-md border px-2 py-0.5 text-xs ${r === t.menu.label.en ? "border-brand-300 bg-brand-50 text-brand-800" : "border-slate-200 bg-white"}`}>{r}</span>
                    ))}
                  </div>
                </div>
              )}
            </div>
          )}
        </section>

        {/* ---- new numbers ---- */}
        <section className="space-y-1">
          <label className="flex items-center gap-2 font-medium text-slate-800">
            <input type="checkbox" checked={t.unknown_customer}
              onChange={(e) => setTriggers({ unknown_customer: e.target.checked })} />
            Numbers that are not in your customer list
          </label>
          <div className="text-[11px] text-slate-500 pl-6">
            A new contact gets this workflow instead of the “not registered” reply — once per conversation, so
            they are not greeted on every message.
          </div>
        </section>

        {/* ---- hand-offs ---- */}
        <section className="space-y-1">
          <div className="font-medium text-slate-800">From other workflows</div>
          {reachedFrom.length ? (
            <ul className="text-xs text-slate-600 list-disc pl-5">
              {reachedFrom.map((w) => <li key={w.key}>“{w.title}” hands customers over here</li>)}
            </ul>
          ) : (
            <div className="text-[11px] text-slate-500">
              No live workflow hands over here yet. Add a <b>Go to workflow</b> step in another workflow to send
              customers here, answers and all.
            </div>
          )}
        </section>

        {/* ---- who it answers ---- */}
        <section className="space-y-1">
          <div className="font-medium text-slate-800">Who it answers</div>
          <div className={`rounded-lg border px-3 py-2 text-xs ${live ? "border-emerald-200 bg-emerald-50 text-emerald-900" : "border-amber-200 bg-amber-50 text-amber-900"}`}>
            {live ? "Every customer - it is switched on." : (
              <>
                Only your test numbers until you switch it on with <b>Testing</b> in the toolbar
                {routing && (routing.test_numbers.length
                  ? <> ({routing.test_numbers.join(", ")}).</>
                  : <>. <b>No test numbers are set yet</b> - add your own phone in Settings, Conversation.</>)}
              </>
            )}
          </div>
        </section>

        {/* ---- AI ---- */}
        <section className="space-y-1">
          <label className="flex items-center gap-2 font-medium text-slate-800">
            <input type="checkbox" checked={!!settings.ai_understand}
              onChange={(e) => set({ ai_understand: e.target.checked })} />
            Understand typed answers with AI
          </label>
          <div className="text-[11px] text-slate-500 pl-6">
            When a customer types instead of tapping (“500 standup chahiye”), the AI works out which button or row they
            mean - only after the exact words and synonyms did not match. Off: they are asked again. Needs a Groq API
            key (Settings, AI assistant); only the question, its choices and what they typed are sent.
          </div>
        </section>

        {/* ---- inactivity ---- */}
        <section className="space-y-1">
          <label className="block">
            <span className="font-medium text-slate-800">End after inactivity (minutes)</span>
            <input type="number" min={1} max={1440} className="input w-28 mt-1" value={settings.idle_minutes ?? IDLE_MINUTES_DEFAULT}
              onChange={(e) => set({ idle_minutes: Number(e.target.value) })} />
          </label>
          <div className="text-[11px] text-slate-500">
            A customer who does not answer for this long is let out; their next message is answered as if they had
            just written in.
          </div>
        </section>
      </fieldset>
    </div>
  );
}
