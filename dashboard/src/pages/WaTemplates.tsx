import { useEffect, useState } from "react";
import { api, WaTemplate, WaTemplateButton, WaTemplateHeader } from "../api";
import MenuPreview from "../MenuPreview";
import { Badge, Empty, ErrorBox, usePoll } from "../ui";

const STATUS_TONE: Record<string, string> = {
  APPROVED: "green", PENDING: "amber", REJECTED: "red", PAUSED: "amber", DISABLED: "red", UNKNOWN: "slate",
};

const STATUS_HELP: Record<string, string> = {
  APPROVED: "Ready to send.",
  PENDING: "With Meta for review. Nothing to do but wait.",
  REJECTED: "Meta refused it. Fix the wording and create a new one.",
  PAUSED: "Meta paused it because customers marked it as spam.",
  DISABLED: "Meta disabled it. It cannot be sent.",
};

const CATEGORY_HELP: Record<string, string> = {
  UTILITY: "Order and account updates the customer is expecting.",
  MARKETING: "Offers, news, anything promotional.",
  AUTHENTICATION: "One-time codes only.",
};

export default function WaTemplates() {
  const { data, error, loading, reload } = usePoll(() => api.waTemplates(), 0);
  const opts = usePoll(() => api.waTemplateLanguages(), 0);
  const [name, setName] = useState("");
  const [body, setBody] = useState("");
  const [category, setCategory] = useState("UTILITY");
  const [language, setLanguage] = useState("en");
  const [header, setHeader] = useState<WaTemplateHeader>({ type: "TEXT", text: "", link: "" });
  const [footer, setFooter] = useState("");
  const [buttons, setButtons] = useState<WaTemplateButton[]>([]);
  const [samples, setSamples] = useState<Record<string, string>>({});
  const [problems, setProblems] = useState<string[]>([]);
  const [preview, setPreview] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const variables = [...new Set([...body.matchAll(/\{\{([A-Za-z0-9_]{1,40})\}\}/g)].map((m) => m[1]))];
  const singleBraces = body.includes("{") && !body.includes("{{");
  const draft = () => ({
    name, body, category, language, footer, buttons, samples,
    header: header.type === "TEXT" && !header.text ? null : header,
  });

  // The server decides what Meta will accept; this is a live echo of that, not a second opinion.
  useEffect(() => {
    if (!body.trim()) {
      setProblems([]);
      setPreview("");
      return;
    }
    let alive = true;
    const t = window.setTimeout(async () => {
      try {
        const r = await api.waTemplatePreview(draft());
        if (alive) {
          setProblems(r.problems);
          setPreview(r.preview);
        }
      } catch { /* the create call reports problems too */ }
    }, 400);
    return () => {
      alive = false;
      window.clearTimeout(t);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [name, body, category, language, footer, JSON.stringify(buttons), JSON.stringify(samples), JSON.stringify(header)]);

  const create = async () => {
    setBusy(true);
    setMsg(null);
    setErr(null);
    try {
      const r = await api.waTemplateCreate(draft());
      if (r.ok) {
        setMsg(r.detail);
        setName("");
        setBody("");
        setButtons([]);
        setSamples({});
        await reload();
      } else {
        setErr(r.detail);
      }
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const remove = async (t: WaTemplate) => {
    if (!confirm(`Delete the template “${t.name}”? Meta removes it for good.`)) return;
    try {
      await api.waTemplateDelete(t.name, t.language);
      await reload();
    } catch (e) {
      setErr((e as Error).message);
    }
  };

  const setButton = (i: number, patch: Partial<WaTemplateButton>) =>
    setButtons(buttons.map((b, n) => (n === i ? { ...b, ...patch } : b)));

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-xl font-semibold">WhatsApp templates</h1>
        <p className="text-sm text-slate-500">
          The only messages you can send to someone who has <b>not</b> written to you in the last 24
          hours — order updates, dispatch notices, reminders. Meta has to approve each one first.
        </p>
      </div>

      <ErrorBox msg={error || err} />
      {msg && <div className="card text-sm bg-sky-50 border-sky-200">{msg}</div>}
      {data && !data.ok && <div className="card text-sm bg-amber-50 border-amber-200">{data.detail}</div>}

      <div className="card space-y-3">
        <div className="font-medium text-sm">Create a template</div>

        <div className="grid md:grid-cols-3 gap-3">
          <label className="space-y-1">
            <span className="text-xs font-medium text-slate-600">Name</span>
            <input className="input w-full font-mono" placeholder="order_ready" value={name}
              onChange={(e) => setName(e.target.value)} />
            <span className="text-[11px] text-slate-400">Lowercase, numbers and underscores.</span>
          </label>
          <label className="space-y-1">
            <span className="text-xs font-medium text-slate-600">Category</span>
            <select className="input w-full" value={category} onChange={(e) => setCategory(e.target.value)}>
              {(opts.data?.categories ?? ["UTILITY", "MARKETING", "AUTHENTICATION"]).map((c) => (
                <option key={c} value={c}>{c}</option>
              ))}
            </select>
            <span className="text-[11px] text-slate-400">{CATEGORY_HELP[category]}</span>
          </label>
          <label className="space-y-1">
            <span className="text-xs font-medium text-slate-600">Language</span>
            <select className="input w-full" value={language} onChange={(e) => setLanguage(e.target.value)}>
              {(opts.data?.languages ?? ["en", "hi", "gu"]).map((l) => <option key={l} value={l}>{l}</option>)}
            </select>
            <span className="text-[11px] text-slate-400">One template per language.</span>
          </label>
        </div>

        <div className="grid md:grid-cols-3 gap-3">
          <label className="space-y-1">
            <span className="text-xs font-medium text-slate-600">Header</span>
            <select className="input w-full" value={header.type}
              onChange={(e) => setHeader({ ...header, type: e.target.value as "TEXT" })}>
              <option value="TEXT">Text (or none)</option>
              <option value="IMAGE">Image</option>
              <option value="VIDEO">Video</option>
              <option value="DOCUMENT">Document</option>
            </select>
          </label>
          <label className="space-y-1 md:col-span-2">
            <span className="text-xs font-medium text-slate-600">
              {header.type === "TEXT" ? "Header text (optional)" : "Public link to the file"}
            </span>
            {header.type === "TEXT" ? (
              <input className="input w-full" value={header.text ?? ""}
                onChange={(e) => setHeader({ ...header, text: e.target.value })} />
            ) : (
              <input className="input w-full font-mono" placeholder="https://…/order.pdf" value={header.link ?? ""}
                onChange={(e) => setHeader({ ...header, link: e.target.value })} />
            )}
          </label>
        </div>

        <label className="block space-y-1">
          <span className="text-xs font-medium text-slate-600">Message</span>
          <textarea className="input w-full" rows={4} dir="auto" value={body}
            placeholder="Hello {{name}}, your order {{so_no}} is ready for dispatch."
            onChange={(e) => setBody(e.target.value)} />
          <span className="text-[11px] text-slate-400">
            Use <span className="font-mono">{"{{name}}"}</span> for anything that changes each time — note the
            double braces. These are Meta&apos;s templates, not the bot&apos;s own messages.
          </span>
        </label>
        {singleBraces && (
          <div className="text-xs text-rose-700">
            That looks like <span className="font-mono">{"{name}"}</span>. Meta templates need double braces.
          </div>
        )}

        {variables.length > 0 && (
          <div className="rounded-lg border border-slate-200 p-2 space-y-1.5">
            <div className="text-xs font-medium text-slate-600">Example values</div>
            <div className="text-[11px] text-slate-500">
              Meta reads your template with these filled in. Leave one blank and a reviewer sees the
              literal text <span className="font-mono">{"{{" + variables[0] + "}}"}</span>, which is a
              common reason for a rejection nobody can explain.
            </div>
            {variables.map((v) => (
              <div key={v} className="flex gap-2 items-center">
                <span className="font-mono text-xs w-32 shrink-0 text-slate-500">{`{{${v}}}`}</span>
                <input className="input text-xs flex-1" value={samples[v] ?? ""} placeholder="e.g. Rajesh Patel"
                  onChange={(e) => setSamples({ ...samples, [v]: e.target.value })} />
              </div>
            ))}
          </div>
        )}

        <label className="block space-y-1">
          <span className="text-xs font-medium text-slate-600">Footer (optional)</span>
          <input className="input w-full" value={footer} onChange={(e) => setFooter(e.target.value)} />
        </label>

        <div className="space-y-1.5">
          <span className="text-xs font-medium text-slate-600">Buttons (up to 3, optional)</span>
          {buttons.map((b, i) => (
            <div key={i} className="flex flex-wrap gap-1 items-center">
              <select className="input text-xs w-32" value={b.type}
                onChange={(e) => setButton(i, { type: e.target.value as "url" })}>
                <option value="quick_reply">Quick reply</option>
                <option value="url">Open a link</option>
                <option value="call">Call a number</option>
              </select>
              <input className="input text-xs w-36" placeholder="Button label" value={b.text}
                onChange={(e) => setButton(i, { text: e.target.value })} />
              {b.type === "url" && (
                <input className="input text-xs flex-1 font-mono" placeholder="https://…" value={b.url ?? ""}
                  onChange={(e) => setButton(i, { url: e.target.value })} />
              )}
              {b.type === "call" && (
                <input className="input text-xs flex-1 font-mono" placeholder="919876543210" value={b.phone ?? ""}
                  onChange={(e) => setButton(i, { phone: e.target.value })} />
              )}
              <button className="btn-ghost text-[11px] text-rose-600"
                onClick={() => setButtons(buttons.filter((_, n) => n !== i))}>✕</button>
            </div>
          ))}
          {buttons.length < 3 && (
            <button className="btn-ghost text-xs"
              onClick={() => setButtons([...buttons, { type: "quick_reply", text: "" }])}>
              + Add a button
            </button>
          )}
        </div>

        {body && (
          <div className="rounded-lg bg-[#efeae2] p-3">
            <div className="text-[11px] text-slate-500 mb-1">What the customer sees</div>
            <div className="bg-white rounded-lg px-3 py-2 shadow-sm max-w-md">
              {header.type !== "TEXT" && header.link && (
                <div className="text-[11px] text-slate-500 bg-slate-100 rounded px-2 py-3 mb-1 text-center">
                  {header.type.toLowerCase()} attachment
                </div>
              )}
              {header.type === "TEXT" && header.text && <div className="text-sm font-semibold">{header.text}</div>}
              <div className="text-sm whitespace-pre-wrap break-words">{preview || body}</div>
              {footer && <div className="text-[11px] text-slate-400 mt-1">{footer}</div>}
              {buttons.filter((b) => b.text).length > 0 && (
                <MenuPreview options={{
                  kind: "buttons",
                  items: buttons.filter((b) => b.text).map((b) => ({
                    title: b.type === "url" ? `🔗 ${b.text}` : b.type === "call" ? `📞 ${b.text}` : b.text,
                    description: "",
                  })),
                  button_text: "", section_title: "", header: "", footer: "",
                }} compact />
              )}
            </div>
          </div>
        )}

        {problems.length > 0 && (
          <div className="rounded-lg border border-amber-200 bg-amber-50 p-2 space-y-0.5">
            {problems.map((p, i) => <div key={i} className="text-xs text-amber-900">⚠ {p}</div>)}
          </div>
        )}

        <div className="flex items-center gap-2">
          <button className="btn-primary" disabled={busy || !name.trim() || !body.trim()} onClick={create}>
            Send to Meta for approval
          </button>
          <span className="text-xs text-slate-500">
            Usually minutes, sometimes a day. The decision is Meta&apos;s, not ours.
          </span>
        </div>
      </div>

      {data?.templates?.length ? (
        <div className="card p-0 overflow-auto">
          <table className="w-full">
            <thead>
              <tr>
                <th className="th">Name</th>
                <th className="th">Status</th>
                <th className="th">Category</th>
                <th className="th">Message</th>
                <th className="th"></th>
              </tr>
            </thead>
            <tbody>
              {data.templates.map((t) => (
                <tr key={`${t.id}-${t.language}`}>
                  <td className="td">
                    <div className="font-mono text-xs">{t.name}</div>
                    <div className="text-[11px] text-slate-400">{t.language}</div>
                  </td>
                  <td className="td">
                    <Badge tone={STATUS_TONE[t.status] || "slate"}>{t.status}</Badge>
                    <div className="text-[11px] text-slate-500 mt-0.5">{STATUS_HELP[t.status] || ""}</div>
                    {t.feedback && <div className="text-[11px] text-rose-700 mt-0.5">{t.feedback}</div>}
                  </td>
                  <td className="td text-xs">{t.category}</td>
                  <td className="td text-xs max-w-sm whitespace-pre-wrap break-words">{t.body}</td>
                  <td className="td text-right">
                    <button className="btn-ghost text-xs text-rose-600" onClick={() => remove(t)}>Delete</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        !loading && data?.ok && <Empty>No templates yet. Create one above.</Empty>
      )}
    </div>
  );
}
