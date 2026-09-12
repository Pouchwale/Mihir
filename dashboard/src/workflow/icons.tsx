import {
  Bell, Braces, CircleHelp, CircleStop, ClipboardCheck, CornerDownRight, Database, FileText, GitBranch,
  MessageSquare, ShoppingBag, Sparkles, Tag, Timer, UserPlus, Webhook, type LucideIcon,
} from "lucide-react";
import { metaOf, type NodeKind, type NodeMeta } from "./types";

/** One icon per step type. Real icons rather than emoji: emoji render at different sizes and
 *  weights on every operating system, which is most of why the editor looked unfinished. */
export const NODE_ICON: Record<NodeKind, LucideIcon> = {
  message: MessageSquare,
  question: CircleHelp,
  product_list: ShoppingBag,
  ai_reply: Sparkles,
  template: FileText,
  condition: GitBranch,
  delay: Timer,
  jump: CornerDownRight,
  set_var: Braces,
  tags: Tag,
  subscribe: Bell,
  assign: UserPlus,
  chat_status: ClipboardCheck,
  api_request: Webhook,
  data: Database,
  end: CircleStop,
};

/** A colour per palette group, so the canvas can be read at a glance: what talks to the customer,
 *  what routes, what reaches outside. `mini` is the minimap swatch. */
export const GROUP_STYLE: Record<NodeMeta["group"], { chip: string; bar: string; mini: string }> = {
  Say: { chip: "bg-sky-50 text-sky-600", bar: "bg-sky-500", mini: "#0ea5e9" },
  Route: { chip: "bg-violet-50 text-violet-600", bar: "bg-violet-500", mini: "#8b5cf6" },
  Remember: { chip: "bg-amber-50 text-amber-600", bar: "bg-amber-400", mini: "#f59e0b" },
  People: { chip: "bg-rose-50 text-rose-600", bar: "bg-rose-500", mini: "#f43f5e" },
  Outside: { chip: "bg-indigo-50 text-indigo-600", bar: "bg-indigo-500", mini: "#6366f1" },
  Finish: { chip: "bg-slate-100 text-slate-600", bar: "bg-slate-400", mini: "#64748b" },
};

export function styleOf(kind: NodeKind) {
  return GROUP_STYLE[metaOf(kind).group];
}

export function NodeIcon({ kind, className = "w-4 h-4" }: { kind: NodeKind; className?: string }) {
  const Icon = NODE_ICON[kind] ?? MessageSquare;
  return <Icon className={className} strokeWidth={2} aria-hidden />;
}
