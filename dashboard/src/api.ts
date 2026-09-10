const KEY_STORAGE = "admin_key";

export function getKey(): string {
  try {
    return sessionStorage.getItem(KEY_STORAGE) || "";
  } catch {
    return "";
  }
}
export function setKey(k: string) {
  try {
    sessionStorage.setItem(KEY_STORAGE, k);
  } catch {}
}
export function clearKey() {
  try {
    sessionStorage.removeItem(KEY_STORAGE);
  } catch {}
}

export class ApiError extends Error {
  status: number;
  constructor(status: number, msg: string) {
    super(msg);
    this.status = status;
  }
}

async function req<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers: Record<string, string> = { "X-Admin-Key": getKey(), ...(init.headers as Record<string, string> | undefined) };
  if (init.body && !(init.body instanceof FormData)) headers["Content-Type"] = "application/json";
  const r = await fetch(`/admin/api${path}`, { ...init, headers });
  if (!r.ok) {
    let detail = r.statusText;
    try {
      detail = (await r.json()).detail || detail;
    } catch {}
    throw new ApiError(r.status, detail);
  }
  return (await r.json()) as T;
}

export const api = {
  overview: () => req<Overview>("/overview"),
  sessions: () => req<SessionRow[]>("/sessions"),
  session: (phone: string) => req<SessionDetail>(`/sessions/${phone}`),
  resetSession: (phone: string) => req<{ ok: boolean }>(`/sessions/${phone}/reset`, { method: "POST" }),
  messages: (p: Record<string, string | number>) => req<Paged<Msg>>(`/messages?${qs(p)}`),
  mismatches: () => req<Mismatch[]>("/mismatches"),
  imports: (kind?: string) => req<SyncRun[]>(`/imports${kind ? `?kind=${kind}` : ""}`),
  importCustomers: (file?: File) => {
    const fd = new FormData();
    if (file) fd.append("file", file);
    return req<SyncRun>("/import-customers", { method: "POST", body: file ? fd : undefined });
  },
  refreshOrders: () => req<SyncRun>("/refresh-orders", { method: "POST" }),
  testFetch: () => req<Preview>("/test-fetch", { method: "POST" }),
  ordersSource: () => req<OrdersSource>("/orders-source"),
  customers: (q = "") => req<CustomerRow[]>(`/customers?q=${encodeURIComponent(q)}`),
  orders: (q = "") => req<OrderRow[]>(`/orders?q=${encodeURIComponent(q)}`),
  outbox: () => req<Outbound[]>("/outbox"),
  queue: () => req<QueueRow[]>("/queue"),
  simulate: (phone: string, text: string, type: "text" | "audio" = "text", selection?: Selection) =>
    req<SimResult>("/simulate", { method: "POST", body: JSON.stringify({ phone, text, type, selection }) }),
  // go-live readiness
  readiness: (deep = true) => req<Readiness>(`/readiness?deep=${deep}`),
  diagnostics: () => req<Diagnostics>("/diagnostics"),
  webhookSelfTest: () => req<{ ok: boolean; url: string; status?: number; detail: string }>("/wati/self-test", { method: "POST" }),
  watiWebhooks: () => req<{ ok: boolean; detail?: string; webhooks: Record<string, unknown>[] }>("/wati/webhooks"),
  // data connections
  connections: () => req<Connections>("/connections"),
  saveConnections: (values: Record<string, unknown>) => req<{ ok: boolean; errors: Record<string, string>; fields?: Record<string, ConnField> }>("/connections", { method: "PUT", body: JSON.stringify({ values }) }),
  resetConnections: (keys: string[]) => req<{ ok: boolean; fields: Record<string, ConnField> }>("/connections/reset", { method: "POST", body: JSON.stringify({ keys }) }),
  testOrders: (values: Record<string, unknown>) => req<Preview>("/connections/orders/test", { method: "POST", body: JSON.stringify({ values }) }),
  testCustomers: (values: Record<string, unknown>) => req<CustomerTest>("/connections/customers/test", { method: "POST", body: JSON.stringify({ values }) }),
  // template editor
  templates: () => req<Catalog>("/templates"),
  templatePreview: (p: { kind: string; key: string; lang: string; text: string }) => req<{ errors: string[]; rendered: string }>("/templates/preview", { method: "POST", body: JSON.stringify(p) }),
  templateSave: (kind: string, key: string, texts: Record<string, string>) => req<{ ok: boolean; errors: Record<string, string[]> }>(`/templates/${kind}/${key}`, { method: "PUT", body: JSON.stringify({ texts }) }),
  templateReset: (kind: string, key: string) => req<{ ok: boolean }>(`/templates/${kind}/${key}`, { method: "DELETE" }),
  templateHistory: (kind: string, key: string) => req<{ id: number; lang: string; text: string | null; action: string; changed_at: string }[]>(`/templates/${kind}/${key}/history`),
  templateButtons: (key: string, buttons: string[]) => req<{ ok: boolean; errors: string[]; buttons: string[] }>(`/templates/buttons/${key}`, { method: "PUT", body: JSON.stringify({ buttons }) }),
  templateTestSend: (p: { kind: string; key: string; lang: string; phone: string; text?: string }) => req<TestSendResult>("/templates/test-send", { method: "POST", body: JSON.stringify(p) }),
  watiStatus: () => req<WatiStatus>("/templates/wati-status"),
  customSave: (c: CustomReply) => req<{ ok: boolean; errors: string[] }>("/templates/custom", { method: "POST", body: JSON.stringify(c) }),
  customTest: (text: string) => req<{ match: CustomReply | null }>("/templates/custom/test", { method: "POST", body: JSON.stringify({ text }) }),
};

export interface Diagnostics {
  verdict: string; wati_mocked: boolean;
  inbound: { at: string; client_ip: string | null; status: number; outcome: string; reason: string | null; phone: string | null; event_type: string | null; wati_msg_id: string | null }[];
  outbound: { at: string; phone: string; kind: string; sent: boolean | null; error: string | null; text: string; simulated: boolean }[];
  failed_queue: { at: string; phone: string; status: string; attempts: number; error: string | null }[];
  alerts: { title: string; detail: string; level: string; at: string }[];
}

export type CheckStatus = "pass" | "warn" | "fail";
export interface Check { key: string; title: string; status: CheckStatus; detail: string; fix: string; where: string; group: string }
export interface Readiness {
  ready: boolean; mode: string; deep: boolean; checked_at: string;
  counts: { pass: number; warn: number; fail: number }; checks: Check[];
}
export interface ConnField { value: unknown; is_set: boolean | null; secret: boolean; from_db: boolean; choices: string[]; type: string }
export interface Connections {
  fields: Record<string, ConnField>; column_fields: string[]; required_columns: string[]; customers_source_effective: string;
  last_orders_preview: Preview | null; jobs: { id: string; next_run: string | null; trigger: string }[]; env_note: string;
}
export interface CustomerTest {
  ok: boolean; source: string; error: string | null; headers: string[]; warnings?: string[];
  accepted: number; rejected: number; rejected_rows: Record<string, string | number | null>[];
  sample: { phone: string; code: string | null; name: string; raw_contact: string | null }[];
}

export type Lang = "en" | "hi" | "gu";
export interface LangText { default: string; text: string; overridden: boolean }
export interface CatalogTemplate {
  key: string; title: string; when: string; allowed: string[]; required: string[]; max_len: number; trilingual: boolean; neutral: boolean; menu: string;
  langs: Record<Lang, LangText>; buttons: string[]; buttons_editable: boolean; buttons_default: string[]; buttons_overridden: boolean; in_flow: boolean;
}
export interface FlowNode { key: string; kind: "customer" | "bot"; title: string; when?: string; text?: string; col: number; row: number }
export interface FlowEdge { from: string; to: string; label: string }
export interface WatiStatus { connected: boolean; mocked: boolean; detail: string; base_url: string; api_version: string }
export interface TestSendResult { ok: boolean; mocked?: boolean; sent_to?: string; text?: string; options?: MenuOptions | null; detail: string }
export interface CatalogLabel { key: string; title: string; when: string; max_len: number; placeholders: string[]; intent: string | null; langs: Record<Lang, LangText> }
export interface CustomReply { key: string; title: string; triggers: string[]; texts: Record<string, string>; buttons: string[]; enabled: boolean }
export interface Catalog {
  templates: CatalogTemplate[]; labels: CatalogLabel[]; custom: CustomReply[]; sample: Record<string, string | number>;
  languages: Record<Lang, string>; button_choices: { key: string; label: string }[]; loaded_at: string | null;
  placeholder_labels: Record<string, string>;
  so_menu_style: "auto" | "list";
  flow: { nodes: FlowNode[]; edges: FlowEdge[] };
}

function qs(p: Record<string, string | number>) {
  return Object.entries(p)
    .filter(([, v]) => v !== "" && v !== undefined && v !== null)
    .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`)
    .join("&");
}

// ---- types ----
export interface Health {
  status: string; db: boolean; mode: string; wati_mocked: boolean; orders_source: string;
  last_customer_sync: string | null; last_order_refresh: string | null; orders_fetched_at: string | null; orders_stale: boolean; time: string;
}
export interface Overview {
  health: Health;
  counts: { customers: number; orders: number; distinct_so: number; active_sessions: number; mismatches: number; queued: number; failed_queue: number };
  messages_per_day: { date: string; in: number; out: number }[];
  outcomes: { outcome: string; count: number }[];
  last_runs: { customers: SyncRun | null; orders: SyncRun | null };
  jobs: { id: string; next_run: string | null; trigger: string }[];
  alerts: { title: string; detail: string; level: string; at: string }[];
  config: Record<string, string | number | boolean>;
}
export interface SessionRow {
  phone: string; step: string; so_no: string | null; po_no: string | null; fg_code: string | null; pending_value: string | null;
  pending_kind: string | null; attempts: number; language: string; lang_chosen?: boolean; updated_at: string | null; customer_name?: string | null;
}
export interface OptionItem { title: string; description: string }
export interface MenuOptions { kind: "buttons" | "list"; items: OptionItem[]; button_text: string; section_title: string; header: string; footer: string }
export interface Selection { kind: "buttons" | "list"; title: string; description?: string }
export interface Msg {
  id: number; wati_msg_id: string | null; phone: string; direction: "in" | "out"; type: string; text: string | null;
  transcript: string | null; outcome: string | null; step_after: string | null; created_at: string | null; options?: MenuOptions | null;
}
export interface SessionDetail { session: SessionRow | null; customer: { name: string; code: string | null } | null; messages: Msg[] }
export interface Paged<T> { total: number; page: number; page_size: number; items: T[] }
export interface Mismatch { id: number; phone: string; excel_name: string; api_name: string; so_no: string | null; created_at: string }
export interface SyncRun {
  id: number; kind: string; source: string | null; started_at: string; finished_at: string | null; ok: boolean; total_rows: number;
  accepted: number; rejected: number; rejected_rows: Record<string, string | number | null>[]; raw_headers: string[]; warnings: string[]; error: string | null;
}
export interface Preview {
  ok: boolean; source: string; headers: string[]; resolved: Record<string, string>; warnings: string[]; missing: string[];
  total_raw: number; mapped: number; sample: Record<string, string | null>[]; error: string | null; at: string;
}
export interface OrdersSource {
  source: string; format: string; file_path: string | null; api_url: string; api_method: string; api_key_in: string; api_key_name: string;
  api_key_set: boolean; sql_url_set: boolean; column_map: Record<string, string>; last_preview: Preview | null;
}
export interface CustomerRow { phone: string; code: string | null; name: string; raw_contact: string | null; imported_at: string | null; so_numbers: string[] }
export interface OrderRow {
  id: number; so_no: string; po_no: string | null; fg_item_code: string | null; customer_name: string; connection_status: string | null; real_status: string | null; fetched_at: string;
}
export interface Outbound { phone: string; text: string; kind: string; at: string; options?: MenuOptions; sent?: boolean }
export interface QueueRow { id: number; phone: string; status: string; attempts: number; error: string | null; created_at: string; updated_at: string }
export interface SimResult {
  queued: { status: string; queue_id?: number; reason?: string }; queue_status?: string; queue_error?: string | null;
  inbound: Msg | null; reply: Msg | null; replies?: Msg[]; session: SessionRow | null;
}
