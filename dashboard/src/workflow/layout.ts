import { portsOf, type WfNode, type WorkflowDoc } from "./types";

// Spacing tuned to the node card (w-72) so arranged steps never overlap.
export const COL_W = 380;
const GAP_Y = 48;
const PAD = 40;

/** Roughly how tall a card draws. Cards now show every button and list row, so a list with eight
 *  rows is far taller than a Wait step - stacking them at a fixed pitch made them overlap. */
export function estimateHeight(n: WfNode): number {
  let h = 64; // colour bar, title row, padding
  const says = ["message", "question", "end", "product_list"].includes(n.type);
  if (says) {
    const text = n.text?.en ?? "";
    const lines = Math.min(6, Math.max(1, Math.ceil(text.length / 36) + (text.match(/\n/g)?.length ?? 0)));
    h += 30 + lines * 18;
    if (n.header?.en) h += 22;
    if (n.footer?.en) h += 18;
    if (n.media?.url || n.header_media?.url) h += 88;
    if (n.type === "product_list" || n.input?.kind === "list") h += 30;
  } else {
    h += 40;
  }
  if (n.type === "question" && n.input && n.input.kind !== "text") {
    const rows = n.input.kind === "language" ? 3 : n.input.options?.length ?? 0;
    const sections = new Set((n.input.options ?? []).map((o) => o.section?.en).filter(Boolean)).size;
    h += rows * (n.input.kind === "list" ? 34 : 30) + sections * 20 + 8;
  }
  const exits = portsOf(n).filter((p) => !(n.type === "question" && n.input?.kind !== "text" && p.port.startsWith("opt:")));
  h += exits.length ? 16 + exits.length * 22 : 0;
  return h;
}

/** Lay the steps out left to right in the order a customer meets them.
 *
 *  Breadth-first from the start step: a step's column is how many steps it is from the start, and
 *  its place in the column is the order it was reached. Anything unreachable goes in a last column of
 *  its own, so arranging never hides a step - it shows plainly which ones nothing leads to. */
export function arrange(doc: WorkflowDoc): WorkflowDoc {
  const ids = new Set(doc.nodes.map((n) => n.id));
  const out = new Map<string, string[]>();
  for (const e of doc.edges) {
    if (ids.has(e.from) && ids.has(e.to)) out.set(e.from, [...(out.get(e.from) ?? []), e.to]);
  }

  const depth = new Map<string, number>();
  if (ids.has(doc.start)) {
    depth.set(doc.start, 0);
    const queue = [doc.start];
    while (queue.length) {
      const id = queue.shift() as string;
      for (const next of out.get(id) ?? []) {
        if (!depth.has(next)) {
          depth.set(next, (depth.get(id) ?? 0) + 1);
          queue.push(next);
        }
      }
    }
  }

  const deepest = Math.max(0, ...depth.values());
  const columns = new Map<number, WfNode[]>();
  for (const n of doc.nodes) {
    const col = depth.has(n.id) ? (depth.get(n.id) as number) : deepest + 1;
    columns.set(col, [...(columns.get(col) ?? []), n]);
  }

  const pos = new Map<string, { x: number; y: number }>();
  for (const [col, members] of columns) {
    let y = PAD;
    for (const n of members) {
      pos.set(n.id, { x: PAD + col * COL_W, y });
      y += estimateHeight(n) + GAP_Y;
    }
  }
  return { ...doc, nodes: doc.nodes.map((n) => ({ ...n, ...(pos.get(n.id) ?? { x: n.x, y: n.y }) })) };
}

/** Somewhere near `want` that no existing step already occupies. */
export function freeSpot(doc: WorkflowDoc, want: { x: number; y: number }): { x: number; y: number } {
  const taken = (x: number, y: number) =>
    doc.nodes.some((n) => Math.abs((n.x ?? 0) - x) < 300 && y < (n.y ?? 0) + estimateHeight(n) && (n.y ?? 0) < y + 160);
  for (let i = 0; i < 40; i++) {
    const x = Math.round(want.x + (i % 4) * 70);
    const y = Math.round(want.y + Math.floor(i / 4) * 70);
    if (!taken(x, y)) return { x, y };
  }
  return { x: Math.round(want.x), y: Math.round(want.y) };
}
