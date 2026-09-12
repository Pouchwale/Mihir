import { useEffect, useMemo, useRef, useState } from "react";
import { api, Catalog, CustomReply, Lang, WatiStatus } from "../api";
import FlowCanvas from "../FlowCanvas";
import MenuPreview from "../MenuPreview";
import MessageEditor, { labelText, previewOptions } from "../MessageEditor";
import { ErrorBox, usePoll } from "../ui";

const LANGS: Lang[] = ["en", "hi", "gu"];

type Sel =
  | { kind: "template"; key: string }
  | { kind: "label"; key: string }
  | { kind: "custom"; key: string }
  | { kind: "new-custom" };

export default function Templates() {
  const { data, error, reload } = usePoll<Catalog>(() => api.templates(), 0);
  const wati = usePoll<WatiStatus>(() => api.watiStatus(), 0);
  const [tab, setTab] = useState<"flow" | "all" | "custom">("flow");
  const [sel, setSel] = useState<Sel | null>(null);
  const [lang, setLang] = useState<Lang>("en");
  const [filter, setFilter] = useState("");
  const editorRef = useRef<HTMLDivElement>(null);

  const select = (s: Sel) => {
    setSel(s);
    if (tab === "flow") requestAnimationFrame(() => editorRef.current?.scrollIntoView({ behavior: "smooth", block: "nearest" }));
  };

  useEffect(() => {
    if (!data) return;
    if (tab === "all" && (!sel || sel.kind === "custom" || sel.kind === "new-custom")) setSel({ kind: "template", key: data.templates[0].key });
    if (tab === "custom" && (!sel || (sel.kind !== "custom" && sel.kind !== "new-custom")))
      setSel(data.custom.length ? { kind: "custom", key: data.custom[0].key } : { kind: "new-custom" });
  }, [tab, data, sel]);

  const match = (s: string) => s.toLowerCase().includes(filter.toLowerCase());
  const tplOf = (key: string) => data?.templates.find((t) => t.key === key);
  const lblOf = (key: string) => data?.labels.find((t) => t.key === key);
  const customOf = (key: string) => data?.custom.find((c) => c.key === key);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-start gap-3">
        <div>
          <h1 className="text-xl font-semibold">Bot messages</h1>
          <p className="text-sm text-slate-500">Everything the customer reads on WhatsApp. Click a message, change the words, press Save — the very next customer sees it. No restart, no developer.</p>
        </div>
        <div className="ml-auto"><WatiBadge status={wati.data} error={wati.error} onRecheck={wati.reload} /></div>
      </div>

      <div className="flex flex-wrap gap-1 items-center">
        {([["flow", "Conversation map"], ["all", "All messages"], ["custom", "Custom replies"]] as const).map(([k, label]) => (
          <button key={k} onClick={() => setTab(k)} className={`btn ${tab === k ? "bg-brand-600 text-white border-brand-600" : "bg-white border-slate-300"}`}>{label}</button>
        ))}
        <div className="ml-auto flex items-center gap-1 text-sm">
          <span className="text-slate-500">Showing:</span>
          {LANGS.map((l) => (
            <button key={l} onClick={() => setLang(l)} className={`btn text-xs ${lang === l ? "bg-slate-700 text-white border-slate-700" : "bg-white border-slate-300"}`}>
              {data ? data.languages[l] : l}
            </button>
          ))}
        </div>
      </div>

      <ErrorBox msg={error} />

      {data && tab === "flow" && (
        <div className="space-y-4">
          <FlowCanvas
            nodes={data.flow.nodes}
            edges={data.flow.edges}
            templates={data.templates}
            lang={lang}
            labelOf={(k) => labelText(data, k, lang)}
            selected={sel && sel.kind === "template" ? sel.key : null}
            onSelect={(key) => select({ kind: "template", key })}
          />
          <div ref={editorRef} className="card">
            {sel && sel.kind === "template" && tplOf(sel.key) ? (
              <MessageEditor key={sel.key} kind="template" item={tplOf(sel.key)!} catalog={data} onSaved={reload} />
            ) : (
              <div className="text-sm text-slate-500 py-6 text-center">Click any white bubble above to edit that message.</div>
            )}
          </div>
          <div className="card text-sm text-slate-600">
            <div className="font-medium text-slate-800 mb-1">Messages that are not on the map</div>
            <div className="flex flex-wrap gap-1">
              {data.templates.filter((t) => !t.in_flow).map((t) => (
                <button key={t.key} className="btn-ghost text-xs" onClick={() => { setTab("all"); select({ kind: "template", key: t.key }); }}>{t.title}</button>
              ))}
            </div>
            <div className="text-xs text-slate-500 mt-1">Rare cases and system messages — edit them under “All messages”.</div>
          </div>
        </div>
      )}

      {data && tab === "all" && (
        <div className="grid lg:grid-cols-4 gap-4">
          <div className="card p-2 space-y-2 lg:max-h-[78vh] lg:overflow-auto">
            <input className="input w-full" placeholder="Search…" value={filter} onChange={(e) => setFilter(e.target.value)} />
            <Group title="Messages to the customer">
              {data.templates.filter((t) => match(t.title + t.key)).map((t) => (
                <Item key={t.key} title={t.title} active={sel?.kind === "template" && sel.key === t.key}
                  edited={LANGS.some((l) => t.langs[l].overridden) || t.buttons_overridden} onClick={() => select({ kind: "template", key: t.key })} />
              ))}
            </Group>
            <Group title="Menu buttons & labels">
              {data.labels.filter((t) => match(t.title + t.key)).map((t) => (
                <Item key={t.key} title={t.title} active={sel?.kind === "label" && sel.key === t.key}
                  edited={LANGS.some((l) => t.langs[l].overridden)} onClick={() => select({ kind: "label", key: t.key })} />
              ))}
            </Group>
          </div>
          <div className="lg:col-span-3 card">
            {sel?.kind === "template" && tplOf(sel.key) && <MessageEditor key={"t" + sel.key} kind="template" item={tplOf(sel.key)!} catalog={data} onSaved={reload} />}
            {sel?.kind === "label" && lblOf(sel.key) && <MessageEditor key={"l" + sel.key} kind="label" item={lblOf(sel.key)!} catalog={data} onSaved={reload} />}
          </div>
        </div>
      )}

      {data && tab === "custom" && (
        <div className="grid lg:grid-cols-4 gap-4">
          <div className="card p-2 space-y-1 lg:max-h-[78vh] lg:overflow-auto">
            <button className="btn-primary w-full justify-center" onClick={() => setSel({ kind: "new-custom" })}>+ New reply</button>
            {data.custom.length === 0 && <div className="text-xs text-slate-400 px-2 py-2">None yet. Example: a customer asks “timing” and the bot answers your office hours.</div>}
            {data.custom.map((c) => (
              <Item key={c.key} title={c.title} sub={c.triggers.join(", ")} active={sel?.kind === "custom" && sel.key === c.key} edited={false} disabled={!c.enabled}
                onClick={() => setSel({ kind: "custom", key: c.key })} />
            ))}
          </div>
          <div className="lg:col-span-3 card">
            {sel?.kind === "custom" && customOf(sel.key) && (
              <CustomEditor key={"c" + sel.key} initial={customOf(sel.key)!} catalog={data} lang={lang}
                onSaved={(k) => { reload(); setSel({ kind: "custom", key: k }); }} onDeleted={() => { setSel(null); reload(); }} />
            )}
            {sel?.kind === "new-custom" && (
              <CustomEditor key="c-new" initial={null} catalog={data} lang={lang}
                onSaved={(k) => { reload(); setSel({ kind: "custom", key: k }); }} onDeleted={() => setSel(null)} />
            )}
          </div>
        </div>
      )}
    </div>
  );
}

function WatiBadge({ status, error, onRecheck }: { status: WatiStatus | null; error: string | null; onRecheck: () => void }) {
  if (error) return <div className="text-xs text-rose-600">WATI status unavailable</div>;
  if (!status) return <div className="text-xs text-slate-400">checking WATI…</div>;
  const tone = status.connected ? "emerald" : status.mocked ? "amber" : "rose";
  const title = status.connected ? "Connected to WhatsApp (WATI)" : status.mocked ? "Test mode — WATI not connected" : "WATI connection problem";
  const cls: Record<string, string> = {
    emerald: "bg-emerald-50 border-emerald-300 text-emerald-800",
    amber: "bg-amber-50 border-amber-300 text-amber-800",
    rose: "bg-rose-50 border-rose-300 text-rose-800",
  };
  return (
    <div className={`rounded-lg border px-3 py-2 text-xs max-w-sm ${cls[tone]}`}>
      <div className="flex items-center gap-2 font-medium">
        <span className={`w-2 h-2 rounded-full ${status.connected ? "bg-emerald-500" : status.mocked ? "bg-amber-500" : "bg-rose-500"}`} />
        {title}
        <button className="ml-auto underline" onClick={onRecheck}>re-check</button>
      </div>
      <div className="mt-1 leading-snug">{status.detail}</div>
    </div>
  );
}

function Group({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div>
      <div className="px-2 pt-2 pb-1 text-[11px] uppercase tracking-wide text-slate-400">{title}</div>
      <div className="space-y-0.5">{children}</div>
    </div>
  );
}

function Item({ title, sub, active, edited, disabled, onClick }: { title: string; sub?: string; active: boolean; edited: boolean; disabled?: boolean; onClick: () => void }) {
  return (
    <button onClick={onClick} className={`w-full text-left rounded-lg px-2 py-1.5 text-sm ${active ? "bg-brand-50 text-brand-700" : "hover:bg-slate-50"} ${disabled ? "opacity-50" : ""}`}>
      <div className="flex items-center gap-1"><span className="truncate">{title}</span>{edited && <span className="ml-auto text-[10px] text-amber-600">edited</span>}</div>
      {sub && <div className="text-[11px] text-slate-400 truncate">{sub}</div>}
    </button>
  );
}

function CustomEditor({ initial, catalog, lang: initialLang, onSaved, onDeleted }: {
  initial: CustomReply | null; catalog: Catalog; lang: Lang; onSaved: (key: string) => void; onDeleted: () => void;
}) {
  const [key, setKey] = useState(initial?.key || "");
  const [title, setTitle] = useState(initial?.title || "");
  const [triggers, setTriggers] = useState((initial?.triggers || []).join(", "));
  const [texts, setTexts] = useState<Record<Lang, string>>({ en: initial?.texts.en || "", hi: initial?.texts.hi || "", gu: initial?.texts.gu || "" });
  const [buttons, setButtons] = useState<string[]>(initial?.buttons || []);
  const [enabled, setEnabled] = useState(initial?.enabled ?? true);
  const [lang, setLang] = useState<Lang>(initialLang);
  const [errors, setErrors] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [testText, setTestText] = useState("");
  const [testResult, setTestResult] = useState<string | null>(null);

  const slug = (s: string) => s.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 40);
  const effectiveKey = initial ? initial.key : key.trim() || slug(title);
  const opts = useMemo(() => previewOptions(catalog, "", buttons, lang), [catalog, buttons, lang]);

  const save = async () => {
    setBusy(true); setMsg(null);
    try {
      const r = await api.customSave({ key: effectiveKey, title, triggers: triggers.split(",").map((s) => s.trim()).filter(Boolean), texts, buttons, enabled });
      setErrors(r.errors || []);
      if (r.ok) { setMsg("Saved. The bot answers this from now on."); onSaved(effectiveKey); }
    } catch (e) { setMsg((e as Error).message); } finally { setBusy(false); }
  };
  const del = async () => {
    if (!initial || !confirm(`Delete "${initial.title}"?`)) return;
    setBusy(true);
    try { await api.templateReset("custom", initial.key); onDeleted(); }
    catch (e) { setMsg((e as Error).message); } finally { setBusy(false); }
  };
  const test = async () => {
    try {
      const r = await api.customTest(testText);
      setTestResult(r.match ? `The bot would answer with "${r.match.title}"` : "No custom reply matches — the normal order flow runs.");
    } catch (e) { setTestResult((e as Error).message); }
  };

  return (
    <div className="grid xl:grid-cols-5 gap-4">
      <div className="xl:col-span-3 space-y-3">
        <div className="font-semibold">{initial ? "Custom reply" : "New custom reply"}</div>
        <p className="text-sm text-slate-500">When a verified customer writes one of these words (and no order number), the bot answers with your text instead of the normal flow. It never interrupts a Yes/No answer or an order lookup.</p>
        <div className="grid sm:grid-cols-2 gap-2">
          <label className="text-sm">Name<input className="input w-full" value={title} onChange={(e) => setTitle(e.target.value)} placeholder="Office hours" /></label>
          <label className="text-sm">Short id<input className="input w-full font-mono" value={effectiveKey} disabled={!!initial} onChange={(e) => setKey(e.target.value)} placeholder="office-hours" /></label>
        </div>
        <label className="text-sm block">Words that trigger it (comma separated, any language)
          <input className="input w-full" value={triggers} onChange={(e) => setTriggers(e.target.value)} placeholder="timing, office hours, समय, સમય" />
        </label>
        <div className="flex gap-1">
          {LANGS.map((l) => (
            <button key={l} onClick={() => setLang(l)} className={`btn ${lang === l ? "bg-brand-600 text-white border-brand-600" : "bg-white border-slate-300"}`}>
              {catalog.languages[l]}{texts[l] ? "" : <span className="ml-1 text-[10px] opacity-70">(empty)</span>}
            </button>
          ))}
        </div>
        <textarea className="input w-full text-sm" rows={5} dir="auto" value={texts[lang]} onChange={(e) => setTexts({ ...texts, [lang]: e.target.value })}
          placeholder="Our office is open Mon-Sat 9:00-18:00. For urgent help call {support}." />
        <div className="text-xs text-slate-500">You can use <span className="font-mono">{"{support}"}</span> for your support contact. Languages you leave empty fall back to English.</div>
        <div className="text-sm">
          <div className="font-medium mb-1">Buttons under the reply (max 3)</div>
          <div className="flex flex-wrap gap-2">
            {catalog.button_choices.map((c) => {
              const on = buttons.includes(c.key);
              return (
                <button key={c.key} onClick={() => setButtons(on ? buttons.filter((b) => b !== c.key) : buttons.length >= 3 ? buttons : [...buttons, c.key])}
                  disabled={!on && buttons.length >= 3}
                  className={`btn text-sm ${on ? "bg-sky-50 border-sky-400 text-sky-700" : "bg-white border-slate-300 text-slate-600"}`}>
                  {on ? "✓ " : "+ "}{labelText(catalog, c.key, lang)}
                </button>
              );
            })}
          </div>
        </div>
        <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />Switched on</label>
        {errors.length > 0 && <ul className="text-sm text-rose-700 bg-rose-50 border border-rose-200 rounded-lg px-3 py-2 list-disc ml-4">{errors.map((e, i) => <li key={i}>{e}</li>)}</ul>}
        <div className="flex flex-wrap gap-2 items-center">
          <button className="btn-primary" disabled={busy} onClick={save}>Save</button>
          {initial && <button className="btn-ghost text-rose-700" disabled={busy} onClick={del}>Delete</button>}
          {msg && <span className="text-sm text-slate-600">{msg}</span>}
        </div>
        {initial && (
          <div className="border-t border-slate-100 pt-2 flex flex-wrap gap-2 items-center">
            <input className="input flex-1 min-w-[200px]" placeholder="Type a customer message to check the trigger…" value={testText} onChange={(e) => setTestText(e.target.value)} />
            <button className="btn-ghost" onClick={test}>Check</button>
            {testResult && <span className="text-xs text-slate-600">{testResult}</span>}
          </div>
        )}
      </div>
      <div className="xl:col-span-2">
        <div className="text-sm font-medium mb-1">What the customer sees</div>
        <div className="rounded-lg bg-[#efeae2] p-3">
          <div className="bg-white rounded-lg px-3 py-2 text-sm shadow-sm max-w-[92%]">
            <div className="whitespace-pre-wrap break-words">{(texts[lang] || texts.en || "…").split("{support}").join(String(catalog.sample.support))}</div>
            {opts && <MenuPreview options={opts} compact />}
          </div>
        </div>
      </div>
    </div>
  );
}
