import { useEffect, useRef, useState } from "react";
import { api, type CustomerRow } from "../api";
import MenuPreview from "../MenuPreview";
import { SimAction, SimMessage } from "./types";
import { MediaPreview, WaText } from "./whatsapp";

interface Bubble {
  dir: "in" | "out" | "action" | "note";
  text: string;
  msg?: SimMessage;
  tapped?: boolean;
}

/** Steps that reach outside the conversation are shown, not performed. */
function asActionBubbles(actions: SimAction[]): Bubble[] {
  return actions.map((a) => ({ dir: "action" as const, text: a.describe }));
}

function asBotBubbles(messages: SimMessage[]): Bubble[] {
  return messages.map((m) => ({ dir: "out" as const, text: m.text, msg: m }));
}

/** What the AI did this turn - the test chat really asks it, so the owner sees what a customer gets. */
function aiNote(ai?: string, ready?: boolean): Bubble[] {
  const notes: Record<string, string> = {
    matched: "Understood by AI as one of the choices",
    answered: "Answered by AI from your FAQ text",
    unsure: "The AI was not sure, so this took Not sure",
    unavailable: ready
      ? "The AI could not answer just now (Groq's limit or an outage), so this took Not sure"
      : "No Groq API key is set, so this took Not sure - add one in Settings, under AI assistant",
  };
  const text = notes[ai ?? ""];
  return text ? [{ dir: "note", text }] : [];
}

/** A WhatsApp list: the button under the message opens the rows, grouped under their sections. */
function ListChoices({ msg, onPick, disabled }: { msg: SimMessage; onPick: (t: string) => void; disabled: boolean }) {
  const [open, setOpen] = useState(false);
  const opts = msg.options;
  if (!opts) return null;
  const groups = msg.sections?.length ? msg.sections : [{ title: opts.section_title ?? "", rows: opts.items }];
  return (
    <div className="mt-2 border-t border-slate-100 pt-1">
      <button type="button" className="w-full text-center text-sm font-medium text-sky-600 py-1 disabled:opacity-60"
        disabled={disabled} onClick={() => setOpen((v) => !v)}>
        ☰ {opts.button_text || "Menu"}
      </button>
      {open && !disabled && (
        <div className="mt-1 rounded-lg border border-slate-200 bg-slate-50 p-1.5 space-y-1">
          {groups.map((g, gi) => (
            <div key={gi}>
              {g.title && <div className="px-1 pt-1 text-[10px] font-semibold uppercase tracking-wide text-slate-400">{g.title}</div>}
              {g.rows.map((r) => (
                <button key={r.title} type="button" onClick={() => onPick(r.title)}
                  className="block w-full text-left rounded-md bg-white px-2 py-1.5 text-sm hover:bg-sky-50">
                  <div className="font-medium text-slate-800">{r.title}</div>
                  {r.description && <div className="text-[11px] text-slate-500">{r.description}</div>}
                </button>
              ))}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

/** Walk a workflow the way a customer would, without anything leaving the server.
 *
 *  This is a dry run of the graph: no WhatsApp message is sent, no conversation is created and the
 *  real bot is untouched, so a draft can be tested long before customers can reach it. */
export default function Simulator({ wfKey, title, hasPublished, onClose }: {
  wfKey: string;
  title: string;
  hasPublished: boolean;
  onClose: () => void;
}) {
  const [log, setLog] = useState<Bubble[]>([]);
  const [state, setState] = useState<Record<string, unknown> | null>(null);
  const [text, setText] = useState("");
  const [use, setUse] = useState<"draft" | "published">("draft");
  const [contact, setContact] = useState("Test customer");
  // Test as one of your customers, so answers checked against your data use their real orders
  const [asPhone, setAsPhone] = useState("");
  const [customers, setCustomers] = useState<CustomerRow[]>([]);
  // ...or as any number you type: the one on the packing slip, a new enquiry, your own phone
  const [typing, setTyping] = useState(false);
  const [typed, setTyped] = useState("");
  const [found, setFound] = useState<CustomerRow | null | undefined>(undefined);  // undefined = still looking
  useEffect(() => {
    api.customers().then((rows) => setCustomers(rows.slice(0, 300))).catch(() => setCustomers([]));
  }, []);
  const [stopped, setStopped] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const bottom = useRef<HTMLDivElement>(null);

  const restart = async (which: "draft" | "published" = use, phone: string = asPhone) => {
    setBusy(true);
    setErr(null);
    setLog([]);
    setStopped("");
    try {
      const r = await api.workflowSimulate(wfKey, { use: which, contact_name: contact, phone });
      setState(r.state);
      setStopped(r.stopped);
      setLog([...asActionBubbles(r.actions ?? []), ...asBotBubbles(r.messages)]);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  useEffect(() => {
    void restart("draft");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [wfKey]);

  // A typed number starts a fresh test on its own, once it looks like a phone number and the typing
  // has stopped - and says whose it is, because that is what decides how the bot treats them.
  const digits = typed.replace(/\D/g, "");
  useEffect(() => {
    if (!typing || digits.length < 8) {
      setFound(undefined);
      return;
    }
    let alive = true;
    setFound(undefined);
    const timer = window.setTimeout(async () => {
      let hit: CustomerRow | null = null;
      try {
        hit = (await api.customers(digits)).find((c) => c.phone === digits) ?? null;
      } catch { /* the test still runs; we just cannot name them */ }
      if (!alive) return;
      setFound(hit);
      setAsPhone(digits);
      void restart(use, digits);
    }, 600);
    return () => { alive = false; window.clearTimeout(timer); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [digits, typing]);
  useEffect(() => {
    bottom.current?.scrollIntoView({ behavior: "smooth" });
  }, [log]);

  const send = async (said: string, tapped = false, media?: { type: string; url: string; address?: string }) => {
    if ((!said.trim() && !media) || busy) return;
    setBusy(true);
    setErr(null);
    const shown = media ? (media.type === "location" ? `📍 Location${media.address ? `: ${media.address}` : ""}` : "📎 Photo") : said;
    setLog((l) => [...l, { dir: "in", text: shown, tapped }]);
    setText("");
    try {
      const r = await api.workflowSimulate(wfKey, {
        text: said, state: state ?? undefined, use, contact_name: contact, phone: asPhone, media,
      });
      setState(r.state);
      setStopped(r.stopped);
      setLog((l) => [...l, ...aiNote(r.ai, r.ai_ready), ...asActionBubbles(r.actions ?? []), ...asBotBubbles(r.messages)]);
      if (r.jump_to) setLog((l) => [...l, { dir: "action", text: `Hands over to the workflow "${r.jump_to}"` }]);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const last = log.length - 1;
  // only what the customer said; sys.* are the stand-in contact details this test supplies
  const answers = Object.entries((state?.vars ?? {}) as Record<string, string>).filter(([k]) => !k.startsWith("sys."));
  // Who this test is running as. A number nobody in the customer list has is a real case worth
  // testing: {sys.verified} is "no" and a data step finds nothing, exactly as for a new enquiry.
  const asCustomer = typing ? found ?? null : customers.find((c) => c.phone === asPhone) ?? null;

  return (
    <div className="fixed inset-0 z-50 bg-black/40 flex items-center justify-center p-4" onClick={onClose}>
      <div role="dialog" aria-modal="true" aria-label={`Test ${title}`}
        className="bg-white rounded-xl shadow-lg w-full max-w-2xl max-h-full flex flex-col" onClick={(e) => e.stopPropagation()}>
        <div className="flex flex-wrap items-center gap-2 border-b border-slate-200 px-3 py-2">
          <div className="font-medium text-sm">Test “{title}”</div>
          {hasPublished && (
            <select className="input text-xs !h-8" value={use}
              onChange={(e) => { const v = e.target.value as "draft" | "published"; setUse(v); void restart(v); }}>
              <option value="draft">the draft you are editing</option>
              <option value="published">the published version</option>
            </select>
          )}
          <label className="flex items-center gap-1 text-xs text-slate-500">
            as
            <select className="input text-xs !h-8 w-44" value={typing ? "__typed" : asPhone} aria-label="Test as"
              title="Test as one of your customers, or as any number you type - their own orders are used"
              onChange={(e) => {
                const picked = e.target.value;
                setFound(undefined);
                if (picked === "__typed") { setTyping(true); setAsPhone(""); void restart(use, ""); return; }
                setTyping(false);
                setTyped("");
                setAsPhone(picked);
                void restart(use, picked);
              }}>
              <option value="">a test customer (no orders)</option>
              <option value="__typed">a number I type…</option>
              {customers.map((c) => <option key={c.phone} value={c.phone}>{c.name}</option>)}
            </select>
          </label>
          {typing && (
            <span className="flex items-center gap-1.5">
              <input className="input text-xs !h-8 w-36 font-mono" value={typed} inputMode="numeric" maxLength={15}
                placeholder="919876543210" aria-label="Number to test as" autoFocus
                onChange={(e) => setTyped(e.target.value)} />
              {digits.length >= 8 && (
                <span className={`badge !text-[11px] ${found === undefined ? "bg-slate-100 text-slate-500"
                  : found ? "bg-emerald-100 text-emerald-700" : "bg-amber-100 text-amber-800"}`}>
                  {found === undefined ? "checking…" : found ? found.name : "not in your customer list"}
                </span>
              )}
            </span>
          )}
          {!asCustomer && (
            <input className="input text-xs !h-8 w-28" value={contact} onChange={(e) => setContact(e.target.value)}
              aria-label="Test customer name" title="The customer name {sys.customer_name} shows in this test" />
          )}
          <button className="btn-ghost text-xs !h-8 ml-auto" onClick={() => restart()}>Start again</button>
          <button className="btn-ghost text-xs !h-8" onClick={onClose}>Close</button>
        </div>

        <div className="px-3 py-1.5 text-[11px] text-slate-500 bg-slate-50 border-b border-slate-200">
          Nothing is sent to WhatsApp — this walks the steps you drew and shows what a customer would see.
          {asPhone && (
            <> Running as <span className="font-mono">{asPhone}</span>
              {asCustomer
                ? <> — {asCustomer.name}, so their own orders are used.</>
                : <> — not in your customer list, so <span className="font-mono">{"{sys.verified}"}</span> is “no” and
                    steps that look in your data find nothing.</>}
            </>
          )}
        </div>

        {err && <div className="px-3 py-2 text-xs text-rose-700 bg-rose-50">{err}</div>}

        <div className="flex-1 overflow-auto p-3 space-y-2 bg-[#efeae2] min-h-[280px]">
          {log.map((b, i) => {
            if (b.dir === "action") {
              return (
                <div key={i} className="mx-auto max-w-[85%] rounded-lg border border-dashed border-slate-300 bg-white/70 px-3 py-1 text-[11px] text-slate-600 text-center">
                  ⚙ {b.text} <span className="text-slate-400">— not actually done in a test</span>
                </div>
              );
            }
            if (b.dir === "note") {
              return (
                <div key={i} className="mx-auto max-w-[85%] rounded-full border border-violet-200 bg-violet-50 px-3 py-1 text-[11px] text-violet-800 text-center">
                  ✨ {b.text}
                </div>
              );
            }
            if (b.dir === "in") {
              return (
                <div key={i} className="max-w-[80%] ml-auto rounded-lg px-3 py-2 text-sm shadow-sm bg-[#d9fdd3]">
                  <div className="whitespace-pre-wrap break-words">
                    {b.tapped && <span className="text-slate-400 mr-1">👆</span>}{b.text}
                  </div>
                </div>
              );
            }
            const m = b.msg;
            const live = !busy && i === last;
            return (
              <div key={i} className="max-w-[80%] rounded-lg px-3 py-2 text-sm shadow-sm bg-white space-y-1.5">
                {m?.media?.url && <MediaPreview type={m.media.type} url={m.media.url} />}
                {m?.header && <div className="font-semibold"><WaText text={m.header} /></div>}
                {(b.text || m?.media?.caption) && <div><WaText text={b.text || m?.media?.caption || ""} /></div>}
                {m?.footer && <div className="text-[11px] text-slate-400"><WaText text={m.footer} /></div>}
                {m?.kind === "product_list" && (
                  <div className="border-t border-slate-100 pt-1 text-center text-sm font-medium text-sky-600">
                    View items <span className="text-[11px] text-slate-400">(opens your WhatsApp catalogue)</span>
                  </div>
                )}
                {m?.options?.kind === "buttons" && (
                  <MenuPreview
                    options={{ ...m.options, button_text: "", section_title: "", header: "", footer: "" }}
                    disabled={!live}
                    onPick={(s) => send(s.title, true)}
                  />
                )}
                {m?.options?.kind === "list" && <ListChoices msg={m} disabled={!live} onPick={(t) => send(t, true)} />}
              </div>
            );
          })}
          {stopped === "jump" && <div className="text-center text-[11px] text-slate-500">— handed to another workflow —</div>}
          {stopped === "end" && <div className="text-center text-[11px] text-slate-500">— conversation finished —</div>}
          {stopped === "handed_over" && (
            <div className="text-center text-[11px] text-slate-500">— a person has the chat now: the bot stays quiet until it is handed back —</div>
          )}
          {stopped === "no_route" && (
            <div className="text-center text-[11px] text-rose-700">
              — nothing is connected here, so a real customer would get nothing more —
            </div>
          )}
          {stopped === "cap" && (
            <div className="text-center text-[11px] text-rose-700">— stopped: these steps loop without ever asking anything —</div>
          )}
          <div ref={bottom} />
        </div>

        {answers.length > 0 && (
          <div className="px-3 py-1.5 text-[11px] text-slate-600 border-t border-slate-200 flex flex-wrap gap-x-3">
            {answers.map(([k, v]) => (
              <span key={k}><span className="font-mono text-slate-400">{k}</span> {String(v)}</span>
            ))}
          </div>
        )}

        <form className="flex gap-2 p-3 border-t border-slate-200"
          onSubmit={(e) => { e.preventDefault(); void send(text); }}>
          <button type="button" className="btn-ghost !h-9 !px-2" title="Send a test photo" aria-label="Send a test photo"
            disabled={busy || stopped === "end" || stopped === "handed_over"}
            onClick={() => void send("", false, { type: "image", url: "https://files.example.com/test-photo.jpg" })}>📎</button>
          <button type="button" className="btn-ghost !h-9 !px-2" title="Share a test location" aria-label="Share a test location"
            disabled={busy || stopped === "end" || stopped === "handed_over"}
            onClick={() => void send("", false, { type: "location", url: "https://maps.google.com/?q=23.0225,72.5714", address: "Ahmedabad, Gujarat" })}>📍</button>
          <input className="input flex-1" placeholder="Type as the customer…" value={text}
            disabled={busy || stopped === "end" || stopped === "handed_over"} onChange={(e) => setText(e.target.value)} autoFocus />
          <button className="btn-primary" disabled={busy || !text.trim() || stopped === "end" || stopped === "handed_over"}>Send</button>
        </form>
      </div>
    </div>
  );
}
