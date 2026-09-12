import { useEffect, useState } from "react";
import { api } from "../api";
import {
  AI_FAQ_MAX, BUILTIN_ORDER_STATUS, CHAT_STATUSES, DATA_FIELDS, DATA_LIST_LINE, DATA_ROWS_MAX, DataFieldsInfo,
  WfNode, WorkflowDoc, WorkflowSummary, variablesIn, DELAY_MAX_SEC,
} from "./types";

export type Set = (p: Partial<WfNode>) => void;

/** A repeated list of plain strings — tags, team names. In one place because three panels need
 *  exactly this, and each hand-rolling its own is how they drift apart. */
function StringList({ values, onChange, placeholder, addLabel }: {
  values: string[]; onChange: (v: string[]) => void; placeholder: string; addLabel: string;
}) {
  return (
    <div className="space-y-1">
      {values.map((v, i) => (
        <div key={i} className="flex gap-1">
          <input className="input text-xs flex-1" value={v} placeholder={placeholder}
            onChange={(e) => onChange(values.map((x, n) => (n === i ? e.target.value : x)))} />
          <button className="btn-ghost text-[11px] text-rose-600"
            onClick={() => onChange(values.filter((_, n) => n !== i))}>✕</button>
        </div>
      ))}
      <button className="btn-ghost text-xs" onClick={() => onChange([...values, ""])}>{addLabel}</button>
    </div>
  );
}

/** Key/value rows, used for headers, saved values and template parameters. */
function PairList({ pairs, onChange, keyPlaceholder, valuePlaceholder, addLabel, mono }: {
  pairs: [string, string][]; onChange: (p: [string, string][]) => void;
  keyPlaceholder: string; valuePlaceholder: string; addLabel: string; mono?: boolean;
}) {
  return (
    <div className="space-y-1">
      {pairs.map(([k, v], i) => (
        <div key={i} className="flex gap-1">
          <input className="input text-xs w-1/3 font-mono" value={k} placeholder={keyPlaceholder}
            onChange={(e) => onChange(pairs.map((r, n) => (n === i ? [e.target.value, r[1]] : r)))} />
          <input className={`input text-xs flex-1 ${mono ? "font-mono" : ""}`} value={v} placeholder={valuePlaceholder}
            onChange={(e) => onChange(pairs.map((r, n) => (n === i ? [r[0], e.target.value] : r)))} />
          <button className="btn-ghost text-[11px] text-rose-600"
            onClick={() => onChange(pairs.filter((_, n) => n !== i))}>✕</button>
        </div>
      ))}
      <button className="btn-ghost text-xs" onClick={() => onChange([...pairs, ["", ""]])}>{addLabel}</button>
    </div>
  );
}

export function DelayPanel({ node, set }: { node: WfNode; set: Set }) {
  const secs = node.seconds ?? 3;
  return (
    <div className="space-y-2">
      <label className="block space-y-1">
        <span className="text-xs font-medium text-slate-600">Wait before the next step</span>
        <div className="flex items-center gap-2">
          <input type="number" min={1} max={DELAY_MAX_SEC} className="input w-24" value={secs}
            onChange={(e) => set({ seconds: Number(e.target.value) })} />
          <span className="text-xs text-slate-500">seconds</span>
        </div>
      </label>
      <div className="flex flex-wrap gap-1">
        {[1, 3, 10, 60, 300].map((n) => (
          <button key={n} className="btn-ghost text-[11px]" onClick={() => set({ seconds: n })}>
            {n < 60 ? `${n}s` : `${n / 60}m`}
          </button>
        ))}
      </div>
      <div className="text-[11px] text-slate-500">
        A short pause makes a run of messages feel less robotic. The longest allowed is{" "}
        {DELAY_MAX_SEC / 60} minutes — a customer waiting longer assumes the bot is broken.
      </div>
    </div>
  );
}

export function TagsPanel({ node, set }: { node: WfNode; set: Set }) {
  return (
    <div className="space-y-2">
      <label className="flex items-center gap-2 text-xs">
        <input type="checkbox" checked={!node.remove} onChange={(e) => set({ remove: !e.target.checked })} />
        Add these tags (untick to remove them instead)
      </label>
      <StringList values={node.tags ?? []} onChange={(tags) => set({ tags })}
        placeholder="e.g. new-lead" addLabel="+ Add a tag" />
      <div className="text-[11px] text-slate-500">
        Tags show on the chat in WATI, so your team can filter by them. A tag can use an answer, e.g.{" "}
        <span className="font-mono">{"{company}"}</span>.
      </div>
    </div>
  );
}

export function AssignPanel({ node, set }: { node: WfNode; set: Set }) {
  const to = node.to ?? "team";
  return (
    <div className="space-y-2">
      <label className="block space-y-1">
        <span className="text-xs font-medium text-slate-600">Give the chat to</span>
        <select className="input w-full" value={to} onChange={(e) => set({ to: e.target.value as "team" })}>
          <option value="team">A team</option>
          <option value="operator">One person</option>
          <option value="bot">Back to the bot</option>
        </select>
      </label>
      {to === "operator" && (
        <label className="block space-y-1">
          <span className="text-xs font-medium text-slate-600">Their WATI email address</span>
          <input className="input w-full font-mono" value={node.email ?? ""} placeholder="sales@yourcompany.com"
            onChange={(e) => set({ email: e.target.value })} />
        </label>
      )}
      {to === "team" && (
        <div className="space-y-1">
          <span className="text-xs font-medium text-slate-600">Team name, exactly as in WATI</span>
          <StringList values={node.teams ?? []} onChange={(teams) => set({ teams })}
            placeholder="e.g. Sales" addLabel="+ Add a team" />
        </div>
      )}
      {to !== "bot" && (
        <div className="rounded-md border border-sky-200 bg-sky-50 px-2 py-1.5 text-[11px] text-sky-900">
          Once a person has the chat the bot goes quiet for this customer, so nothing after this step runs - put
          any message before it. The bot takes the chat back when you click <b>Hand back to bot</b> in Live
          sessions, or after the silence set in Settings.
        </div>
      )}
      <div className="text-[11px] text-slate-500">
        WATI identifies a person by email and a team by name, and gives no way to list either — so
        these are typed and spelling matters. Assigning is a WATI Pro/Business feature; on a lower
        plan the chat still lands in WATI&apos;s Unassigned queue.
      </div>
    </div>
  );
}

export function StatusPanel({ node, set }: { node: WfNode; set: Set }) {
  return (
    <label className="block space-y-1">
      <span className="text-xs font-medium text-slate-600">Mark the conversation</span>
      <select className="input w-full" value={node.status ?? "pending"} onChange={(e) => set({ status: e.target.value })}>
        {CHAT_STATUSES.map((s) => <option key={s} value={s}>{s}</option>)}
      </select>
      <span className="text-[11px] text-slate-500">
        Anything other than <span className="font-mono">block</span> only works while the customer has
        written within the last 24 hours.
      </span>
    </label>
  );
}

export function SubscribePanel({ node, set }: { node: WfNode; set: Set }) {
  return (
    <label className="flex items-center gap-2 text-xs">
      <input type="checkbox" checked={node.subscribe !== false}
        onChange={(e) => set({ subscribe: e.target.checked })} />
      Subscribe this contact to campaigns (untick to unsubscribe them)
    </label>
  );
}

export function TemplatePanel({ node, set }: { node: WfNode; set: Set }) {
  const params = Object.entries(node.params ?? {}) as [string, string][];
  return (
    <div className="space-y-2">
      <label className="block space-y-1">
        <span className="text-xs font-medium text-slate-600">Approved template name</span>
        <input className="input w-full font-mono" value={node.template_name ?? ""} placeholder="order_ready"
          onChange={(e) => set({ template_name: e.target.value })} />
        <span className="text-[11px] text-slate-500">
          The name from the WhatsApp templates page. Meta has to have approved it first.
        </span>
      </label>
      <span className="text-xs font-medium text-slate-600">Values to fill in</span>
      <PairList pairs={params} keyPlaceholder="name" valuePlaceholder="{company}" addLabel="+ Add a value"
        onChange={(next) => set({ params: Object.fromEntries(next.filter(([k]) => k)) })} />
    </div>
  );
}

export function JumpPanel({ node, set }: { node: WfNode; set: Set }) {
  const [workflows, setWorkflows] = useState<WorkflowSummary[] | null>(null);
  useEffect(() => {
    api.workflows().then(setWorkflows).catch(() => setWorkflows([]));
  }, []);
  const current = node.workflow ?? "";
  const known = !current || current === BUILTIN_ORDER_STATUS || !workflows || workflows.some((w) => w.key === current);
  return (
    <label className="block space-y-1">
      <span className="text-xs font-medium text-slate-600">Hand the customer to</span>
      <select className="input w-full" value={current} onChange={(e) => set({ workflow: e.target.value })}>
        <option value="">Choose…</option>
        <option value={BUILTIN_ORDER_STATUS}>The order-status bot, at its main menu</option>
        {(workflows ?? []).map((w) => (
          <option key={w.key} value={w.key}>{w.title}{w.live ? "" : " (not live)"}</option>
        ))}
        {!known && <option value={current}>{current} (no longer exists)</option>}
      </select>
      <span className="text-[11px] text-slate-500">
        The conversation leaves this workflow here, so nothing follows this step. The answers collected so far go
        with the customer. If the chosen workflow is not live, they get the order-status main menu instead.
      </span>
    </label>
  );
}

export function ApiPanel({ node, doc, set }: { node: WfNode; doc: WorkflowDoc; set: Set }) {
  const headers = Object.entries(node.headers ?? {}) as [string, string][];
  const saves = Object.entries(node.save ?? {}) as [string, string][];
  const routes = node.routes ?? [];
  const known = variablesIn(doc);
  return (
    <div className="space-y-2">
      <div className="flex gap-1">
        <select className="input text-xs w-20" value={node.method ?? "GET"}
          onChange={(e) => set({ method: e.target.value as "GET" })}>
          <option>GET</option>
          <option>POST</option>
        </select>
        <input className="input text-xs flex-1 font-mono" value={node.url ?? ""}
          placeholder="https://your-system.com/api/quote" onChange={(e) => set({ url: e.target.value })} />
      </div>

      <span className="text-xs font-medium text-slate-600">Headers</span>
      <PairList pairs={headers} keyPlaceholder="Authorization" valuePlaceholder="Bearer …" mono
        addLabel="+ Add a header"
        onChange={(next) => set({ headers: Object.fromEntries(next.filter(([k]) => k)) })} />

      {node.method === "POST" && (
        <label className="block space-y-1">
          <span className="text-xs font-medium text-slate-600">Body (JSON)</span>
          <textarea className="input w-full font-mono text-xs" rows={3} value={node.body ?? ""}
            placeholder={'{"company": "{company}"}'} onChange={(e) => set({ body: e.target.value })} />
          <span className="text-[11px] text-slate-400">Put placeholders inside the quotes.</span>
        </label>
      )}

      <span className="text-xs font-medium text-slate-600">Remember from the answer</span>
      <PairList pairs={saves} keyPlaceholder="quote_id" valuePlaceholder="result.quoteId" mono
        addLabel="+ Remember a value"
        onChange={(next) => set({ save: Object.fromEntries(next.filter(([k]) => k)) })} />
      <div className="text-[11px] text-slate-500">
        Use a dot path for something nested (<span className="font-mono">result.quoteId</span>) and an
        index for a list (<span className="font-mono">$.items[0].status</span>).
      </div>

      <span className="text-xs font-medium text-slate-600">Extra paths out</span>
      {routes.map((r, i) => (
        <div key={r.id} className="rounded-lg border border-slate-200 p-2 space-y-1">
          <div className="flex gap-1">
            <input className="input text-xs flex-1" value={r.label} placeholder="Name this path"
              onChange={(e) => set({ routes: routes.map((x, n) => (n === i ? { ...x, label: e.target.value } : x)) })} />
            <button className="btn-ghost text-[11px] text-rose-600"
              onClick={() => set({ routes: routes.filter((_, n) => n !== i) })}>✕</button>
          </div>
          <div className="flex gap-1">
            <input className="input text-xs flex-1 font-mono" value={r.path} placeholder="stock"
              onChange={(e) => set({ routes: routes.map((x, n) => (n === i ? { ...x, path: e.target.value } : x)) })} />
            <select className="input text-xs" value={r.op}
              onChange={(e) => set({ routes: routes.map((x, n) => (n === i ? { ...x, op: e.target.value } : x)) })}>
              <option value="eq">is</option>
              <option value="ne">is not</option>
              <option value="contains">contains</option>
              <option value="is_empty">is blank</option>
            </select>
            <input className="input text-xs w-20" value={r.value} placeholder="0"
              onChange={(e) => set({ routes: routes.map((x, n) => (n === i ? { ...x, value: e.target.value } : x)) })} />
          </div>
        </div>
      ))}
      <button className="btn-ghost text-xs"
        onClick={() => set({ routes: [...routes, { id: `r${routes.length + 1}`, label: `Path ${routes.length + 1}`, path: "", op: "eq", value: "" }] })}>
        + Add a path
      </button>
      <div className="text-[11px] text-slate-500">
        <b>Worked</b> and <b>Failed</b> must both lead somewhere: if the other system is down the
        customer still needs an answer.
        {known.length > 0 && ` You can use ${known.map((v) => `{${v}}`).join(", ")}.`}
      </div>
    </div>
  );
}

const FAQ_EXAMPLE = `Pouch types: stand-up, zipper, flat and spout pouches, printed to your design.
Minimum order: 500 pieces per design.
Delivery: 10-12 working days after artwork approval.
Prices depend on size, material and quantity - we send a quote on WhatsApp within a day.`;

/** Answer with AI: the customer asks in their own words; the AI answers only from the owner's text. */
export function AiReplyPanel({ node, set }: { node: WfNode; set: Set }) {
  const faq = node.faq ?? "";
  const [ready, setReady] = useState<boolean | null>(null);
  useEffect(() => {
    api.aiStatus().then((s) => setReady(s.configured)).catch(() => setReady(null));
  }, []);
  return (
    <div className="space-y-2">
      <label className="block space-y-1">
        <span className="flex justify-between text-xs font-medium text-slate-600">
          <span>What the AI may answer from (your FAQ)</span>
          <span className={faq.length > AI_FAQ_MAX ? "text-rose-600" : "text-slate-400"}>{faq.length}/{AI_FAQ_MAX}</span>
        </span>
        <textarea className="input w-full text-xs" rows={9} value={faq} placeholder={FAQ_EXAMPLE}
          aria-label="FAQ text" onChange={(e) => set({ faq: e.target.value })} />
      </label>
      <div className="flex gap-2">
        <label className="block flex-1 space-y-1">
          <span className="text-xs font-medium text-slate-600">Tone</span>
          <select className="input w-full" value={node.tone ?? "friendly"}
            onChange={(e) => set({ tone: e.target.value as "friendly" | "formal" })}>
            <option value="friendly">Warm and friendly</option>
            <option value="formal">Formal</option>
          </select>
        </label>
        <label className="block w-36 space-y-1">
          <span className="text-xs font-medium text-slate-600">Longest answer</span>
          <input type="number" min={100} max={1024} className="input w-full" value={node.max_chars ?? 600}
            onChange={(e) => set({ max_chars: Number(e.target.value) })} />
        </label>
      </div>
      <label className="block space-y-1">
        <span className="text-xs font-medium text-slate-600">Save their question as</span>
        <input className="input w-full font-mono text-xs" value={node.store ?? ""} placeholder="question"
          onChange={(e) => set({ store: e.target.value })} />
        <span className="text-[11px] text-slate-500">
          So a later step can pass it on, e.g. a message to your team: <span className="font-mono">{"{question}"}</span>.
        </span>
      </label>
      <div className="rounded-md border border-violet-200 bg-violet-50 px-2 py-1.5 text-[11px] text-violet-900 space-y-1">
        <div>
          The customer types a question and the AI answers <b>only from the text above</b>, in their language. When that
          text does not clearly answer it - or the AI cannot be reached - the conversation takes <b>Not sure</b>:
          connect it to a person or the menu.
        </div>
        <div>
          Only this text and the customer&apos;s question go to Groq - never their number, name or orders. Connect
          <b> Answered</b> back to this step to let them ask another.
        </div>
      </div>
      {ready === false && (
        <div className="rounded-md border border-amber-200 bg-amber-50 px-2 py-1.5 text-[11px] text-amber-900">
          No Groq API key is set yet, so every question takes Not sure. Add it in Settings, under AI assistant.
        </div>
      )}
    </div>
  );
}

// ---------------- finding things in your own data ----------------
/** English defaults, so the panel is never blank while the owner's own column names are fetched. */
const FALLBACK_FIELDS: DataFieldsInfo = {
  sources: [
    { value: "orders", label: "Their orders",
      finds: [{ value: "all", label: "All of this customer's orders" },
        { value: "so", label: "The order with an SO number" },
        { value: "po", label: "The order with a PO number" },
        { value: "fg", label: "The orders with an item code" }],
      fields: DATA_FIELDS.orders.map((name) => ({ name, label: name, values: [] })) },
    { value: "customer", label: "Their record in your customer list",
      finds: [{ value: "all", label: "Their own record" }],
      fields: DATA_FIELDS.customer.map((name) => ({ name, label: name, values: [] })) },
  ],
  ops: [{ value: "eq", label: "is" }, { value: "ne", label: "is not" }, { value: "contains", label: "contains" },
    { value: "is_set", label: "is filled in" }, { value: "is_empty", label: "is blank" }],
  sorts: [{ value: "newest", label: "Newest first" }, { value: "oldest", label: "Oldest first" },
    { value: "as_is", label: "As they come" }],
  groups: [{ value: "so", label: "One row per order" }, { value: "line", label: "One row per order line" }],
};

// Fetched once per browser session: the labels are the owner's own column names, which only change
// when they change them in Settings.
let cachedFields: DataFieldsInfo | null = null;

export function useDataFields(): DataFieldsInfo {
  const [got, setGot] = useState<DataFieldsInfo | null>(cachedFields);
  useEffect(() => {
    if (cachedFields) return;
    api.dataFields().then((d) => { cachedFields = d; setGot(d); }).catch(() => setGot(null));
  }, []);
  return got ?? FALLBACK_FIELDS;
}

export function fieldsOf(info: DataFieldsInfo, source: string | undefined) {
  return (info.sources.find((s) => s.value === (source ?? "orders")) ?? info.sources[0]).fields;
}

/** Find in your data: what to look up, what to keep from it, and what each row reads. */
export function DataPanel({ node, doc, set }: { node: WfNode; doc: WorkflowDoc; set: Set }) {
  const info = useDataFields();
  const source = info.sources.find((s) => s.value === (node.source ?? "orders")) ?? info.sources[0];
  const fields = source.fields;
  const filters = node.filter ?? [];
  const saves = Object.entries(node.save ?? {}) as [string, string][];
  const known = variablesIn(doc).filter((v) => v !== `${node.store}_count` && v !== `${node.store}_list`);
  const setFilter = (i: number, patch: Partial<{ field: string; op: string; value: string }>) =>
    set({ filter: filters.map((f, n) => (n === i ? { ...f, ...patch } : f)) });
  const setSave = (next: [string, string][]) => set({ save: Object.fromEntries(next.filter(([k]) => k)) });

  return (
    <div className="space-y-2">
      <label className="block space-y-1">
        <span className="text-xs font-medium text-slate-600">Look in</span>
        <select className="input w-full" value={node.source ?? "orders"}
          onChange={(e) => set({ source: e.target.value as "orders", find: "all" })}>
          {info.sources.map((s) => <option key={s.value} value={s.value}>{s.label}</option>)}
        </select>
      </label>

      {(node.source ?? "orders") === "orders" && (
        <>
          <label className="block space-y-1">
            <span className="text-xs font-medium text-slate-600">Find</span>
            <select className="input w-full" value={node.find ?? "all"} onChange={(e) => set({ find: e.target.value as "all" })}>
              {source.finds.map((f) => <option key={f.value} value={f.value}>{f.label}</option>)}
            </select>
          </label>
          {(node.find ?? "all") !== "all" && (
            <label className="block space-y-1">
              <span className="text-xs font-medium text-slate-600">Which value</span>
              <input className="input w-full font-mono text-xs" value={node.match ?? ""} placeholder="{typed_so}"
                onChange={(e) => set({ match: e.target.value })} />
              <span className="text-[11px] text-slate-500">
                The answer you saved earlier.{known.length > 0 && ` You can use ${known.map((v) => `{${v}}`).join(", ")}.`}
              </span>
            </label>
          )}
          <label className="block space-y-1">
            <span className="text-xs font-medium text-slate-600">One row per</span>
            <select className="input w-full" value={node.group ?? "so"} onChange={(e) => set({ group: e.target.value as "so" })}>
              {info.groups.map((g) => <option key={g.value} value={g.value}>{g.label}</option>)}
            </select>
          </label>
        </>
      )}

      <div className="space-y-1">
        <span className="text-xs font-medium text-slate-600">Only keep rows where</span>
        {filters.map((f, i) => (
          <div key={i} className="flex gap-1">
            <select className="input text-xs flex-1" value={f.field} onChange={(e) => setFilter(i, { field: e.target.value })}>
              <option value="">Choose a field…</option>
              {fields.map((x) => <option key={x.name} value={x.name}>{x.label}</option>)}
            </select>
            <select className="input text-xs w-24" value={f.op} onChange={(e) => setFilter(i, { op: e.target.value })}>
              {info.ops.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
            </select>
            {!["is_set", "is_empty"].includes(f.op) && (
              <>
                <input className="input text-xs w-28" value={f.value} list={`vals-${node.id}-${i}`}
                  onChange={(e) => setFilter(i, { value: e.target.value })} />
                <datalist id={`vals-${node.id}-${i}`}>
                  {(fields.find((x) => x.name === f.field)?.values ?? []).map((v) => <option key={v} value={v} />)}
                </datalist>
              </>
            )}
            <button className="btn-ghost text-[11px] text-rose-600" aria-label={`Remove rule ${i + 1}`}
              onClick={() => set({ filter: filters.filter((_, n) => n !== i) })}>✕</button>
          </div>
        ))}
        <button className="btn-ghost text-xs"
          onClick={() => set({ filter: [...filters, { field: fields[0]?.name ?? "", op: "eq", value: "" }] })}>
          + Add a rule
        </button>
      </div>

      <div className="flex gap-2">
        <label className="block flex-1 space-y-1">
          <span className="text-xs font-medium text-slate-600">Order them</span>
          <select className="input w-full" value={node.sort ?? "newest"} onChange={(e) => set({ sort: e.target.value as "newest" })}>
            {info.sorts.map((s) => <option key={s.value} value={s.value}>{s.label}</option>)}
          </select>
        </label>
        <label className="block w-28 space-y-1">
          <span className="text-xs font-medium text-slate-600">Show at most</span>
          <input type="number" min={1} max={DATA_ROWS_MAX} className="input w-full" value={node.limit ?? DATA_ROWS_MAX}
            onChange={(e) => set({ limit: Number(e.target.value) })} />
        </label>
      </div>

      <label className="block space-y-1">
        <span className="text-xs font-medium text-slate-600">Call what you find</span>
        <input className="input w-full font-mono text-xs" value={node.store ?? ""} placeholder="orders"
          onChange={(e) => set({ store: e.target.value })} />
        <span className="text-[11px] text-slate-500">
          Later steps can write <span className="font-mono">{`{${node.store || "orders"}_count}`}</span> (how many
          there are) and <span className="font-mono">{`{${node.store || "orders"}_list}`}</span> (the lines below).
        </span>
      </label>

      <div className="space-y-1">
        <span className="text-xs font-medium text-slate-600">Remember from the first one</span>
        {saves.map(([name, field], i) => (
          <div key={i} className="flex gap-1">
            <input className="input text-xs w-1/2 font-mono" value={name} placeholder="newest_so"
              onChange={(e) => setSave(saves.map((r, n) => (n === i ? [e.target.value, r[1]] : r)))} />
            <select className="input text-xs flex-1" value={field}
              onChange={(e) => setSave(saves.map((r, n) => (n === i ? [r[0], e.target.value] : r)))}>
              <option value="">Choose a field…</option>
              {fields.map((x) => <option key={x.name} value={x.name}>{x.label}</option>)}
            </select>
            <button className="btn-ghost text-[11px] text-rose-600" aria-label={`Remove value ${i + 1}`}
              onClick={() => setSave(saves.filter((_, n) => n !== i))}>✕</button>
          </div>
        ))}
        <button className="btn-ghost text-xs" onClick={() => setSave([...saves, ["", fields[0]?.name ?? ""]])}>
          + Remember a value
        </button>
      </div>

      <label className="block space-y-1">
        <span className="text-xs font-medium text-slate-600">Each line reads</span>
        <input className="input w-full font-mono text-xs" value={node.list_line ?? DATA_LIST_LINE}
          onChange={(e) => set({ list_line: e.target.value })} />
        <span className="text-[11px] text-slate-500">
          Used by <span className="font-mono">{`{${node.store || "orders"}_list}`}</span>. Fields:{" "}
          {fields.map((x) => `{${x.name}}`).join(" ")}. This one is not translated - the words come from your data.
        </span>
      </label>

      <div className="rounded-md border border-sky-200 bg-sky-50 px-2 py-1.5 text-[11px] text-sky-900">
        Only this customer's own orders are ever read - never another company's, and never your internal
        Connection Status. <b>Nothing found</b> must lead somewhere: a customer with no orders still needs an answer.
      </div>
    </div>
  );
}
