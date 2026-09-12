import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import type { Lang } from "../api";
import MenuPreview from "../MenuPreview";
import {
  AiReplyPanel, ApiPanel, AssignPanel, DataPanel, DelayPanel, JumpPanel, StatusPanel, SubscribePanel, TagsPanel,
  TemplatePanel, fieldsOf, useDataFields,
} from "./panels";
import { NodeIcon, styleOf } from "./icons";
import { NodeContext, useNodeId, useTranslate } from "./translate";
import { MediaPreview, WaText } from "./whatsapp";
import {
  CondOp, DEFAULT_LANGUAGE_LABELS, I18n, InputKind, LANGS, LANGUAGE_NAMES, LANG_NAME, LIMITS, MediaType,
  LOOKUP_SOURCES, LookupSource, QUESTION_PRESETS, SavedQuestion, WfInput, WfIssue, WfNode, WfOption, WorkflowDoc,
  answerExtras, applyPreset, isMultilingual, metaOf, offeredLanguages, ownQuestion, presetOf, questionSpec, textOf,
  variablesIn,
} from "./types";

type Set = (p: Partial<WfNode>) => void;

/** One trilingual string. There is a single language selector for the whole editor rather than one
 *  per field: a question with a 10-row list has 23 translatable strings, and 23 sets of tabs is
 *  unusable. When a translation is missing the English shows through as the placeholder, so the
 *  owner always sees what the customer will actually get. */
function Field({ label, value, onChange, lang, max, rows, hint, sideBySide, path }: {
  label: string;
  value: I18n | undefined;
  onChange: (v: I18n) => void;
  lang: Lang;
  max: number;
  rows?: number;
  hint?: string;
  sideBySide?: boolean;
  /** where this text lives in the step ("input.options.0.label"), so Translate can fill it */
  path?: string;
}) {
  const tr = useTranslate();
  const nodeId = useNodeId();
  const typedFrom = useRef("");
  const v = value ?? {};
  const current = v[lang] ?? "";
  const english = v.en ?? "";
  const over = current.length > max;
  const Tag = rows ? "textarea" : "input";
  const canTranslate = tr.enabled && !!path && !!english.trim();
  const busy = tr.busy === `${nodeId}|${path}`;
  return (
    <label className="block space-y-1">
      <div className="flex items-baseline gap-2">
        <span className="text-xs font-medium text-slate-600">{label}</span>
        {canTranslate && (
          <button type="button" className="text-[11px] text-brand-700 hover:underline disabled:opacity-50" disabled={busy}
            title="Translate this English into Hindi and Gujarati (replaces what is there)"
            onClick={(e) => { e.preventDefault(); void tr.fill(nodeId, path as string, "overwrite"); }}>
            {busy ? "translating…" : "Translate"}
          </button>
        )}
        <span className={`ml-auto text-[11px] ${over ? "text-rose-600 font-medium" : "text-slate-400"}`}>
          {current.length} / {max}
        </span>
      </div>
      {sideBySide && lang !== "en" && english && (
        <div className="text-[11px] text-slate-500 bg-slate-50 rounded px-2 py-1 whitespace-pre-wrap">{english}</div>
      )}
      <Tag
        className={`input w-full ${over ? "border-rose-400" : ""}`}
        dir="auto"
        rows={rows}
        value={current}
        placeholder={lang === "en" ? "" : english || ""}
        onChange={(e: { target: { value: string } }) => onChange({ ...v, [lang]: e.target.value })}
        onFocus={() => { typedFrom.current = current; }}
        onBlur={() => {
          // Finished writing the English: fill the empty Hindi and Gujarati right away.
          if (tr.enabled && tr.auto && path && lang === "en" && current.trim() && current !== typedFrom.current) {
            void tr.fill(nodeId, path, "empty");
          }
        }}
      />
      {lang !== "en" && !current && english && (
        <div className="text-[11px] text-slate-400">↳ empty, so these customers see the English</div>
      )}
      {hint && <div className="text-[11px] text-slate-400">{hint}</div>}
    </label>
  );
}

interface Props {
  doc: WorkflowDoc;
  node: WfNode | null;
  lang: Lang;
  issues: WfIssue[];
  readOnly?: boolean;
  onChange: (node: WfNode) => void;
  onMakeStart: (id: string) => void;
  onDelete: (id: string) => void;
  onDuplicate: (id: string) => void;
}

export default function Inspector({ doc, node, lang, issues, readOnly, onChange, onMakeStart, onDelete, onDuplicate }: Props) {
  const [sideBySide, setSideBySide] = useState(() => {
    try {
      return localStorage.getItem("wf_side_by_side") === "1";
    } catch {
      return false;
    }
  });

  const tr = useTranslate();

  if (!node) return null;

  const multi = isMultilingual(doc);
  // One text per step (the WATI way): every field edits the single text, whatever language is picked.
  const editLang: Lang = multi ? lang : "en";
  const side = multi && sideBySide;
  const mine = issues.filter((i) => i.node_id === node.id);
  const set: Set = (patch) => onChange({ ...node, ...patch });
  const meta = metaOf(node.type);
  const missing = countMissing(node);
  const hasFile = !!node.media?.url;

  return (
    <NodeContext.Provider value={node.id}>
    <div className="p-3 space-y-3">
      <div className="flex items-center gap-2">
        <span className={`grid place-items-center w-9 h-9 rounded-lg shrink-0 ${styleOf(node.type).chip}`}>
          <NodeIcon kind={node.type} className="w-[18px] h-[18px]" />
        </span>
        <input className="input flex-1 font-medium" value={node.title} disabled={readOnly}
          onChange={(e) => set({ title: e.target.value })} />
      </div>
      <div className="text-[11px] text-slate-500">{meta.blurb}</div>

      {/* Only on steps that say something: a Wait or a Tag has nothing to translate. */}
      {multi && missing.total > 0 && (
        <div className="flex items-center gap-2 text-[11px]">
          <span className={missing.en ? "text-rose-600 font-medium" : missing.hi + missing.gu === 0 ? "text-emerald-600" : "text-slate-500"}>
            {missing.en ? `EN ${missing.en} missing` : "EN ✓"}{missing.hi ? ` · HI ${missing.hi} missing` : ""}{missing.gu ? ` · GU ${missing.gu} missing` : ""}
          </span>
          {tr.enabled && missing.hi + missing.gu > 0 && (
            <button type="button" className="text-brand-700 hover:underline disabled:opacity-50" disabled={tr.busy !== null}
              onClick={() => void tr.fillStep(node.id)}>
              {tr.busy === `${node.id}|*` ? "translating…" : "Translate this step"}
            </button>
          )}
          {tr.enabled && (
            <label className="ml-auto flex items-center gap-1 text-slate-500"
              title="Fill Hindi and Gujarati as soon as you finish writing the English">
              <input type="checkbox" checked={tr.auto} onChange={(e) => tr.setAuto(e.target.checked)} />
              auto-translate
            </label>
          )}
          <label className={`${tr.enabled ? "" : "ml-auto "}flex items-center gap-1 text-slate-500`}>
            <input type="checkbox" checked={sideBySide}
              onChange={(e) => {
                setSideBySide(e.target.checked);
                try { localStorage.setItem("wf_side_by_side", e.target.checked ? "1" : "0"); } catch { /* private mode */ }
              }} />
            show English
          </label>
        </div>
      )}

      {node.unsupported && <UnsupportedNote node={node} />}

      <fieldset disabled={readOnly} className="space-y-3">
        {(node.type === "message" || node.type === "question" || node.type === "end" || node.type === "ai_reply") && (
          <Field
            label={node.type === "end" ? "Last message (optional)"
              : node.type === "ai_reply" ? "Opening line (optional), e.g. Ask me anything about our pouches"
              : hasFile ? "Text (optional with a file)" : "What the bot says"}
            value={node.text}
            onChange={(text) => set({ text })}
            path="text"
            lang={editLang}
            max={LIMITS.body}
            rows={4}
            sideBySide={side}
            hint={placeholderHint(doc)}
          />
        )}

        {node.type === "message" && !node.unsupported && <MediaPanel node={node} set={set} lang={editLang} sideBySide={side} />}
        {node.type === "question" && <QuestionPanel node={node} doc={doc} lang={editLang} set={set} sideBySide={side} />}
        {node.type === "product_list" && <ProductListPanel node={node} set={set} lang={editLang} sideBySide={side} />}
        {node.type === "condition" && <ConditionPanel node={node} doc={doc} set={set} />}
        {node.type === "set_var" && <SetVarPanel node={node} set={set} />}
        {node.type === "delay" && <DelayPanel node={node} set={set} />}
        {node.type === "tags" && <TagsPanel node={node} set={set} />}
        {node.type === "assign" && <AssignPanel node={node} set={set} />}
        {node.type === "chat_status" && <StatusPanel node={node} set={set} />}
        {node.type === "subscribe" && <SubscribePanel node={node} set={set} />}
        {node.type === "template" && <TemplatePanel node={node} set={set} />}
        {node.type === "jump" && <JumpPanel node={node} set={set} />}
        {node.type === "api_request" && <ApiPanel node={node} doc={doc} set={set} />}
        {node.type === "data" && <DataPanel node={node} doc={doc} set={set} />}
        {node.type === "ai_reply" && <AiReplyPanel node={node} set={set} />}
      </fieldset>

      {mine.length > 0 && (
        <div className="rounded-lg border border-black/5 bg-slate-50 p-2 space-y-1">
          {mine.map((i, n) => (
            <div key={n} className={`text-xs ${i.level === "fail" ? "text-rose-700" : "text-amber-700"}`}>
              {i.level === "fail" ? "✖" : "⚠"} {i.message}
            </div>
          ))}
        </div>
      )}

      {!readOnly && (
        <div className="flex flex-wrap gap-2 pt-1">
          {doc.start !== node.id && (
            <button className="btn-ghost text-xs" onClick={() => onMakeStart(node.id)}>Start here</button>
          )}
          <button className="btn-ghost text-xs" onClick={() => onDuplicate(node.id)} title="Copy this step (Ctrl+D)">
            Duplicate
          </button>
          <button className="btn-ghost text-xs text-rose-600 ml-auto" onClick={() => onDelete(node.id)}>Delete step</button>
        </div>
      )}
    </div>
    </NodeContext.Provider>
  );
}

function MediaPanel({ node, set, lang, sideBySide }: { node: WfNode; set: Set; lang: Lang; sideBySide: boolean }) {
  const media = node.media;
  return (
    <div className="space-y-2">
      <label className="block space-y-1">
        <span className="text-xs font-medium text-slate-600">Attach a file</span>
        <select className="input w-full" value={media?.type ?? ""}
          onChange={(e) => {
            const t = e.target.value as MediaType | "";
            set({ media: t ? { type: t, url: media?.url ?? "", caption: media?.caption } : undefined });
          }}>
          <option value="">No attachment</option>
          <option value="image">Picture</option>
          <option value="video">Video</option>
          <option value="document">Document (PDF, etc.)</option>
          <option value="audio">Voice note or audio</option>
        </select>
      </label>
      {media && (
        <>
          <label className="block space-y-1">
            <span className="text-xs font-medium text-slate-600">Public link to the file</span>
            <input className="input w-full font-mono text-xs" placeholder="https://…" value={media.url}
              onChange={(e) => set({ media: { ...media, url: e.target.value } })} />
            <span className="text-[11px] text-slate-400">
              WhatsApp fetches the file from this address, so it must be public and start with https://
            </span>
          </label>
          {media.type !== "audio" && (
            <Field label="Caption (optional)" path="media.caption" value={media.caption} onChange={(caption) => set({ media: { ...media, caption } })}
              lang={lang} max={LIMITS.body} rows={2} sideBySide={sideBySide} />
          )}
          {media.url && <MediaPreview type={media.type} url={media.url} />}
        </>
      )}
    </div>
  );
}

function QuestionPanel({ node, doc, lang, set, sideBySide }: {
  node: WfNode; doc: WorkflowDoc; lang: Lang; set: Set; sideBySide: boolean;
}) {
  const input = node.input ?? { kind: "buttons" as InputKind };
  const options = input.options ?? [];
  const fromData = input.kind === "data_list";
  const isList = input.kind === "list" || fromData;
  const choice = input.kind === "buttons" || input.kind === "list";
  const cap = isList ? LIMITS.listRows : LIMITS.buttons;
  const titleMax = isList ? LIMITS.rowTitle : LIMITS.buttonText;
  const setInput = (patch: Partial<typeof input>) => set({ input: { ...input, ...patch } });
  const setOption = (i: number, patch: Partial<WfOption>) =>
    setInput({ options: options.map((o, n) => (n === i ? { ...o, ...patch } : o)) });
  const headerKind = node.header_media?.type ?? "text";
  const rule = node.validate ?? { type: "any" as const };

  const active = presetOf(node);
  const [saved, setSaved] = useState<SavedQuestion[]>([]);
  const [naming, setNaming] = useState(false);
  const [saveName, setSaveName] = useState("");
  const [saveErr, setSaveErr] = useState("");
  useEffect(() => {
    api.questionPresets().then(setSaved).catch(() => setSaved([]));
  }, []);
  const saveOwn = async () => {
    setSaveErr("");
    try {
      const got = await api.saveQuestionPreset(saveName.trim(), questionSpec(node) as Record<string, unknown>);
      setSaved((s) => [...s, got]);
      setNaming(false);
    } catch (e) {
      setSaveErr((e as Error).message);
    }
  };
  const removeSaved = async (q: SavedQuestion) => {
    if (!confirm(`Delete your saved question “${q.label}”? Workflows that already use it keep their copy.`)) return;
    await api.deleteQuestionPreset(q.id);
    setSaved((s) => s.filter((x) => x.id !== q.id));
  };
  return (
    <>
      <div className="space-y-1">
        <span className="text-xs font-medium text-slate-600">What are you asking?</span>
        <div className="flex flex-wrap gap-1">
          {QUESTION_PRESETS.map((p) => (
            <button key={p.id} type="button" onClick={() => set(applyPreset(node, p))}
              className={`rounded-full border px-2 py-0.5 text-[11px] transition ${active === p.id
                ? "border-brand-400 bg-brand-50 text-brand-800" : "border-slate-200 bg-white hover:border-brand-300"}`}>
              {p.label}
            </button>
          ))}
          <button type="button" onClick={() => set(ownQuestion(node))}
            className="rounded-full border border-dashed border-slate-300 bg-white px-2 py-0.5 text-[11px] text-slate-700 hover:border-brand-400">
            ✎ Your own question
          </button>
          {saved.map((q) => (
            <span key={q.id} className="inline-flex items-center rounded-full border border-violet-200 bg-violet-50 text-[11px] text-violet-800">
              <button type="button" className="py-0.5 pl-2 pr-1" title="Your saved question" onClick={() => set(JSON.parse(JSON.stringify(q.spec)))}>
                ★ {q.label}
              </button>
              <button type="button" className="pr-1.5 text-violet-400 hover:text-rose-600" aria-label={`Delete saved question ${q.label}`}
                onClick={() => void removeSaved(q)}>×</button>
            </span>
          ))}
        </div>
        <div className="text-[11px] text-slate-400">
          Each sets the answer type, the check and a ready-written question in all three languages. Or write your own:
          type what to ask below, then pick how they answer and what counts as a right answer - including a check
          against your SO / PO data.
        </div>
        {naming ? (
          <div className="space-y-1">
            <div className="flex items-center gap-1">
              <input className="input !h-7 flex-1 min-w-0 text-xs" placeholder="Name it, e.g. Order number" value={saveName}
                maxLength={40} autoFocus aria-label="Name for your saved question" onChange={(e) => setSaveName(e.target.value)}
                onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); void saveOwn(); } }} />
              <button type="button" className="btn-primary !h-7 text-xs" disabled={!saveName.trim()} onClick={() => void saveOwn()}>Save</button>
              <button type="button" className="btn-ghost !h-7 text-xs" onClick={() => setNaming(false)}>Cancel</button>
            </div>
            {saveErr && <div className="text-[11px] text-rose-600">{saveErr}</div>}
          </div>
        ) : (
          <button type="button" className="text-[11px] text-brand-700 hover:underline"
            onClick={() => { setNaming(true); setSaveName(""); setSaveErr(""); }}>
            ☆ Save this question to reuse it in any workflow
          </button>
        )}
      </div>
      <label className="block space-y-1">
        <span className="text-xs font-medium text-slate-600">How they answer</span>
        <select className="input w-full" value={input.kind} onChange={(e) => setInput({ kind: e.target.value as InputKind })}>
          <option value="buttons">Tap buttons (up to 3)</option>
          <option value="list">A list (up to 10 rows)</option>
          <option value="text">They type it</option>
          <option value="language">Choose a language</option>
          <option value="data_list">A list from your data (their own orders)</option>
        </select>
      </label>

      {input.kind === "language" && <LanguageButtons input={input} setInput={setInput} />}
      {fromData && <DataListPanel node={node} doc={doc} input={input} setInput={setInput} lang={lang} sideBySide={sideBySide} />}

      {(choice || input.kind === "language" || fromData) && (
        <div className="space-y-2 rounded-lg border border-slate-200 p-2">
          <div className="text-xs font-medium text-slate-600">Around the message</div>
          {!isList && input.kind !== "language" && (
            <label className="block space-y-1">
              <span className="text-[11px] text-slate-500">Header</span>
              <select className="input w-full text-xs" value={headerKind}
                onChange={(e) => {
                  const t = e.target.value;
                  set(t === "text" ? { header_media: undefined } : { header_media: { type: t as MediaType, url: node.header_media?.url ?? "" } });
                }}>
                <option value="text">Text (or none)</option>
                <option value="image">Picture</option>
                <option value="video">Video</option>
                <option value="document">Document</option>
              </select>
            </label>
          )}
          {headerKind === "text" || isList ? (
            <Field label="Header text (optional)" path="header" value={node.header} onChange={(header) => set({ header })}
              lang={lang} max={LIMITS.header} sideBySide={sideBySide} />
          ) : (
            <label className="block space-y-1">
              <span className="text-[11px] text-slate-500">Public link to the file</span>
              <input className="input w-full font-mono text-xs" placeholder="https://…" value={node.header_media?.url ?? ""}
                onChange={(e) => set({ header_media: { type: headerKind as MediaType, url: e.target.value } })} />
            </label>
          )}
          <Field label="Footer (optional)" path="footer" value={node.footer} onChange={(footer) => set({ footer })}
            lang={lang} max={LIMITS.footer} sideBySide={sideBySide} />
        </div>
      )}

      {choice && (
        <div className="space-y-2">
          {isList && (
            <Field label="List button" path="input.button_text" value={input.button_text} onChange={(button_text) => setInput({ button_text })}
              lang={lang} max={LIMITS.listButton} sideBySide={sideBySide} />
          )}
          {options.map((o, i) => (
            <div key={i} className="rounded-lg border border-slate-200 p-2 space-y-2">
              <div className="flex items-center gap-2">
                <span className="text-[11px] text-slate-400">{isList ? "Row" : "Button"} {i + 1}</span>
                <input className="input text-[11px] w-24 ml-auto font-mono" value={o.value}
                  onChange={(e) => setOption(i, { value: e.target.value })} title="Internal id, never shown" />
                <button className="btn-ghost text-[11px] text-rose-600"
                  onClick={() => setInput({ options: options.filter((_, n) => n !== i) })}>✕</button>
              </div>
              <Field label={isList ? "Row title" : "Button text"} path={`input.options.${i}.label`} value={o.label} onChange={(label) => setOption(i, { label })}
                lang={lang} max={titleMax} sideBySide={sideBySide} />
              {isList && (
                <>
                  <Field label="Description (optional)" path={`input.options.${i}.description`} value={o.description} onChange={(description) => setOption(i, { description })}
                    lang={lang} max={LIMITS.rowDesc} sideBySide={sideBySide} />
                  <Field label="Section (optional)" path={`input.options.${i}.section`} value={o.section} onChange={(section) => setOption(i, { section })}
                    lang={lang} max={LIMITS.sectionTitle} sideBySide={sideBySide}
                    hint="Rows with the same section title are grouped under it." />
                </>
              )}
            </div>
          ))}
          <button className="btn-ghost text-xs" disabled={options.length >= cap}
            title={options.length >= cap ? `WhatsApp allows at most ${cap} here` : ""}
            onClick={() => setInput({ options: [...options, { value: `o${Date.now().toString(36)}`, label: { en: "" } }] })}>
            + Add {isList ? "row" : "button"}{options.length >= cap ? ` (max ${cap})` : ""}
          </button>
          <div className="text-[11px] text-slate-500 bg-slate-50 rounded p-2">
            <b>Anything else</b>: connect this exit to choose what happens when the customer types instead of
            tapping. Left unconnected, the question simply asks again.
          </div>
          <Preview node={node} lang={lang} />
        </div>
      )}

      {input.kind === "language" && <Preview node={node} lang={lang} />}

      {input.kind === "text" && (
        <div className="space-y-2">
          <label className="block space-y-1">
            <span className="text-xs font-medium text-slate-600">Accept</span>
            <select className="input w-full" value={rule.type}
              onChange={(e) => set({ validate: { type: e.target.value as "any" } })}>
              <option value="any">Anything they type</option>
              <option value="number">A number</option>
              <option value="email">An email address</option>
              <option value="phone">A phone number</option>
              <option value="date">A date (like 25/12/2026)</option>
              <option value="url">A website address</option>
              <option value="time">A time (like 11:30 or 4 pm)</option>
              <option value="file">A photo, document or video they send</option>
              <option value="location">Their location, shared from WhatsApp</option>
              <option value="lookup">Checked against your data (SO, PO, item, customer code)</option>
              <option value="regex">Text matching a pattern</option>
            </select>
          </label>
          {rule.type === "number" && (
            <div className="flex gap-2">
              <input className="input text-xs w-1/2" placeholder="At least (optional)" value={rule.min ?? ""}
                onChange={(e) => set({ validate: { ...rule, min: e.target.value } })} />
              <input className="input text-xs w-1/2" placeholder="At most (optional)" value={rule.max ?? ""}
                onChange={(e) => set({ validate: { ...rule, max: e.target.value } })} />
            </div>
          )}
          {rule.type === "regex" && (
            <label className="block space-y-1">
              <input className="input w-full font-mono text-xs" placeholder="e.g. [0-9]{6} for a PIN code" value={rule.pattern ?? ""}
                onChange={(e) => set({ validate: { ...rule, pattern: e.target.value } })} />
              <span className="text-[11px] text-slate-400">The whole answer must match. Checked before you can publish.</span>
            </label>
          )}
          {rule.type === "lookup" && (
            <div className="space-y-1 rounded-lg border border-sky-200 bg-sky-50 p-2">
              <select className="input w-full text-xs" value={rule.source ?? ""} aria-label="What to check the answer against"
                onChange={(e) => set({ validate: { ...rule, source: (e.target.value || undefined) as LookupSource | undefined } })}>
                <option value="">Check it against…</option>
                {LOOKUP_SOURCES.map((s) => <option key={s.value} value={s.value}>{s.label}</option>)}
              </select>
              <div className="text-[11px] text-sky-900">
                Only this customer's own orders are searched - never another company's. If it is not found they are
                asked again, or it takes <b>Not valid</b> when you connect that exit.
                {node.store && rule.source
                  ? <> Later steps can then write {answerExtras(node).map((v) => `{${v}}`).join(" ")}.</>
                  : <> Name the answer below to use what was found, e.g. <span className="font-mono">order</span> gives {"{order_status}"}.</>}
              </div>
            </div>
          )}
          {(rule.type === "file" || rule.type === "location") && (
            <div className="text-[11px] text-slate-500 bg-slate-50 rounded p-2">
              {rule.type === "file"
                ? "The customer sends a photo, document or video on WhatsApp; the answer saved is its link."
                : "The customer shares a location on WhatsApp (📎, then Location); the answer saved is a Google Maps link."}
              {node.store && <> Also saved: {answerExtras(node).map((v) => `{${v}}`).join(" ")}.</>}
            </div>
          )}
        </div>
      )}

      <label className="block space-y-1">
        <span className="text-xs font-medium text-slate-600">Remember the answer as</span>
        <input className="input w-full font-mono" value={node.store ?? ""} placeholder="company_name"
          onChange={(e) => set({ store: e.target.value })} />
        <span className="text-[11px] text-slate-400">
          {node.store ? `Later steps can write {${node.store}}` : "Leave blank to discard the answer"}
        </span>
      </label>

      <label className="block space-y-1">
        <span className="text-xs font-medium text-slate-600">If the answer does not fit</span>
        <select className="input w-full" value={node.max_retries ?? 0}
          onChange={(e) => set({ max_retries: Number(e.target.value) })}>
          <option value={0}>Keep asking</option>
          {[1, 2, 3, 4, 5].map((n) => (
            <option key={n} value={n}>Ask {n === 1 ? "once more" : `${n} more times`}, then take “Gave up”</option>
          ))}
        </select>
      </label>
      {input.kind === "text" && rule.type !== "any" && (
        <div className="text-[11px] text-slate-400">
          Connect <b>Not valid</b> to decide where a wrong answer goes: straight away with <b>Keep asking</b>, or
          once the tries run out if <b>Gave up</b> is not connected.
        </div>
      )}
      <Field label="What to say when it does not fit" path="invalid_text" value={node.invalid_text}
        onChange={(invalid_text) => set({ invalid_text })} lang={lang} max={LIMITS.body} rows={2} sideBySide={sideBySide} />
    </>
  );
}

function ProductListPanel({ node, set, lang, sideBySide }: { node: WfNode; set: Set; lang: Lang; sideBySide: boolean }) {
  return (
    <div className="space-y-2">
      <Field label="Header" path="header" value={node.header} onChange={(header) => set({ header })}
        lang={lang} max={LIMITS.header} sideBySide={sideBySide} />
      <Field label="Message" path="text" value={node.text} onChange={(text) => set({ text })}
        lang={lang} max={LIMITS.body} rows={3} sideBySide={sideBySide} />
      <label className="block space-y-1">
        <span className="text-xs font-medium text-slate-600">Catalogue id</span>
        <input className="input w-full font-mono text-xs" value={node.catalog_id ?? ""} onChange={(e) => set({ catalog_id: e.target.value })} />
      </label>
      <label className="block space-y-1">
        <span className="text-xs font-medium text-slate-600">Product set id</span>
        <input className="input w-full font-mono text-xs" value={node.set_id ?? ""} onChange={(e) => set({ set_id: e.target.value })} />
      </label>
      <div className="text-[11px] text-slate-500">
        These come from the catalogue connected to your WhatsApp Business account (Meta Commerce Manager,
        linked in WATI). Without them WhatsApp cannot show the products.
      </div>
    </div>
  );
}

function UnsupportedNote({ node }: { node: WfNode }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="rounded-lg border border-rose-200 bg-rose-50 p-2 text-xs text-rose-800 space-y-1">
      <div>
        <b>This step came from WATI as “{node.unsupported}”</b>, which the builder cannot run yet. Its settings
        are kept so nothing is lost. Rebuild it with the steps on the left, then delete this one.
      </div>
      <button className="underline" onClick={() => setOpen((v) => !v)}>{open ? "Hide" : "Show"} the original WATI settings</button>
      {open && (
        <pre className="max-h-48 overflow-auto bg-white/70 rounded p-1.5 text-[10px] text-slate-700 whitespace-pre-wrap break-all">
          {JSON.stringify(node.wati_raw, null, 2)}
        </pre>
      )}
    </div>
  );
}

function Preview({ node, lang }: { node: WfNode; lang: Lang }) {
  const input = node.input;
  if (!input || !["buttons", "list", "language"].includes(input.kind)) return null;
  const items = input.kind === "language"
    ? offeredLanguages(input).map((c) => ({ title: input.language_labels?.[c]?.trim() || DEFAULT_LANGUAGE_LABELS[c], description: "" }))
    : (input.options ?? []).map((o) => ({ title: textOf(o.label, lang) || "…", description: textOf(o.description, lang) }));
  if (!items.length) return null;
  const header = textOf(node.header, lang);
  const footer = textOf(node.footer, lang);
  return (
    <div className="rounded-lg bg-[#efeae2] p-2">
      <div className="text-[11px] text-slate-500 mb-1">What the customer sees</div>
      <div className="bg-white rounded-lg px-2.5 py-2 shadow-sm space-y-1">
        {node.header_media?.url && <MediaPreview type={node.header_media.type} url={node.header_media.url} compact />}
        {header && <div className="text-sm font-semibold"><WaText text={header} /></div>}
        <div className="text-sm"><WaText text={textOf(node.text, lang) || "…"} /></div>
        {footer && <div className="text-[11px] text-slate-400">{footer}</div>}
        <MenuPreview options={{ kind: input.kind === "list" ? "list" : "buttons", items, button_text: textOf(input.button_text, lang) || "Menu", section_title: textOf(input.section_title, lang), header: "", footer: "" }} compact />
      </div>
    </div>
  );
}

function ConditionPanel({ node, doc, set }: { node: WfNode; doc: WorkflowDoc; set: Set }) {
  const branches = node.branches ?? [];
  const vars = variablesIn(doc);
  const setBranch = (i: number, patch: Partial<(typeof branches)[0]>) =>
    set({ branches: branches.map((b, n) => (n === i ? { ...b, ...patch } : b)) });

  return (
    <div className="space-y-2">
      {branches.map((b, i) => (
        <div key={b.id} className="rounded-lg border border-slate-200 p-2 space-y-1.5">
          <div className="flex items-center gap-2">
            <input className="input text-xs flex-1" value={b.label} placeholder="Name this path"
              onChange={(e) => setBranch(i, { label: e.target.value })} />
            <button className="btn-ghost text-[11px] text-rose-600"
              onClick={() => set({ branches: branches.filter((_, n) => n !== i) })}>✕</button>
          </div>
          <div className="flex gap-1">
            <select className="input text-xs flex-1" value={b.when.var}
              onChange={(e) => setBranch(i, { when: { ...b.when, var: e.target.value } })}>
              <option value="">choose an answer…</option>
              {vars.map((v) => <option key={v} value={v}>{v}</option>)}
              <option value="sys.language">sys.language</option>
              <option value="sys.customer_name">sys.customer_name</option>
              <option value="sys.time">sys.time (now, like 14:30)</option>
              <option value="sys.hour">sys.hour (0-23)</option>
              <option value="sys.weekday">sys.weekday (Mon … Sun)</option>
              <option value="sys.date">sys.date (DD/MM/YYYY)</option>
            </select>
            <select className="input text-xs" value={b.when.op}
              onChange={(e) => setBranch(i, { when: { ...b.when, op: e.target.value as CondOp } })}>
              <option value="eq">is</option>
              <option value="ne">is not</option>
              <option value="contains">contains</option>
              <option value="starts_with">starts with</option>
              <option value="ends_with">ends with</option>
              <option value="gte">is at least</option>
              <option value="lte">is at most</option>
              <option value="gt">is more than</option>
              <option value="lt">is less than</option>
              <option value="between">is between</option>
              <option value="is_set">is answered</option>
              <option value="is_empty">is blank</option>
            </select>
          </div>
          {b.when.op !== "is_set" && b.when.op !== "is_empty" && (
            <input className="input text-xs w-full" value={b.when.value ?? ""} placeholder={b.when.op === "between" ? "10:00-19:00" : "value"}
              onChange={(e) => setBranch(i, { when: { ...b.when, value: e.target.value } })} />
          )}
        </div>
      ))}
      <button className="btn-ghost text-xs"
        onClick={() => set({ branches: [...branches, { id: `b${Date.now().toString(36)}`, label: `Rule ${branches.length + 1}`, when: { var: "", op: "eq", value: "" } }] })}>
        + Add a rule
      </button>
      <div className="text-[11px] text-slate-500">
        Rules are tried top to bottom. Anything that matches none of them takes <b>Otherwise</b>. For working
        hours: <span className="font-mono">sys.time</span> is between <span className="font-mono">10:00-19:00</span> (Indian time),
        or <span className="font-mono">sys.weekday</span> is <span className="font-mono">Sun</span>.
      </div>
    </div>
  );
}

function SetVarPanel({ node, set }: { node: WfNode; set: Set }) {
  const assign = node.assign ?? {};
  const rows = Object.entries(assign);
  const write = (next: [string, string][]) => set({ assign: Object.fromEntries(next.filter(([k]) => k)) });
  return (
    <div className="space-y-2">
      {rows.map(([k, v], i) => (
        <div key={i} className="flex gap-1">
          <input className="input text-xs w-1/3 font-mono" value={k} placeholder="name"
            onChange={(e) => write(rows.map((r, n) => (n === i ? [e.target.value, r[1]] : r)))} />
          <input className="input text-xs flex-1" value={v} placeholder="value"
            onChange={(e) => write(rows.map((r, n) => (n === i ? [r[0], e.target.value] : r)))} />
          <button className="btn-ghost text-[11px] text-rose-600"
            onClick={() => write(rows.filter((_, n) => n !== i))}>✕</button>
        </div>
      ))}
      <button className="btn-ghost text-xs" onClick={() => write([...rows, ["", ""]])}>+ Remember something</button>
      <label className="flex items-start gap-2 text-[11px] text-slate-600">
        <input type="checkbox" className="mt-0.5" checked={!!node.to_contact} onChange={(e) => set({ to_contact: e.target.checked })} />
        <span>Also save these on the customer's WATI contact (WATI's “Update attribute”), so broadcasts and filters in WATI can use them.</span>
      </label>
    </div>
  );
}

function placeholderHint(doc: WorkflowDoc): string {
  const vars = [...variablesIn(doc), "sys.customer_name"];
  return `You can use ${vars.map((v) => `{${v}}`).join(", ")}. *bold*, _italic_ and ~strike~ work as on WhatsApp.`;
}

function countMissing(node: WfNode): { en: number; hi: number; gu: number; total: number } {
  const out = { en: 0, hi: 0, gu: 0, total: 0 };
  // `required`: the customer always sees it, so English cannot be blank
  const check = (v: I18n | undefined, required = false) => {
    if (v === undefined) return;
    out.total += 1;
    if (!(v.en ?? "").trim()) {
      if (required) out.en += 1;
      return;
    }
    for (const lg of LANGS) if (lg !== "en" && !(v[lg] ?? "").trim()) out[lg as "hi" | "gu"] += 1;
  };
  const pictureOnly = !!node.media?.url;
  check(node.text, (node.type === "message" && !pictureOnly) || node.type === "question" || node.type === "product_list");
  check(node.header);
  check(node.footer);
  check(node.media?.caption);
  check(node.invalid_text);
  check(node.retry_text);
  check(node.input?.button_text);
  check(node.input?.section_title);
  for (const o of node.input?.options ?? []) {
    check(o.label, true);
    check(o.description);
    check(o.section);
  }
  return out;
}

export { LANG_NAME };

/** The language question's buttons: which languages to offer, and what each button says. */
function LanguageButtons({ input, setInput }: { input: WfInput; setInput: (p: Partial<WfInput>) => void }) {
  const offered = offeredLanguages(input);
  const labels = input.language_labels ?? {};
  return (
    <div className="space-y-2 rounded-lg border border-slate-200 p-2">
      <div className="text-xs font-medium text-slate-600">The language buttons</div>
      {LANGS.map((code) => {
        const on = offered.includes(code);
        const shown = (labels[code] ?? "").trim() || DEFAULT_LANGUAGE_LABELS[code];
        const long = shown.length > LIMITS.buttonText;
        return (
          <div key={code} className="flex items-center gap-2">
            <input type="checkbox" checked={on} disabled={on && offered.length <= 2}
              aria-label={`Offer ${LANGUAGE_NAMES[code]}`}
              title={on && offered.length <= 2 ? "A language question needs at least two languages" : ""}
              onChange={(e) => setInput({ languages: LANGS.filter((c) => (c === code ? e.target.checked : offered.includes(c))) })} />
            <span className="w-14 shrink-0 text-[11px] text-slate-500">{LANGUAGE_NAMES[code]}</span>
            <input className={`input flex-1 min-w-0 ${long ? "border-rose-400" : ""}`} disabled={!on}
              value={labels[code] ?? ""} placeholder={DEFAULT_LANGUAGE_LABELS[code]}
              aria-label={`${LANGUAGE_NAMES[code]} button text`}
              onChange={(e) => setInput({ language_labels: { ...labels, [code]: e.target.value } })} />
            <span className={`w-10 shrink-0 text-right text-[11px] ${long ? "text-rose-600 font-medium" : "text-slate-400"}`}>
              {shown.length}/{LIMITS.buttonText}
            </span>
          </div>
        );
      })}
      <div className="text-[11px] text-slate-500">
        A language button reads the same for everyone, so write each in its own script (“हिंदी”, “ગુજરાતી”) and
        customers find theirs at a glance. Left blank, the wording from Bot messages is used. Whichever they tap
        becomes the language for the rest of the conversation.
      </div>
    </div>
  );
}

/** A question whose rows are whatever a Find-in-your-data step found for this customer. */
function DataListPanel({ node, doc, input, setInput, lang, sideBySide }: {
  node: WfNode; doc: WorkflowDoc; input: WfInput; setInput: (p: Partial<WfInput>) => void;
  lang: Lang; sideBySide: boolean;
}) {
  const info = useDataFields();
  const steps = doc.nodes.filter((n) => n.type === "data" && (n.store ?? "").trim());
  const chosen = steps.find((n) => n.store === input.from);
  const fields = fieldsOf(info, chosen?.source);
  const pick = (label: string, key: "title_field" | "description_field" | "section_field", hint: string) => (
    <label className="block space-y-1">
      <span className="text-[11px] text-slate-500">{label}</span>
      <select className="input w-full text-xs" value={input[key] ?? ""} aria-label={label}
        onChange={(e) => setInput({ [key]: e.target.value })}>
        <option value="">{hint}</option>
        {fields.map((f) => <option key={f.name} value={f.name}>{f.label}</option>)}
      </select>
    </label>
  );
  return (
    <div className="space-y-2 rounded-lg border border-slate-200 p-2">
      <label className="block space-y-1">
        <span className="text-xs font-medium text-slate-600">Show the rows from</span>
        <select className="input w-full" value={input.from ?? ""} aria-label="Show the rows from"
          onChange={(e) => setInput({ from: e.target.value })}>
          <option value="">Choose a Find-in-your-data step…</option>
          {steps.map((n) => <option key={n.id} value={n.store}>{n.title || n.store} ({n.store})</option>)}
        </select>
      </label>
      {steps.length === 0 && (
        <div className="rounded-md border border-amber-200 bg-amber-50 px-2 py-1.5 text-[11px] text-amber-900">
          Add a <b>Find in your data</b> step before this question - it is what finds the rows to show.
        </div>
      )}
      <label className="block space-y-1">
        <span className="text-[11px] text-slate-500">Show them as</span>
        <select className="input w-full text-xs" value={input.show ?? "auto"} aria-label="Show them as"
          onChange={(e) => setInput({ show: e.target.value as "auto" })}>
          <option value="auto">Buttons for up to 3, a list beyond that</option>
          <option value="list">Always a list</option>
          <option value="buttons">Always buttons (only the first 3)</option>
        </select>
      </label>
      {pick("Each row reads", "title_field", "Choose a field…")}
      {pick("Smaller line under it (optional)", "description_field", "Nothing")}
      {pick("Group rows under (optional)", "section_field", "No grouping")}
      {(input.show ?? "auto") !== "list" && (input.description_field ?? "") && (
        <div className="text-[11px] text-amber-800">
          Buttons show the title only, so the smaller line is not shown when there are three or fewer rows.
        </div>
      )}
      <Field label="List button" path="input.button_text" value={input.button_text}
        onChange={(button_text) => setInput({ button_text })} lang={lang} max={LIMITS.listButton} sideBySide={sideBySide} />
      <div className="text-[11px] text-slate-500">
        The rows are this customer's own orders, so they are only known while they are talking - which is why there
        is one exit for the row they pick, not one per row. What they pick is checked against your data again before
        it is used, so the status they see is the current one. Give the question a name below (e.g.{" "}
        <span className="font-mono">order</span>) and later steps can say{" "}
        <span className="font-mono">{node.store ? `{${node.store}_status}` : "{order_status}"}</span>.
      </div>
    </div>
  );
}
