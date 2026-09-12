import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Background, BackgroundVariant, Controls, Handle, MarkerType, MiniMap, Position, ReactFlow,
  ReactFlowProvider, applyEdgeChanges, applyNodeChanges, useNodesInitialized, useReactFlow,
  type Connection, type Edge, type EdgeChange, type Node, type NodeChange, type NodeProps,
} from "@xyflow/react";
// Imported HERE, not in main.tsx: this module is lazy-loaded, and pulling the stylesheet into the
// main bundle would undo the whole reason for code-splitting the editor.
import "@xyflow/react/dist/style.css";
import type { Lang } from "../api";
import { NodeIcon, styleOf } from "./icons";
import { BUILTIN_ORDER_STATUS, DEFAULT_LANGUAGE_LABELS, describeTriggers, metaOf, offeredLanguages, portsOf, textOf, type NodeKind, type WfIssue, type WfNode, type WorkflowDoc } from "./types";
import { MediaPreview, WaText } from "./whatsapp";

/** The drag payload type the palette sets and the canvas accepts. */
export const DRAG_TYPE = "application/x-wf-node";
export const CARD_W = 288; // w-72: wide enough for a WhatsApp bubble and its buttons
const CARD_H = 220;
// Below this a card's text is too small to read, so on open we show the start instead of
// shrinking a long flow into an unreadable strip.
const READABLE_ZOOM = 0.7;

export interface CanvasApi {
  /** The centre of what is on screen, in canvas coordinates - where a clicked palette item goes. */
  centre: () => { x: number; y: number };
  fitAll: () => void;
  /** The readable starting view: everything if it fits legibly, otherwise the start step. */
  home: () => void;
  focus: (id: string) => void;
}

interface CardData extends Record<string, unknown> {
  node: WfNode;
  lang: Lang;
  isStart: boolean;
  /** how the workflow starts, shown on the start step */
  starts: string[];
  worst: "fail" | "warn" | null;
  unreachable: boolean;
  wired: string[];
}

/** A one-line description of what a step does, for the card body. */
function summary(node: WfNode, lang: Lang): string {
  switch (node.type) {
    case "message":
    case "question":
    case "end":
      return textOf(node.text, lang);
    case "condition":
      return (node.branches ?? []).map((b) => b.label || b.id).join(" · ");
    case "set_var":
      return Object.keys(node.assign ?? {}).map((k) => `{${k}}`).join("  ");
    case "delay":
      return node.seconds ? `Waits ${node.seconds < 60 ? `${node.seconds} seconds` : `${Math.round(node.seconds / 60)} min`}` : "";
    case "tags":
      return (node.tags ?? []).filter(Boolean).map((t) => `#${t}`).join("  ");
    case "assign":
      if (node.to === "bot") return "Hands the chat back to the bot";
      if (node.to === "operator") return node.email ? `To ${node.email}` : "";
      return (node.teams ?? []).filter(Boolean).length ? `To ${(node.teams ?? []).filter(Boolean).join(", ")}` : "";
    case "chat_status":
      return node.status ? `Marks the chat ${node.status}` : "";
    case "subscribe":
      return node.subscribe === false ? "Unsubscribes from campaigns" : "Subscribes to campaigns";
    case "template":
      return node.template_name ? `Sends ${node.template_name}` : "";
    case "api_request":
      return node.url ? `${node.method ?? "GET"} ${node.url}` : "";
    case "data": {
      const what = node.source === "customer" ? "their record in your customer list"
        : node.find && node.find !== "all" ? `the order for ${node.match || "an answer"}` : "all their orders";
      return `Finds ${what}${node.store ? ` → {${node.store}_list}` : ""}`;
    }
    case "ai_reply":
      return textOf(node.text, lang)
        || (node.faq?.trim() ? `Answers questions from your FAQ (${node.faq.trim().length} characters)` : "");
    case "jump":
      if (node.workflow === BUILTIN_ORDER_STATUS) return "Hands over to the order-status bot, at its main menu";
      return node.workflow ? `Continues in “${node.workflow}”` : "";
    default:
      return "";
  }
}

const SOURCE_ON = "!w-2.5 !h-2.5 !bg-slate-500 !border-2 !border-white";
// A hollow red ring is an exit that leads nowhere - the commonest way to strand a customer.
const SOURCE_OFF = "!w-3 !h-3 !bg-white !border-2 !border-rose-400";

/** The message as the customer's phone shows it: attachment, header, text, footer, and the list
 *  button under the bubble. Formatting and {answers} are drawn, not left as raw markup. */
function Bubble({ node, lang }: { node: WfNode; lang: Lang }) {
  const text = textOf(node.text, lang);
  const header = textOf(node.header, lang);
  const footer = textOf(node.footer, lang);
  const media = node.media?.url ? node.media : node.header_media?.url ? node.header_media : null;
  const caption = node.media ? textOf(node.media.caption, lang) : "";
  const empty = !text && !header && !media && !caption;
  const listButton = node.type === "question" && node.input?.kind === "list";
  return (
    <div className="mx-3 mb-2.5 rounded-lg bg-[#efeae2] p-1.5">
      <div className="rounded-md bg-white px-2.5 py-2 shadow-sm space-y-1.5">
        {media && <MediaPreview type={media.type} url={media.url} compact />}
        {header && <div className="text-[12.5px] font-semibold text-slate-900"><WaText text={header} vars /></div>}
        {(text || caption) && (
          <div className="text-[12.5px] leading-snug text-slate-700 line-clamp-6"><WaText text={text || caption} vars /></div>
        )}
        {empty && <div className="text-[12px] italic text-slate-400">Not set up yet — click to edit</div>}
        {footer && <div className="text-[11px] text-slate-400"><WaText text={footer} /></div>}
        {node.type === "product_list" && (
          <div className="text-[11px] text-slate-500">
            {node.catalog_id && node.set_id ? `Catalogue ${node.catalog_id} · set ${node.set_id}` : "No catalogue chosen yet"}
          </div>
        )}
      </div>
      {(listButton || node.type === "product_list") && (
        <div className="mt-1 rounded-md bg-white text-center text-[12px] font-medium text-sky-600 py-1 shadow-sm">
          {listButton ? `☰ ${textOf(node.input?.button_text, lang) || "Menu"}` : "View items"}
        </div>
      )}
    </div>
  );
}

/** Every button or list row the customer can tap, each with its own connector - the part of a WATI
 *  card that shows where each choice leads. */
function Choices({ node, lang, wired }: { node: WfNode; lang: Lang; wired: string[] }) {
  const spec = node.input;
  if (!spec) return null;
  const isList = spec.kind === "list";
  const rows = spec.kind === "language"
    ? offeredLanguages(spec).map((code) => ({
        port: `opt:${code}`, title: spec.language_labels?.[code]?.trim() || DEFAULT_LANGUAGE_LABELS[code],
        description: "", section: "",
      }))
    : (spec.options ?? []).map((o) => ({
        port: `opt:${o.value}`,
        title: textOf(o.label, lang) || o.value,
        description: textOf(o.description, lang),
        section: textOf(o.section, lang),
      }));
  let previous = "";
  return (
    <div className="px-3 pb-2 space-y-1">
      {rows.map((r) => {
        const on = wired.includes(r.port);
        const heading = isList && r.section && r.section !== previous ? r.section : "";
        previous = r.section;
        return (
          <div key={r.port}>
            {heading && (
              <div className="pt-1 pb-0.5 text-[10px] font-semibold uppercase tracking-wide text-slate-400 truncate">{heading}</div>
            )}
            <div className={`relative rounded-md border px-2 py-1 text-[12px] ${isList ? "text-left" : "text-center"} ${
              on ? "border-sky-200 bg-sky-50/60 text-sky-700" : "border-dashed border-rose-300 bg-rose-50/40 text-rose-600"}`}>
              <div className="font-medium truncate">{r.title}</div>
              {r.description && <div className="text-[10.5px] text-slate-500 truncate">{r.description}</div>}
              {/* right: -13 puts the handle on the card's edge (12px padding + 1px border) */}
              <Handle type="source" position={Position.Right} id={r.port} style={{ right: -13 }}
                className={on ? SOURCE_ON : SOURCE_OFF} />
            </div>
          </div>
        );
      })}
    </div>
  );
}

/** One step on the canvas, drawn the way WATI draws it: the message as the customer will see it, and
 *  every button or row they can tap, each with its own connector. */
function Card({ data, selected }: NodeProps) {
  const { node, lang, isStart, starts, worst, unreachable, wired } = data as unknown as CardData;
  const meta = metaOf(node.type);
  const style = styleOf(node.type);
  const ports = portsOf(node);
  const tappable = node.type === "question" && !!node.input
    && node.input.kind !== "text" && node.input.kind !== "data_list";
  const others = ports.filter((p) => !(tappable && p.port.startsWith("opt:")));
  const says = ["message", "question", "end", "product_list"].includes(node.type);
  const border = selected
    ? "border-brand-500 ring-2 ring-brand-500/25"
    : worst === "fail" ? "border-rose-300" : worst === "warn" ? "border-amber-300" : "border-slate-200";

  return (
    <div className={`w-72 rounded-xl border bg-white shadow-card ${border} ${unreachable ? "opacity-60 border-dashed" : ""}`}>
      <div className={`h-1 rounded-t-[11px] ${style.bar}`} />
      {/* Every step, End included, must accept an incoming connection - without a target handle
          React Flow silently refuses to draw an edge into it. */}
      <Handle type="target" position={Position.Left} style={{ top: 27 }}
        className="!w-2.5 !h-2.5 !bg-slate-400 !border-2 !border-white" />
      <div className="flex items-center gap-2 px-3 pt-2.5 pb-2">
        <span className={`grid place-items-center w-6 h-6 rounded-md shrink-0 ${style.chip}`}>
          <NodeIcon kind={node.type} className="w-3.5 h-3.5" />
        </span>
        <span className="text-[13px] font-semibold text-slate-800 truncate">{node.title || meta.title}</span>
        <span className="ml-auto flex items-center gap-1.5 shrink-0">
          {isStart && <span className="badge bg-brand-50 text-brand-700 !text-[10px] !py-0">Start</span>}
          {worst && <span className={`w-2 h-2 rounded-full ${worst === "fail" ? "bg-rose-500" : "bg-amber-400"}`} />}
        </span>
      </div>
      {isStart && (
        <div className="mx-3 mb-2 flex flex-wrap gap-1 text-[10.5px] leading-tight">
          {starts.length ? starts.map((s) => (
            <span key={s} className="rounded-full border border-brand-200 bg-brand-50 px-1.5 py-0.5 text-brand-800">{s}</span>
          )) : <span className="text-amber-700">Nothing starts it yet — set it under Start</span>}
        </div>
      )}

      {node.unsupported ? (
        <div className="mx-3 mb-2.5 rounded-md bg-rose-50 border border-rose-200 px-2 py-1.5 text-[11.5px] text-rose-700">
          WATI “{node.unsupported}” step — not supported here yet. Click for its original settings.
        </div>
      ) : says ? (
        <Bubble node={node} lang={lang} />
      ) : (
        <div className="px-3 pb-2.5 text-[12.5px] leading-snug text-slate-600 whitespace-pre-wrap break-words line-clamp-3">
          {summary(node, lang) || <span className="italic text-slate-400">Not set up yet — click to edit</span>}
        </div>
      )}

      {tappable && !node.unsupported && <Choices node={node} lang={lang} wired={wired} />}
      {node.type === "question" && node.input?.kind === "data_list" && (
        <div className="px-3 pb-2 text-[11px] text-slate-500">
          Shows the rows found by{" "}
          <span className="font-mono text-violet-700">{node.input.from || "— choose a step"}</span>
          {node.store ? <> → <span className="font-mono text-violet-700">{`{${node.store}}`}</span></> : ""}
        </div>
      )}
      {node.type === "question" && node.input?.kind === "text" && (
        <div className="px-3 pb-2 text-[11px] text-slate-500">
          Waits for a typed answer{node.store ? <> → <span className="font-mono text-violet-700">{`{${node.store}}`}</span></> : ""}
        </div>
      )}

      {others.length > 0 && (
        <div className="border-t border-slate-100 px-3 py-2 space-y-1.5">
          {others.map((p) => {
            const on = wired.includes(p.port);
            // Optional exits: unconnected, the question simply asks again ("Anything else", "Not valid")
            // or stops asking ("Gave up") - not an error to draw in red.
            const optional = p.port === "default" || p.port === "invalid" || p.port === "retry_exhausted";
            return (
              <div key={p.port} className="relative flex items-center justify-end gap-2 text-[11.5px]">
                <span className={`truncate ${on || optional ? "text-slate-600" : "text-rose-600 font-medium"}`}>{p.label}</span>
                <Handle type="source" position={Position.Right} id={p.port} style={{ right: -13 }}
                  className={on ? SOURCE_ON : optional ? "!w-2.5 !h-2.5 !bg-white !border-2 !border-slate-300" : SOURCE_OFF} />
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

// Module scope on purpose: a fresh object each render remounts every node on every keystroke.
const nodeTypes = { card: Card };

interface Props {
  doc: WorkflowDoc;
  lang: Lang;
  issues: WfIssue[];
  selected: string | null;
  readOnly?: boolean;
  onSelect: (id: string | null) => void;
  onChange: (doc: WorkflowDoc) => void;
  onDropNode: (kind: NodeKind, at: { x: number; y: number }) => void;
  onReady?: (api: CanvasApi) => void;
}

export default function Canvas(props: Props) {
  return (
    <ReactFlowProvider>
      <Inner {...props} />
    </ReactFlowProvider>
  );
}

function Inner({ doc, lang, issues, selected, readOnly, onSelect, onChange, onDropNode, onReady }: Props) {
  const rf = useReactFlow();
  const wrap = useRef<HTMLDivElement>(null);

  // Handlers can fire back to back in one tick - deleting a step also deletes its connections, and
  // each reports separately. Each must build on the result of the one before rather than on the doc
  // from the last render, or the second write quietly undoes the first and a deleted step returns.
  const docRef = useRef(doc);
  docRef.current = doc;
  const commit = useCallback((next: WorkflowDoc) => {
    docRef.current = next;
    onChange(next);
  }, [onChange]);

  const reachable = useMemo(() => reachableFrom(doc), [doc]);
  const worst = useMemo(() => {
    const m = new Map<string, "fail" | "warn">();
    for (const i of issues) {
      if (i.node_id && (i.level === "fail" || !m.has(i.node_id))) m.set(i.node_id, i.level);
    }
    return m;
  }, [issues]);

  // React Flow owns these arrays; the document is rebuilt into them, keeping what React Flow
  // measured. Throwing the measured size away on every edit is what left the minimap blank and
  // made fit/centre work from zero-sized boxes.
  const [nodes, setNodes] = useState<Node[]>([]);
  const [edges, setEdges] = useState<Edge[]>([]);

  useEffect(() => {
    setNodes((prev) => {
      const old = new Map(prev.map((n) => [n.id, n]));
      return doc.nodes.map((n) => {
        const o = old.get(n.id);
        return {
          id: n.id,
          type: "card",
          position: o?.dragging ? o.position : { x: n.x ?? 0, y: n.y ?? 0 },
          dragging: o?.dragging,
          measured: o?.measured,
          selected: n.id === selected,
          data: {
            node: n, lang,
            isStart: doc.start === n.id,
            starts: doc.start === n.id ? describeTriggers(doc) : [],
            worst: worst.get(n.id) ?? null,
            unreachable: !reachable.has(n.id),
            wired: doc.edges.filter((e) => e.from === n.id).map((e) => e.port),
          } as CardData,
        } as Node;
      });
    });
  }, [doc, lang, selected, worst, reachable]);

  useEffect(() => {
    // An exit that no longer exists (a deleted button, the old "next" of a hand-over) has no handle
    // to draw from; the problems list names it instead.
    const exits = new Map(doc.nodes.map((n) => [n.id, new Set(portsOf(n).map((p) => p.port))]));
    setEdges((prev) => {
      const chosen = new Set(prev.filter((e) => e.selected).map((e) => e.id));
      return doc.edges.filter((e) => exits.get(e.from)?.has(e.port)).map((e) => {
        return {
          id: e.id,
          source: e.from,
          sourceHandle: e.port,
          target: e.to,
          type: "smoothstep",
          selected: chosen.has(e.id),
          markerEnd: { type: MarkerType.ArrowClosed, width: 16, height: 16, color: "#94a3b8" },
          style: { stroke: "#94a3b8", strokeWidth: 1.5 },
          pathOptions: { borderRadius: 14 },
        } as Edge;
      });
    });
  }, [doc]);

  const onNodesChange = useCallback((c: NodeChange[]) => setNodes((n) => applyNodeChanges(c, n)), []);
  const onEdgesChange = useCallback((c: EdgeChange[]) => setEdges((e) => applyEdgeChanges(c, e)), []);

  // Positions are written to the document once, when a drag ends - not on every mouse move, which
  // would save and re-validate the whole workflow sixty times a second.
  const onNodeDragStop = useCallback((_: unknown, __: Node, moved: Node[]) => {
    if (readOnly) return;
    const pos = new Map(moved.map((n) => [n.id, n.position]));
    const cur = docRef.current;
    commit({
      ...cur,
      nodes: cur.nodes.map((n) => {
        const p = pos.get(n.id);
        return p ? { ...n, x: Math.round(p.x), y: Math.round(p.y) } : n;
      }),
    });
  }, [commit, readOnly]);

  const onNodesDelete = useCallback((gone: Node[]) => {
    if (readOnly) return;
    const ids = new Set(gone.map((n) => n.id));
    const cur = docRef.current;
    const kept = cur.nodes.filter((n) => !ids.has(n.id));
    commit({
      ...cur,
      nodes: kept,
      edges: cur.edges.filter((e) => !ids.has(e.from) && !ids.has(e.to)),
      start: ids.has(cur.start) ? (kept[0]?.id ?? "") : cur.start,
    });
    if (selected && ids.has(selected)) onSelect(null);
  }, [commit, readOnly, selected, onSelect]);

  const onEdgesDelete = useCallback((gone: Edge[]) => {
    if (readOnly) return;
    const ids = new Set(gone.map((e) => e.id));
    const cur = docRef.current;
    commit({ ...cur, edges: cur.edges.filter((e) => !ids.has(e.id)) });
  }, [commit, readOnly]);

  const onConnect = useCallback((c: Connection) => {
    if (readOnly || !c.source || !c.target || c.source === c.target) return;
    const port = c.sourceHandle || "next";
    const cur = docRef.current;
    // One exit leads to exactly one place: drawing a new connection replaces the old one.
    const kept = cur.edges.filter((e) => !(e.from === c.source && e.port === port));
    const id = `e${Date.now().toString(36)}${Math.random().toString(36).slice(2, 5)}`;
    commit({ ...cur, edges: [...kept, { id, from: c.source, port, to: c.target }] });
  }, [commit, readOnly]);

  // Fit everything if that stays readable, otherwise show the start step at a readable zoom.
  // Fitting a long flow unconditionally is exactly what shrank it to an unreadable strip.
  const placeView = useCallback((d: WorkflowDoc, duration = 0) => {
    const el = wrap.current;
    if (!el || !d.nodes.length) return;
    const W = el.clientWidth;
    const H = el.clientHeight;
    const xs = d.nodes.map((n) => n.x ?? 0);
    const ys = d.nodes.map((n) => n.y ?? 0);
    const bw = Math.max(...xs) - Math.min(...xs) + CARD_W;
    const bh = Math.max(...ys) - Math.min(...ys) + CARD_H;
    if (Math.min((W - 80) / bw, (H - 80) / bh) >= READABLE_ZOOM) {
      void rf.fitView({ padding: 0.15, maxZoom: 1, duration });
      return;
    }
    const start = d.nodes.find((n) => n.id === d.start) ?? d.nodes[0];
    const z = 0.85;
    void rf.setViewport({ x: 60 - (start.x ?? 0) * z, y: H / 2 - ((start.y ?? 0) + CARD_H / 2) * z, zoom: z }, { duration });
  }, [rf]);

  const initialized = useNodesInitialized();
  const placed = useRef(false);
  useEffect(() => {
    if (!initialized || placed.current || !doc.nodes.length) return;
    placed.current = true;
    placeView(doc);
  }, [initialized, doc, placeView]);

  useEffect(() => {
    onReady?.({
      centre: () => {
        const r = wrap.current?.getBoundingClientRect();
        if (!r) return { x: 120, y: 120 };
        return rf.screenToFlowPosition({ x: r.left + r.width / 2, y: r.top + r.height / 2 });
      },
      // "Show everything", but never below a zoom where the text can still be read.
      fitAll: () => void rf.fitView({ padding: 0.15, maxZoom: 1, minZoom: 0.5, duration: 300 }),
      home: () => placeView(docRef.current, 300),
      focus: (id: string) => {
        const n = docRef.current.nodes.find((x) => x.id === id);
        if (n) void rf.setCenter((n.x ?? 0) + CARD_W / 2, (n.y ?? 0) + CARD_H / 2,
          { zoom: Math.max(rf.getZoom(), 0.9), duration: 300 });
      },
    });
  }, [onReady, rf, placeView]);

  const onDragOver = (e: React.DragEvent) => {
    if (!readOnly && e.dataTransfer.types.includes(DRAG_TYPE)) {
      e.preventDefault();
      e.dataTransfer.dropEffect = "move";
    }
  };
  const onDrop = (e: React.DragEvent) => {
    const kind = e.dataTransfer.getData(DRAG_TYPE) as NodeKind;
    if (!kind || readOnly) return;
    e.preventDefault();
    const p = rf.screenToFlowPosition({ x: e.clientX, y: e.clientY });
    onDropNode(kind, { x: p.x - CARD_W / 2, y: p.y - 24 });
  };

  return (
    <div ref={wrap} className="w-full h-full" onDragOver={onDragOver} onDrop={onDrop}>
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        onNodeDragStop={onNodeDragStop}
        onNodesDelete={onNodesDelete}
        onEdgesDelete={onEdgesDelete}
        onConnect={onConnect}
        isValidConnection={(c) => c.source !== c.target}
        onNodeClick={(_, n) => onSelect(n.id)}
        onPaneClick={() => onSelect(null)}
        nodesDraggable={!readOnly}
        nodesConnectable={!readOnly}
        elementsSelectable
        deleteKeyCode={readOnly ? null : ["Backspace", "Delete"]}
        snapToGrid
        snapGrid={[10, 10]}
        minZoom={0.2}
        maxZoom={1.75}
        proOptions={{ hideAttribution: false }}
      >
        <Background variant={BackgroundVariant.Dots} gap={20} size={1.3} color="#cbd5e1" />
        {/* No fit button here: the toolbar's Fit keeps a readable-zoom floor, React Flow's own does not. */}
        <Controls showInteractive={false} showFitView={false} position="bottom-left" />
        <MiniMap
          position="bottom-right"
          pannable
          zoomable
          style={{ width: 170, height: 110 }}
          nodeBorderRadius={6}
          nodeColor={(n) => styleOf(((n.data as unknown as CardData).node).type).mini}
          maskColor="rgba(241, 245, 249, 0.7)"
        />
      </ReactFlow>
    </div>
  );
}

/** Steps a customer can actually get to. Anything else is drawn but dead. */
export function reachableFrom(doc: WorkflowDoc): Set<string> {
  const out = new Set<string>();
  if (!doc.nodes.some((n) => n.id === doc.start)) return out;
  const byFrom = new Map<string, string[]>();
  for (const e of doc.edges) byFrom.set(e.from, [...(byFrom.get(e.from) ?? []), e.to]);
  const stack = [doc.start];
  out.add(doc.start);
  while (stack.length) {
    for (const to of byFrom.get(stack.pop() as string) ?? []) {
      if (!out.has(to) && doc.nodes.some((n) => n.id === to)) {
        out.add(to);
        stack.push(to);
      }
    }
  }
  return out;
}
