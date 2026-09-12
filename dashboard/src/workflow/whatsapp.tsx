import { Fragment, type ReactNode } from "react";

/** WhatsApp's own formatting - *bold*, _italic_, ~strike~ and ```monospace``` - drawn the way the
 *  customer's phone draws it, so what the owner reads in the editor is what the customer reads.
 *
 *  A marker only counts when it hugs a word, as on WhatsApp: "2 * 3" is not bold, and the underscore
 *  inside {sys.customer_name} is not the start of italics. */
const PATTERN =
  /```([\s\S]+?)```|(?<![\p{L}\p{N}])\*(\S(?:[^*\n]*?\S)?)\*(?![\p{L}\p{N}])|(?<![\p{L}\p{N}])_(\S(?:[^_\n]*?\S)?)_(?![\p{L}\p{N}])|(?<![\p{L}\p{N}])~(\S(?:[^~\n]*?\S)?)~(?![\p{L}\p{N}])|(\{[A-Za-z][A-Za-z0-9_.]{0,40}\})/gu;

function format(text: string, vars: boolean, key = "k"): ReactNode[] {
  const out: ReactNode[] = [];
  let last = 0;
  let i = 0;
  for (const m of text.matchAll(PATTERN)) {
    const at = m.index ?? 0;
    if (at > last) out.push(text.slice(last, at));
    const k = `${key}-${i++}`;
    if (m[1] !== undefined) out.push(<code key={k} className="font-mono text-[0.95em] bg-black/5 rounded px-0.5">{m[1]}</code>);
    else if (m[2] !== undefined) out.push(<strong key={k} className="font-semibold">{format(m[2], vars, k)}</strong>);
    else if (m[3] !== undefined) out.push(<em key={k}>{format(m[3], vars, k)}</em>);
    else if (m[4] !== undefined) out.push(<s key={k}>{format(m[4], vars, k)}</s>);
    else if (m[5] !== undefined) {
      // an answer or contact detail that is filled in when the message is sent
      out.push(vars
        ? <span key={k} className="rounded bg-violet-50 text-violet-700 px-1 font-mono text-[0.9em]">{m[5]}</span>
        : m[5]);
    }
    last = at + m[0].length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

/** Text with WhatsApp formatting applied. `vars` highlights {placeholders} (on in the editor, off in
 *  a test chat, where they have already been filled in). */
export function WaText({ text, vars = false, className = "" }: { text: string; vars?: boolean; className?: string }) {
  if (!text) return null;
  return (
    <span className={`whitespace-pre-wrap break-words ${className}`}>
      {text.split("\n").map((line, n, all) => (
        <Fragment key={n}>
          {format(line, vars, `l${n}`)}
          {n < all.length - 1 && "\n"}
        </Fragment>
      ))}
    </span>
  );
}

/** What an attachment looks like above a message: a picture shows, anything else is a labelled file. */
export function MediaPreview({ type, url, compact = false }: { type: string; url: string; compact?: boolean }) {
  if (!url) return null;
  if (type === "image" && /^https?:\/\//.test(url)) {
    return (
      <img src={url} alt="" loading="lazy" referrerPolicy="no-referrer"
        className={`w-full object-cover rounded-md bg-slate-100 ${compact ? "h-20" : "max-h-56"}`}
        onError={(e) => { (e.currentTarget as HTMLImageElement).style.display = "none"; }} />
    );
  }
  const label = type === "video" ? "Video" : type === "audio" ? "Voice note" : "Document";
  return (
    <div className="flex items-center gap-2 rounded-md bg-slate-100 px-2 py-1.5 text-[11px] text-slate-600">
      <span className="font-medium">{label}</span>
      <span className="truncate text-slate-400">{url.split("/").pop()}</span>
    </div>
  );
}
