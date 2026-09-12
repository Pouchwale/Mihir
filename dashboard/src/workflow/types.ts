import type { Lang } from "../api";

/** A string in every language the bot speaks. English is required; the others fall back to it. */
export type I18n = Partial<Record<Lang, string>>;

export const LANGS: Lang[] = ["en", "hi", "gu"];
export const LANG_NAME: Record<Lang, string> = { en: "English", hi: "हिंदी", gu: "ગુજરાતી" };

/** WhatsApp's own limits. Mirrored from backend/app/services/menus.py, which is the authority -
 *  these are only here so the editor can warn while you type. The server has the final say. */
export const LIMITS = {
  buttons: 3,
  buttonText: 20,
  listRows: 10,
  rowTitle: 24,
  rowDesc: 72,
  sectionTitle: 24,
  listButton: 20,
  header: 60,
  footer: 60,
  body: 1024,
} as const;

export type NodeKind =
  | "message" | "question" | "condition" | "set_var" | "end"
  | "delay" | "tags" | "assign" | "chat_status" | "subscribe" | "api_request" | "template" | "jump"
  | "product_list" | "ai_reply" | "data";

export type MediaType = "image" | "video" | "document" | "audio";
export const MEDIA_TYPES: MediaType[] = ["image", "video", "document", "audio"];
export const CHAT_STATUSES = ["open", "pending", "solved", "block"] as const;
export const DELAY_MAX_SEC = 600;  // WATI caps its Time Delay node at 10 minutes
export type InputKind = "buttons" | "list" | "text" | "language" | "data_list";
export type CondOp =
  | "eq" | "ne" | "contains" | "starts_with" | "ends_with"
  | "gt" | "gte" | "lt" | "lte" | "is_set" | "is_empty" | "between";

export interface WfOption {
  value: string;
  label: I18n;
  description?: I18n;
  /** List rows only: rows with the same section title are grouped under it, as in WATI. */
  section?: I18n;
  synonyms?: string[];
}

export interface WfInput {
  kind: InputKind;
  options?: WfOption[];
  /** data_list: the Find-in-your-data step whose rows this shows, and what each row reads */
  from?: string;
  /** buttons for two or three rows, a list beyond that ("auto"), or always one of them */
  show?: "auto" | "list" | "buttons";
  title_field?: string;
  description_field?: string;
  section_field?: string;
  button_text?: I18n;
  section_title?: I18n;
  /** language question: which buttons to offer (all three when unset) */
  languages?: Lang[];
  /** language question: the owner's own wording on each button */
  language_labels?: Partial<Record<Lang, string>>;
}

export interface WfBranch {
  id: string;
  label: string;
  when: { var: string; op: CondOp; value?: string };
}

/** One step on the canvas. Deliberately one shape with optional parts rather than a union per kind:
 *  the document round-trips through the server untouched, so extra keys must survive an edit. */
export interface WfNode {
  id: string;
  type: NodeKind;
  title: string;
  x: number;
  y: number;
  text?: I18n;
  input?: WfInput;
  store?: string;
  validate?: {
    type: "any" | "number" | "email" | "phone" | "regex" | "date" | "url" | "time" | "file" | "location" | "lookup";
    min?: string; max?: string; pattern?: string;
    /** lookup: what the answer is checked against in the business's data */
    source?: LookupSource;
  };
  max_retries?: number;
  retry_text?: I18n;
  invalid_text?: I18n;
  // WhatsApp's frame around a question with buttons or a list
  header?: I18n;
  footer?: I18n;
  header_media?: { type: MediaType; url: string };
  // a message can carry a picture, video, document or voice note
  media?: { type: MediaType; url: string; caption?: I18n };
  branches?: WfBranch[];
  assign?: Record<string, string>;
  /** Remember: also save the values on the customer's WATI contact (WATI's "Update attribute") */
  to_contact?: boolean;
  // delay
  seconds?: number;
  // tags
  tags?: string[];
  remove?: boolean;
  // assign
  to?: "operator" | "team" | "bot";
  email?: string;
  teams?: string[];
  // chat status / subscribe
  status?: string;
  subscribe?: boolean;
  // send an approved template
  template_name?: string;
  params?: Record<string, string>;
  // api request
  url?: string;
  method?: "GET" | "POST";
  headers?: Record<string, string>;
  body?: string;
  save?: Record<string, string>;
  routes?: { id: string; label: string; path: string; op: string; value: string }[];
  // find in your data
  source?: "orders" | "customer";
  find?: "all" | "so" | "po" | "fg";
  /** the value to look up, e.g. {typed_so} */
  match?: string;
  group?: "so" | "line";
  sort?: "newest" | "oldest" | "as_is";
  limit?: number;
  filter?: { field: string; op: string; value: string }[];
  /** what one row reads in {store_list}, e.g. "SO {so_no} - {real_status}" */
  list_line?: string;
  // jump
  workflow?: string;
  // product list (WhatsApp catalogue)
  catalog_id?: string;
  set_id?: string;
  // answer with AI: the owner's FAQ text, how it sounds, and the longest answer
  faq?: string;
  tone?: "friendly" | "formal";
  max_chars?: number;
  // a step kept from a WATI import that cannot run here yet
  unsupported?: string;
  wati_raw?: unknown;
}

export interface WfEdge {
  id: string;
  from: string;
  port: string;
  to: string;
}

export interface WorkflowSettings {
  /** false = one text per step, the WATI way (a separate path per language). */
  multilingual?: boolean;
  imported_from?: string;
  /** How customers reach it. Read through triggersOf(), which tolerates older shapes. */
  triggers?: Partial<Triggers>;
  /** A customer who does not answer for this long is let out. */
  idle_minutes?: number;
  /** fill Hindi and Gujarati as soon as the English is written (on unless switched off) */
  auto_translate?: boolean;
  /** buttons and lists: let the AI work out which choice a typed answer means (off unless switched on) */
  ai_understand?: boolean;
  [key: string]: unknown;
}

export interface WorkflowDoc {
  schema: number;
  start: string;
  settings?: WorkflowSettings;
  nodes: WfNode[];
  edges: WfEdge[];
}

export function isMultilingual(doc: WorkflowDoc): boolean {
  return doc.settings?.multilingual !== false;
}

export interface WfIssue {
  level: "fail" | "warn";
  message: string;
  node_id: string | null;
  field: string;
}

export interface WorkflowSummary {
  key: string;
  title: string;
  description: string;
  enabled: boolean;
  priority: number;
  draft_version: number | null;
  published_version: number | null;
  updated_at: string | null;
  created_at: string | null;
  node_count?: number;
  issue_counts?: { fail: number; warn: number };
  /** Published AND switched on: answering real customers. */
  live?: boolean;
  /** How it starts, in words (from the published version when there is one). */
  starts?: string[];
}

export interface WorkflowDetail extends WorkflowSummary {
  doc: WorkflowDoc;
  published_doc: WorkflowDoc | null;
  issues: WfIssue[];
}

export interface ImportReport {
  source: "wati" | "native";
  name: string;
  description: string;
  found: Record<string, number>;
  steps: number;
  connections: number;
  adapted: string[];
  warnings: string[];
}

export interface WfVersion {
  version: number;
  status: string;
  notes: string;
  created_at: string | null;
  published_at: string | null;
  node_count: number;
  is_live: boolean;
}

export interface SimMessage {
  text: string;
  options: {
    kind: "buttons" | "list";
    items: { title: string; description: string }[];
    button_text?: string;
    section_title?: string;
  } | null;
  node_id: string;
  kind?: "text" | "media" | "product_list";
  header?: string;
  footer?: string;
  media?: { type: string; url: string; caption: string } | null;
  sections?: { title: string; rows: { title: string; description: string }[] }[] | null;
}

export interface SimAction {
  kind: string;
  detail: Record<string, unknown>;
  node_id: string;
  describe: string;
}

export interface SimTurn {
  messages: SimMessage[];
  state: Record<string, unknown>;
  stopped: string;
  /** What the step WOULD have done. A dry run never assigns a real chat or tags a real customer. */
  actions: SimAction[];
  jump_to: string;
  node: { id: string; type: string; title: string } | null;
  /** what the AI did this turn: understood a typed answer, answered from the FAQ, or was not sure */
  ai?: "" | "matched" | "answered" | "unsure" | "unavailable";
  /** a Groq key is set and the AI is not paused */
  ai_ready?: boolean;
}

// ---------------- node registry ----------------
export interface NodeMeta {
  kind: NodeKind;
  title: string;
  blurb: string;
  group: "Say" | "Route" | "Remember" | "People" | "Outside" | "Finish";
}

export const NODE_META: NodeMeta[] = [
  { kind: "message", title: "Message", group: "Say", blurb: "Say something - text, a picture, a video or a document." },
  { kind: "question", title: "Question", group: "Say", blurb: "Ask with buttons, a list, or a typed answer, and remember it." },
  { kind: "template", title: "Send template", group: "Say", blurb: "Send a Meta-approved template message." },
  { kind: "product_list", title: "Product list", group: "Say", blurb: "Show products from your WhatsApp catalogue." },
  { kind: "ai_reply", title: "Answer with AI", group: "Say", blurb: "Answer the customer's own question from your FAQ text." },
  { kind: "condition", title: "Branch", group: "Route", blurb: "Take a different path depending on an answer." },
  { kind: "delay", title: "Wait", group: "Route", blurb: "Pause before the next step, so it does not feel robotic." },
  { kind: "jump", title: "Go to workflow", group: "Route", blurb: "Hand the conversation to another workflow." },
  { kind: "set_var", title: "Remember", group: "Remember", blurb: "Store a value without asking the customer." },
  { kind: "tags", title: "Tag the chat", group: "Remember", blurb: "Label the conversation so you can filter it in WATI." },
  { kind: "subscribe", title: "Subscribe", group: "Remember", blurb: "Opt the contact in or out of campaigns." },
  { kind: "assign", title: "Assign", group: "People", blurb: "Give the chat to a person or a team in WATI." },
  { kind: "chat_status", title: "Chat status", group: "People", blurb: "Mark the conversation open, pending or solved." },
  { kind: "api_request", title: "Call an API", group: "Outside", blurb: "Fetch data from another system and use it here." },
  { kind: "data", title: "Find in your data", group: "Outside", blurb: "Look up this customer's own orders, to show or to remember." },
  { kind: "end", title: "End", group: "Finish", blurb: "Finish the conversation." },
];

export const NODE_GROUPS = ["Say", "Route", "Remember", "People", "Outside", "Finish"] as const;

export function metaOf(kind: NodeKind): NodeMeta {
  return NODE_META.find((m) => m.kind === kind) ?? NODE_META[0];
}

let seq = 0;
export function newNodeId(): string {
  seq += 1;
  return `n${Date.now().toString(36)}${seq}`;
}

export function blankNode(kind: NodeKind, x: number, y: number): WfNode {
  const base: WfNode = { id: newNodeId(), type: kind, title: metaOf(kind).title, x, y };
  if (kind === "message" || kind === "end") return { ...base, text: { en: "", hi: "", gu: "" } };
  if (kind === "question") {
    return {
      ...base,
      text: { en: "", hi: "", gu: "" },
      input: {
        kind: "buttons",
        options: [
          { value: "yes", label: { en: "Yes", hi: "", gu: "" } },
          { value: "no", label: { en: "No", hi: "", gu: "" } },
        ],
      },
      store: "",
      validate: { type: "any" },
      max_retries: 1,
    };
  }
  if (kind === "product_list") {
    return { ...base, header: { en: "" }, text: { en: "", hi: "", gu: "" }, catalog_id: "", set_id: "" };
  }
  if (kind === "condition") {
    return { ...base, branches: [{ id: "b1", label: "Rule 1", when: { var: "", op: "eq", value: "" } }] };
  }
  if (kind === "ai_reply") {
    return { ...base, text: { en: "", hi: "", gu: "" }, faq: "", tone: "friendly", max_chars: 600, store: "question" };
  }
  if (kind === "delay") return { ...base, seconds: 3 };
  if (kind === "tags") return { ...base, tags: [""], remove: false };
  if (kind === "assign") return { ...base, to: "team", teams: [""], email: "" };
  if (kind === "chat_status") return { ...base, status: "pending" };
  if (kind === "subscribe") return { ...base, subscribe: true };
  if (kind === "template") return { ...base, template_name: "", params: {} };
  if (kind === "jump") return { ...base, workflow: "" };
  if (kind === "api_request") {
    return { ...base, method: "GET", url: "", headers: {}, body: "", save: {}, routes: [] };
  }
  if (kind === "data") {
    return {
      ...base, source: "orders", find: "all", match: "", group: "so", sort: "newest", limit: DATA_ROWS_MAX,
      store: "orders", save: {}, filter: [], list_line: DATA_LIST_LINE,
    };
  }
  return { ...base, assign: {} };
}

/** Every exit a step has, in the order the canvas should draw them.
 *  MUST agree with ports_of() in backend/app/services/workflow/schema.py - when the two disagree,
 *  a connection the owner draws is silently refused on save. */
export function portsOf(node: WfNode): { port: string; label: string }[] {
  if (["message", "set_var", "delay", "tags", "chat_status", "subscribe", "product_list"].includes(node.type)) {
    return [{ port: "next", label: "Next" }];
  }
  if (node.type === "end" || node.type === "jump") return [];
  // "Not sure" is also taken when the AI cannot be reached, so it must lead somewhere
  if (node.type === "ai_reply") return [{ port: "answered", label: "Answered" }, { port: "unsure", label: "Not sure" }];
  if (node.type === "data") return [{ port: "found", label: "Found something" }, { port: "none", label: "Nothing found" }];
  // An Assign to a person ends the bot's part - the chat is theirs. Only a refusal leads on.
  if (node.type === "assign" && (node.to ?? "operator") !== "bot") return [{ port: "on_error", label: "If it fails" }];
  if (node.type === "assign" || node.type === "template") {
    return [{ port: "next", label: "Done" }, { port: "on_error", label: "If it fails" }];
  }
  if (node.type === "api_request") {
    const out = [{ port: "success", label: "Worked" }];
    for (const r of node.routes ?? []) out.push({ port: `route:${r.id}`, label: r.label || r.id });
    return [...out, { port: "failed", label: "Failed" }];
  }
  if (node.type === "condition") {
    const out = (node.branches ?? []).map((b) => ({ port: `branch:${b.id}`, label: b.label || b.id }));
    return [...out, { port: "else", label: "Otherwise" }];
  }
  const spec = node.input ?? { kind: "buttons" };
  const out: { port: string; label: string }[] = [];
  if (spec.kind === "language") {
    for (const code of offeredLanguages(spec)) {
      out.push({ port: `opt:${code}`, label: spec.language_labels?.[code]?.trim() || LANGUAGE_NAMES[code] });
    }
  } else if (spec.kind === "data_list") {
    // one exit, never one per row: which rows there are is only known while a customer is talking
    out.push({ port: "next", label: "They chose one" }, { port: "default", label: "Anything else" },
      { port: "empty", label: "Nothing to show" });
  } else if (spec.kind === "buttons" || spec.kind === "list") {
    for (const o of spec.options ?? []) out.push({ port: `opt:${o.value}`, label: o.label.en || o.value });
    // WATI's "default" route: the customer typed instead of tapping. Optional.
    out.push({ port: "default", label: "Anything else" });
  } else {
    out.push({ port: "next", label: "Answered" });
  }
  if ((node.validate?.type ?? "any") !== "any") out.push({ port: "invalid", label: "Not valid" });
  if ((node.max_retries ?? 0) > 0) out.push({ port: "retry_exhausted", label: "Gave up" });
  return out;
}

/** Text for a language, falling back to English then to anything set - the same rule the bot uses
 *  when it actually sends the message. */
export function textOf(value: I18n | undefined, lang: Lang): string {
  if (!value) return "";
  const order: Lang[] = [lang, "en", ...LANGS];
  for (const lg of order) {
    const got = value[lg];
    if (got && got.trim()) return got;
  }
  return "";
}

/** Answers a later step can use in its text or test against. */
export function variablesIn(doc: WorkflowDoc): string[] {
  const out = new Set<string>();
  for (const n of doc.nodes) {
    if ((n.type === "question" || n.type === "ai_reply") && n.store) {
      out.add(n.store);
      for (const extra of answerExtras(n)) out.add(extra);
    }
    if (n.type === "set_var") for (const k of Object.keys(n.assign ?? {})) out.add(k);
    if (n.type === "api_request") for (const k of Object.keys(n.save ?? {})) out.add(k);
    if (n.type === "data") {
      if (n.store) out.add(`${n.store}_count`), out.add(`${n.store}_list`);
      for (const k of Object.keys(n.save ?? {})) out.add(k);
    }
    if (n.type === "question" && n.input?.kind === "data_list" && n.store) {
      const from = doc.nodes.find((d) => d.type === "data" && d.store === n.input?.from);
      for (const f of DATA_FIELDS[from?.source ?? "orders"]) out.add(`${n.store}_${f}`);
      if ((from?.source ?? "orders") === "orders") {
        for (const f of LOOKUP_FIELDS.so) out.add(`${n.store}_${f}`);
      }
    }
  }
  return [...out].sort();
}

// ---------------- going live ----------------
/** The built-in order-status bot as a Go-to-workflow target. Mirrors schema.BUILTIN_ORDER_STATUS. */
export const BUILTIN_ORDER_STATUS = "@order-status";
export const IDLE_MINUTES_DEFAULT = 30;

export interface KeywordTrigger {
  text: string;
  /** exact: the whole message; contains: anywhere in it */
  match: "exact" | "contains" | "similar";
}

export interface Triggers {
  keywords: KeywordTrigger[];
  /** a row in the order-status bot's main menu */
  menu: { enabled: boolean; label: I18n };
  /** numbers that are not in the customer list */
  unknown_customer: boolean;
}

/** How a workflow starts, normalised the same way as schema.triggers_of on the server. */
export function triggersOf(doc: WorkflowDoc): Triggers {
  const raw = (doc.settings?.triggers ?? {}) as {
    keywords?: (string | Partial<KeywordTrigger>)[];
    menu?: { enabled?: boolean; label?: I18n };
    unknown_customer?: boolean;
  };
  return {
    keywords: (raw.keywords ?? []).map((k) => (typeof k === "string"
      ? { text: k, match: "exact" as const }
      : { text: k.text ?? "", match: k.match === "contains" || k.match === "similar" ? k.match : ("exact" as const) })),
    menu: { enabled: !!raw.menu?.enabled, label: raw.menu?.label ?? {} },
    unknown_customer: !!raw.unknown_customer,
  };
}

/** How a workflow starts, in words - the same phrasing the server gives the Workflows list. */
export function describeTriggers(doc: WorkflowDoc): string[] {
  const t = triggersOf(doc);
  const out = t.keywords.filter((k) => k.text.trim())
    .map((k) => `"${k.text.trim()}"${k.match === "contains" ? " (anywhere in a message)" : k.match === "similar" ? " (or a similar spelling)" : ""}`);
  if (t.menu.enabled && t.menu.label.en?.trim()) out.push(`Main-menu button "${t.menu.label.en.trim()}"`);
  if (t.unknown_customer) out.push("New numbers (not in the customer list)");
  return out;
}

export interface RoutingWorkflow {
  key: string;
  title: string;
  version: number;
  priority: number;
  /** switched on; until then only the test numbers in Settings reach it */
  for_everyone: boolean;
  keywords: KeywordTrigger[];
  menu_label: string;
  unknown_customer: boolean;
  idle_minutes: number;
  jumps_to: string[];
  reached_from: string[];
  /** customers inside it right now */
  active: number;
}

export interface RoutingOverview {
  workflows: RoutingWorkflow[];
  main_menu: Record<Lang, string[]>;
  new_numbers: string | null;
  conflicts: string[];
  /** the owner's own phones, from Settings */
  test_numbers: string[];
}

export interface WorkflowRunInfo {
  phone: string;
  workflow: string;
  title: string;
  version: number;
  status: "active" | "waiting" | "running";
  step: string;
  trigger: string;
  started_at: string | null;
  updated_at: string | null;
  due_at: string | null;
}

/** A chat a person has taken: the bot is quiet for this number until it is handed back. */
export interface HandoverInfo {
  phone: string;
  customer_name: string | null;
  assignee: string;
  source: string;
  started_at: string | null;
  last_message_at: string | null;
  /** when the bot takes the chat back by itself; null = only by hand */
  returns_at: string | null;
}

/** A number that signed up through the new-numbers workflow. */
export interface NewCustomerRow {
  phone: string;
  name: string;
  whatsapp_name: string;
  details: Record<string, string>;
  workflow: string;
  created_at: string | null;
  updated_at: string | null;
  in_customer_list: boolean;
}

// ---------------- the language question ----------------
export const LANGUAGE_NAMES: Record<Lang, string> = { en: "English", hi: "Hindi", gu: "Gujarati" };
/** What the language buttons say unless the owner words them - the Bot messages defaults. */
export const DEFAULT_LANGUAGE_LABELS: Record<Lang, string> = { en: "English", hi: "हिंदी", gu: "ગુજરાતી" };

/** Which language buttons a question offers, in the usual order. Mirrors schema.offered_languages. */
export function offeredLanguages(spec: WfInput | undefined): Lang[] {
  const got = (spec?.languages ?? LANGS).filter((c) => LANGS.includes(c));
  return got.length ? LANGS.filter((c) => got.includes(c)) : [...LANGS];
}

// ---------------- what a question can ask ----------------
export interface QuestionPreset {
  id: string;
  label: string;
  kind: InputKind;
  validate?: WfNode["validate"];
  store?: string;
  text?: I18n;
  invalid?: I18n;
  retries?: number;
  options?: WfOption[];
}

/** The things an owner usually asks, each with its checks and a ready-made question in all three
 *  languages - written by hand, so they read better than a machine translation. */
export const QUESTION_PRESETS: QuestionPreset[] = [
  { id: "yes_no", label: "Yes / No", kind: "buttons", store: "answer",
    text: { en: "Would you like to go ahead?", hi: "क्या आप आगे बढ़ना चाहेंगे?", gu: "શું તમે આગળ વધવા માંગો છો?" },
    options: [{ value: "yes", label: { en: "Yes", hi: "हाँ", gu: "હા" } }, { value: "no", label: { en: "No", hi: "नहीं", gu: "ના" } }] },
  { id: "buttons", label: "A choice (buttons)", kind: "buttons" },
  { id: "list", label: "A choice (list)", kind: "list",
    options: [{ value: "o1", label: { en: "First choice" } }, { value: "o2", label: { en: "Second choice" } }] },
  { id: "language", label: "Language", kind: "language",
    text: { en: "Please choose your language.", hi: "कृपया अपनी भाषा चुनें।", gu: "કૃપા કરીને તમારી ભાષા પસંદ કરો." } },
  { id: "name", label: "Name", kind: "text", store: "name",
    text: { en: "May I know your name?", hi: "क्या मैं आपका नाम जान सकता हूँ?", gu: "શું હું તમારું નામ જાણી શકું?" } },
  { id: "company", label: "Company", kind: "text", store: "company",
    text: { en: "What is your company name?", hi: "आपकी कंपनी का नाम क्या है?", gu: "તમારી કંપનીનું નામ શું છે?" } },
  { id: "phone", label: "Phone number", kind: "text", validate: { type: "phone" }, store: "phone", retries: 2,
    text: { en: "Please share your phone number with the country code.", hi: "कृपया देश के कोड के साथ अपना फ़ोन नंबर बताएं।", gu: "કૃપા કરીને દેશના કોડ સાથે તમારો ફોન નંબર જણાવો." },
    invalid: { en: "That doesn't look like a phone number. Please type it again with the country code.", hi: "यह फ़ोन नंबर सही नहीं लगता। कृपया देश के कोड के साथ दोबारा लिखें।", gu: "આ ફોન નંબર સાચો લાગતો નથી. કૃપા કરીને દેશના કોડ સાથે ફરી લખો." } },
  { id: "email", label: "Email", kind: "text", validate: { type: "email" }, store: "email", retries: 2,
    text: { en: "What is your email address?", hi: "आपका ईमेल पता क्या है?", gu: "તમારું ઈમેલ સરનામું શું છે?" },
    invalid: { en: "That doesn't look like an email address. Please type it again.", hi: "यह ईमेल पता सही नहीं लगता। कृपया दोबारा लिखें।", gu: "આ ઈમેલ સરનામું સાચું લાગતું નથી. કૃપા કરીને ફરી લખો." } },
  { id: "city", label: "City", kind: "text", store: "city",
    text: { en: "Which city are you in?", hi: "आप किस शहर में हैं?", gu: "તમે કયા શહેરમાં છો?" } },
  { id: "quantity", label: "Quantity", kind: "text", validate: { type: "number", min: "1" }, store: "quantity", retries: 2,
    text: { en: "How many pieces do you need?", hi: "आपको कितने पीस चाहिए?", gu: "તમને કેટલા પીસ જોઈએ છે?" },
    invalid: { en: "Please type a number, for example 500.", hi: "कृपया एक संख्या लिखें, जैसे 500।", gu: "કૃપા કરીને એક સંખ્યા લખો, જેમ કે 500." } },
  { id: "date", label: "Date", kind: "text", validate: { type: "date" }, store: "date", retries: 2,
    text: { en: "Which date suits you? Please type it like 25/12/2026.", hi: "आपके लिए कौन-सी तारीख ठीक है? कृपया इस तरह लिखें: 25/12/2026", gu: "તમારા માટે કઈ તારીખ યોગ્ય છે? કૃપા કરીને આ રીતે લખો: 25/12/2026" },
    invalid: { en: "Please type the date like 25/12/2026.", hi: "कृपया तारीख इस तरह लिखें: 25/12/2026", gu: "કૃપા કરીને તારીખ આ રીતે લખો: 25/12/2026" } },
  { id: "website", label: "Website", kind: "text", validate: { type: "url" }, store: "website", retries: 2,
    text: { en: "What is your website address?", hi: "आपकी वेबसाइट का पता क्या है?", gu: "તમારી વેબસાઇટનું સરનામું શું છે?" },
    invalid: { en: "Please type the full website address, like www.example.com.", hi: "कृपया पूरा वेबसाइट पता लिखें, जैसे www.example.com", gu: "કૃપા કરીને આખું વેબસાઇટ સરનામું લખો, જેમ કે www.example.com" } },
  { id: "so_check", label: "SO number (checked)", kind: "text", validate: { type: "lookup", source: "so" }, store: "order", retries: 2,
    text: { en: "Please type your SO number.", hi: "कृपया अपना SO नंबर लिखें।", gu: "કૃપા કરીને તમારો SO નંબર લખો." },
    invalid: { en: "I couldn't find that SO number in your orders. Please check it and type it again.", hi: "यह SO नंबर आपके ऑर्डर में नहीं मिला। कृपया जाँचकर दोबारा लिखें।", gu: "આ SO નંબર તમારા ઓર્ડરમાં મળ્યો નથી. કૃપા કરીને તપાસીને ફરી લખો." } },
  { id: "po_check", label: "PO number (checked)", kind: "text", validate: { type: "lookup", source: "po" }, store: "order", retries: 2,
    text: { en: "Please type your PO number.", hi: "कृपया अपना PO नंबर लिखें।", gu: "કૃપા કરીને તમારો PO નંબર લખો." },
    invalid: { en: "I couldn't find that PO number in your orders. Please check it and type it again.", hi: "यह PO नंबर आपके ऑर्डर में नहीं मिला। कृपया जाँचकर दोबारा लिखें।", gu: "આ PO નંબર તમારા ઓર્ડરમાં મળ્યો નથી. કૃપા કરીને તપાસીને ફરી લખો." } },
  { id: "file", label: "Photo / document", kind: "text", validate: { type: "file" }, store: "file", retries: 2,
    text: { en: "Please send the photo or document here.", hi: "कृपया फ़ोटो या दस्तावेज़ यहाँ भेजें।", gu: "કૃપા કરીને ફોટો અથવા દસ્તાવેજ અહીં મોકલો." },
    invalid: { en: "Please send it as a photo or a document.", hi: "कृपया इसे फ़ोटो या दस्तावेज़ के रूप में भेजें।", gu: "કૃપા કરીને તેને ફોટો અથવા દસ્તાવેજ તરીકે મોકલો." } },
  { id: "location", label: "Location", kind: "text", validate: { type: "location" }, store: "location", retries: 2,
    text: { en: "Please share your location (tap 📎, then Location).", hi: "कृपया अपनी लोकेशन भेजें (📎 दबाएँ, फिर लोकेशन)।", gu: "કૃપા કરીને તમારું લોકેશન મોકલો (📎 દબાવો, પછી લોકેશન)." },
    invalid: { en: "Please share it as a location from WhatsApp.", hi: "कृपया इसे WhatsApp से लोकेशन के रूप में भेजें।", gu: "કૃપા કરીને તેને WhatsApp થી લોકેશન તરીકે મોકલો." } },
  { id: "time", label: "Time", kind: "text", validate: { type: "time" }, store: "time", retries: 2,
    text: { en: "What time suits you? For example 11:30 or 4 pm.", hi: "आपके लिए कौन-सा समय ठीक है? जैसे 11:30 या 4 pm।", gu: "તમારા માટે કયો સમય યોગ્ય છે? જેમ કે 11:30 અથવા 4 pm." },
    invalid: { en: "Please type a time, like 11:30 or 4 pm.", hi: "कृपया समय लिखें, जैसे 11:30 या 4 pm।", gu: "કૃપા કરીને સમય લખો, જેમ કે 11:30 અથવા 4 pm." } },
  { id: "free", label: "Anything they type", kind: "text", validate: { type: "any" } },
];

const PRESET_TEXTS = new Set(QUESTION_PRESETS.flatMap((p) => [p.text?.en, p.invalid?.en]).filter(Boolean) as string[]);
const PRESET_STORES = new Set(QUESTION_PRESETS.map((p) => p.store).filter(Boolean) as string[]);

/** Switch a question to a preset, keeping anything the owner already wrote themselves. */
export function applyPreset(node: WfNode, p: QuestionPreset): Partial<WfNode> {
  const mine = (v?: I18n) => !!v?.en?.trim() && !PRESET_TEXTS.has(v.en.trim());
  const keepOptions = !!node.input?.options?.length && p.id !== "yes_no";
  const patch: Partial<WfNode> = {
    input: { ...(node.input ?? { kind: p.kind }), kind: p.kind, ...(p.options && !keepOptions ? { options: p.options } : {}) },
    validate: p.validate ?? { type: "any" },
  };
  if (p.store !== undefined && (!node.store || PRESET_STORES.has(node.store))) patch.store = p.store;
  if (p.text && !mine(node.text)) patch.text = p.text;
  if (p.invalid && !mine(node.invalid_text)) patch.invalid_text = p.invalid;
  if (p.retries !== undefined) patch.max_retries = p.retries;
  return patch;
}

/** Which preset a question currently matches, if any - for highlighting it. */
export function presetOf(node: WfNode): string {
  const kind = node.input?.kind;
  const rule = node.validate?.type ?? "any";
  const source = node.validate?.source ?? "";
  const hit = QUESTION_PRESETS.find((p) => p.kind === kind && (p.validate?.type ?? "any") === rule
    && (p.validate?.source ?? "") === source
    && (p.kind !== "text" || rule !== "any" || (p.store ?? "") === (node.store ?? "") || p.id === "free"));
  return hit?.id ?? "";
}

// ---------------- every text in a workflow, for Translate ----------------
export interface DocString { nodeId: string; path: string; value: I18n }

/** Every customer-visible text, with where it lives. Mirrors schema.walk_strings. */
export function stringsOf(doc: WorkflowDoc): DocString[] {
  const out: DocString[] = [];
  const add = (nodeId: string, path: string, value: I18n | undefined) => {
    if (value && typeof value === "object") out.push({ nodeId, path, value });
  };
  for (const n of doc.nodes) {
    add(n.id, "text", n.text);
    add(n.id, "header", n.header);
    add(n.id, "footer", n.footer);
    add(n.id, "media.caption", n.media?.caption);
    add(n.id, "invalid_text", n.invalid_text);
    add(n.id, "retry_text", n.retry_text);
    add(n.id, "input.button_text", n.input?.button_text);
    add(n.id, "input.section_title", n.input?.section_title);
    (n.input?.options ?? []).forEach((o, i) => {
      add(n.id, `input.options.${i}.label`, o.label);
      add(n.id, `input.options.${i}.description`, o.description);
      add(n.id, `input.options.${i}.section`, o.section);
    });
  }
  return out;
}

/** The value at a dotted path inside a step ("input.options.2.label"). */
export function getAt(node: WfNode | undefined, path: string): unknown {
  let cur: unknown = node;
  for (const key of path.split(".")) {
    if (cur === null || typeof cur !== "object") return undefined;
    cur = (cur as Record<string, unknown>)[key];
  }
  return cur;
}

/** A copy of the step with the value at a dotted path replaced. */
export function setAt(node: WfNode, path: string, value: unknown): WfNode {
  const root = JSON.parse(JSON.stringify(node)) as Record<string, unknown>;
  const keys = path.split(".");
  let cur: Record<string, unknown> = root;
  keys.slice(0, -1).forEach((key, i) => {
    if (cur[key] === null || typeof cur[key] !== "object") cur[key] = /^\d+$/.test(keys[i + 1]) ? [] : {};
    cur = cur[key] as Record<string, unknown>;
  });
  cur[keys[keys.length - 1]] = value;
  return root as unknown as WfNode;
}

/** Keep a step's connections in step with its exits. A button whose id is edited keeps its
 *  connection; an exit that no longer exists (a deleted button, a changed answer type, a language
 *  taken off) loses it - otherwise an invisible connection would block publishing. */
export function rewire(edges: WfEdge[], before: WfNode, after: WfNode): WfEdge[] {
  const was = before.input?.options ?? [];
  const now = after.input?.options ?? [];
  const renamed = new Map<string, string>();
  if (before.input?.kind === after.input?.kind && was.length === now.length) {
    was.forEach((o, i) => { if (o.value !== now[i].value) renamed.set(`opt:${o.value}`, `opt:${now[i].value}`); });
  }
  const exits = new Set(portsOf(after).map((p) => p.port));
  return edges.flatMap((e) => {
    if (e.from !== after.id) return [e];
    const port = renamed.get(e.port) ?? e.port;
    return exits.has(port) ? [{ ...e, port }] : [];
  });
}

// ---------------- checking an answer against the business's data ----------------
export type LookupSource = "so" | "po" | "fg" | "customer_code";
export const LOOKUP_SOURCES: { value: LookupSource; label: string }[] = [
  { value: "so", label: "An SO number from their own orders" },
  { value: "po", label: "A PO number from their own orders" },
  { value: "fg", label: "An item code from their own orders" },
  { value: "customer_code", label: "Their customer code (from your customer list)" },
];
/** What is saved beside the answer once it is found. Mirrors schema.LOOKUP_FIELDS. */
export const LOOKUP_FIELDS: Record<LookupSource, string[]> = {
  so: ["status", "so", "po", "items", "count"], po: ["status", "so", "po", "items", "count"],
  fg: ["status", "so", "po", "items", "count"], customer_code: ["name"],
};

// ---------------- finding things in your own data ----------------
/** WhatsApp shows at most 10 rows, so a data step never keeps more. Mirrors schema.DATA_ROWS_MAX. */
export const DATA_ROWS_MAX = 10;
export const DATA_LIST_LINE = "SO {so_no} - {real_status}";
/** The fields a workflow may read. Names are the bot's own and never change - only their labels
 *  follow the owner's column map, which is why those are fetched. connection_status is not here:
 *  it is the internal status and never reaches a customer. Mirrors schema.DATA_FIELDS. */
export const DATA_FIELDS: Record<string, string[]> = {
  orders: ["so_no", "po_no", "fg_item_code", "fg_description", "real_status", "customer_name"],
  customer: ["customer_code", "customer_name"],
};
/** Fields no two rows share, so a customer can tell one row from another. */
export const DATA_KEY_FIELDS = ["so_no", "po_no", "fg_item_code", "customer_code"];

export interface DataField { name: string; label: string; values: string[] }
export interface DataSource { value: string; label: string; finds: { value: string; label: string }[]; fields: DataField[] }
export interface DataFieldsInfo {
  sources: DataSource[];
  ops: { value: string; label: string }[];
  sorts: { value: string; label: string }[];
  groups: { value: string; label: string }[];
}

/** Values a question saves beside its answer, like {order_status}. Mirrors schema.answer_extras. */
export function answerExtras(n: WfNode): string[] {
  const store = n.store ?? "";
  const rule = n.validate;
  if (!store || !rule) return [];
  if (rule.type === "lookup" && rule.source) return LOOKUP_FIELDS[rule.source].map((f) => `${store}_${f}`);
  if (rule.type === "file" || rule.type === "location") return [`${store}_type`, `${store}_address`];
  return [];
}

// ---------------- the owner's own questions ----------------
export interface SavedQuestion { id: number; label: string; spec: Partial<WfNode> }

/** A blank question to write from scratch - keeping anything the owner already wrote themselves. */
export function ownQuestion(node: WfNode): Partial<WfNode> {
  const mine = (v?: I18n) => !!v?.en?.trim() && !PRESET_TEXTS.has(v.en.trim());
  return {
    input: { ...(node.input ?? { kind: "text" }), kind: "text" },
    validate: { type: "any" },
    text: mine(node.text) ? node.text : { en: "", hi: "", gu: "" },
    invalid_text: mine(node.invalid_text) ? node.invalid_text : undefined,
    store: node.store && !PRESET_STORES.has(node.store) ? node.store : "",
  };
}

/** The settings a saved question carries - never a position or a connection. */
export function questionSpec(node: WfNode): Partial<WfNode> {
  const { input, validate, store, text, invalid_text, retry_text, max_retries, header, footer } = node;
  return JSON.parse(JSON.stringify({ input, validate, store, text, invalid_text, retry_text, max_retries, header, footer }));
}

// ---------------- the AI assistant (Groq) ----------------
/** The longest FAQ an Answer-with-AI step may hold. Mirrors schema.AI_FAQ_MAX. */
export const AI_FAQ_MAX = 8000;

export interface AiStatus { configured: boolean; builder_model: string; live_model: string; translate_provider: string }

/** What the assistant built: a preview until the owner applies it. */
export interface AiBuildResult {
  doc: WorkflowDoc;
  issues: WfIssue[];
  /** steps the AI pointed at that do not exist */
  notes: string[];
  title: string;
  summary: string;
  tokens: number;
  mode: "new" | "edit";
  /** why fixing stopped early, e.g. Groq's limit */
  note: string;
  /** Create with AI: the new workflow's key */
  key?: string;
}

export interface AiJob {
  id: string;
  status: "running" | "done" | "failed";
  stage: string;
  /** seconds left of a wait for Groq's per-minute limit */
  wait_seconds: number;
  result: AiBuildResult | null;
  error: string;
}

export interface AiFinding { step_id: string | null; severity: "problem" | "suggestion"; problem: string; suggestion: string }
export interface AiReview { summary: string; findings: AiFinding[] }
