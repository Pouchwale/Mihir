import { useEffect, useMemo, useRef, useState } from "react";
import { api, ConnField, Connections, CustomerTest, Preview } from "../api";
import { Choice, ColumnPicker, Num, ResultBox, Row, Secret, SourceCard, Text } from "../ConnectionFields";
import { Badge, Empty, ErrorBox, fmt, usePoll } from "../ui";

type Tab = "history" | "orders" | "customers";

const COLUMN_LABELS: Record<string, string> = {
  so_no: "Order number (SO)", po_no: "Customer PO number", fg_item_code: "Item code (FG)",
  fg_description: "Item description (shown to the customer)",
  customer_name: "Customer name", connection_status: "Connection status (internal)", real_status: "Real status (sent to customer)",
};

const CUSTOMER_COLUMNS = ["customers_col_contact", "customers_col_name", "customers_col_code"];
const CUSTOMER_REQUIRED = ["customers_col_contact", "customers_col_name"];
const CUSTOMER_LABELS: Record<string, string> = {
  customers_col_contact: "WhatsApp number",
  customers_col_name: "Customer name (must match the PPC table exactly)",
  customers_col_code: "Customer code",
};

/** The columns to show now: the required ones, whatever is mapped, and anything just added by hand.
 *  A column nobody uses is noise on this page, so an unmapped optional one is offered rather than
 *  listed. */
function shownColumns(all: string[], required: string[], mapped: (k: string) => boolean, added: string[]) {
  return all.filter((k) => required.includes(k) || mapped(k) || added.includes(k));
}

function AddColumn({ options, labels, onAdd }: {
  options: string[]; labels: Record<string, string>; onAdd: (k: string) => void;
}) {
  if (options.length === 0) return null;
  return (
    <select className="input text-sm w-full mt-2" value="" aria-label="Add a column"
      onChange={(e) => e.target.value && onAdd(e.target.value)}>
      <option value="">+ Add a column…</option>
      {options.map((k) => <option key={k} value={k}>{labels[k] || k}</option>)}
    </select>
  );
}

export default function Imports() {
  const [tab, setTab] = useState<Tab>("history");
  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-xl font-semibold">Data sources</h1>
        <p className="text-sm text-slate-500">Where the bot gets its two data sets, and every import it has run. Changes here apply straight away — no restart.</p>
      </div>
      <div className="flex flex-wrap gap-1">
        {([["history", "Import history"], ["orders", "Order data (PPC)"], ["customers", "Customer Excel (SAP)"]] as const).map(([k, label]) => (
          <button key={k} onClick={() => setTab(k)} className={`btn ${tab === k ? "bg-brand-600 text-white border-brand-600" : "bg-white border-slate-300"}`}>{label}</button>
        ))}
      </div>
      {tab === "history" && <History />}
      {tab === "orders" && <OrdersConnection />}
      {tab === "customers" && <CustomersConnection />}
    </div>
  );
}

/** Shared form state: a working copy of the saved settings, sending only what actually changed. */
function useConnForm() {
  const { data, error, reload } = usePoll<Connections>(() => api.connections(), 0);
  const [vals, setVals] = useState<Record<string, unknown>>({});
  const [initial, setInitial] = useState<Record<string, unknown>>({});
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!data) return;
    const v: Record<string, unknown> = {};
    Object.entries(data.fields).forEach(([k, f]) => { v[k] = f.secret ? null : f.value; });
    setVals(v);
    setInitial(v);
  }, [data]);

  const set = (k: string, v: unknown) => setVals((old) => ({ ...old, [k]: v }));
  // typed accessors: a generic get() would narrow "file" to a literal type and break comparisons
  const str = (k: string, fallback = ""): string => { const v = vals[k]; return v === undefined || v === null ? fallback : String(v); };
  const num = (k: string, fallback: number): number => { const v = vals[k]; return v === undefined || v === null || v === "" ? fallback : Number(v); };
  const obj = <T,>(k: string, fallback: T): T => (vals[k] === undefined || vals[k] === null ? fallback : (vals[k] as T));
  const field = (k: string): ConnField | undefined => data?.fields[k];

  const changed = useMemo(() => {
    const out: Record<string, unknown> = {};
    Object.entries(vals).forEach(([k, v]) => {
      const f = data?.fields[k];
      if (!f) return;
      if (f.secret) { if (v !== null) out[k] = v; return; }
      if (JSON.stringify(v) !== JSON.stringify(initial[k])) out[k] = v;
    });
    return out;
  }, [vals, initial, data]);

  const save = async (extra: Record<string, unknown> = {}) => {
    setBusy(true); setMsg(null); setErrors({});
    try {
      const payload = { ...changed, ...extra };
      if (!Object.keys(payload).length) { setMsg("Nothing to save."); return; }
      const r = await api.saveConnections(payload);
      if (r.ok) { setMsg("Saved. The bot uses this from now on."); await reload(); }
      else { setErrors(r.errors); setMsg("Please fix the marked fields."); }
    } catch (e) { setMsg((e as Error).message); } finally { setBusy(false); }
  };

  /** Values to send to a Test button: what is on screen, secrets only when retyped. */
  const draft = () => {
    const out: Record<string, unknown> = {};
    Object.entries(vals).forEach(([k, v]) => {
      const f = data?.fields[k];
      if (!f) return;
      if (f.secret) { if (v !== null && v !== "") out[k] = v; return; }
      out[k] = v;
    });
    return out;
  };

  return { data, error, reload, vals, set, str, num, obj, field, errors, msg, setMsg, busy, setBusy, save, draft, dirty: Object.keys(changed).length > 0 };
}

// ---------------- orders ----------------
function OrdersConnection() {
  const f = useConnForm();
  const [test, setTest] = useState<Preview | null>(null);
  const [running, setRunning] = useState(false);

  useEffect(() => { if (f.data?.last_orders_preview && !test) setTest(f.data.last_orders_preview); }, [f.data, test]);

  const source = f.str("orders_source", "file");
  const headers = test?.headers ?? [];
  const map = f.obj<Record<string, string>>("orders_column_map", {});
  const setMap = (k: string, v: string) => f.set("orders_column_map", { ...map, [k]: v });
  const [added, setAdded] = useState<string[]>([]);

  const runTest = async () => {
    f.setBusy(true); f.setMsg(null);
    try { setTest(await api.testOrders(f.draft())); } catch (e) { f.setMsg((e as Error).message); } finally { f.setBusy(false); }
  };
  const refreshNow = async () => {
    setRunning(true);
    try { const r = await api.refreshOrders(); f.setMsg(r.ok ? `Loaded ${r.accepted} order rows.` : `Failed: ${r.error}`); }
    catch (e) { f.setMsg((e as Error).message); } finally { setRunning(false); }
  };

  if (f.error) return <ErrorBox msg={f.error} />;
  if (!f.data) return <div className="text-slate-500">Loading…</div>;

  return (
    <div className="grid xl:grid-cols-2 gap-4">
      <div className="card space-y-3">
        <div className="font-medium">Where the order table comes from</div>
        <div className="grid sm:grid-cols-3 gap-2">
          <SourceCard active={source === "http"} title="API endpoint" note="A URL from your PPC/ERP system, with an API key" onClick={() => f.set("orders_source", "http")} />
          <SourceCard active={source === "sql"} title="Database query" note="Read straight from a MySQL / SQL Server database" onClick={() => f.set("orders_source", "sql")} />
          <SourceCard active={source === "file"} title="File on this server" note="A file that another system writes for you" onClick={() => f.set("orders_source", "file")} />
        </div>

        {source === "http" && (
          <div className="space-y-3">
            <Row label="Endpoint URL" hint="The address that returns the whole BOM PPC table.">
              <Text value={f.str("orders_api_url", "")} onChange={(v) => f.set("orders_api_url", v)} placeholder="https://ppc.yourcompany.com/api/orders" error={f.errors.orders_api_url} />
            </Row>
            <div className="grid sm:grid-cols-2 gap-3">
              <Row label="Request type"><Choice value={f.str("orders_api_method", "GET")} choices={f.field("orders_api_method")?.choices ?? []} onChange={(v) => f.set("orders_api_method", v)} /></Row>
              <Row label="Send the key as" hint="Ask your PPC team which one they expect.">
                <Choice value={f.str("orders_api_key_in", "header")} choices={f.field("orders_api_key_in")?.choices ?? []} onChange={(v) => f.set("orders_api_key_in", v)}
                  labels={{ header: "A header", query: "Part of the URL", bearer: "Bearer token" }} />
              </Row>
            </div>
            <div className="grid sm:grid-cols-2 gap-3">
              <Row label="API key" hint="Stored encrypted. Never shown again.">
                <Secret field={f.field("orders_api_key")} value={f.vals.orders_api_key as string | null} onChange={(v) => f.set("orders_api_key", v)} />
              </Row>
              {f.str("orders_api_key_in", "header") !== "bearer" && (
                <Row label="Key name" hint="e.g. X-API-Key or apikey"><Text value={f.str("orders_api_key_name", "")} onChange={(v) => f.set("orders_api_key_name", v)} mono /></Row>
              )}
            </div>
            {f.str("orders_api_method", "GET") === "POST" && (
              <Row label="Request body (optional)" hint="JSON sent with the request, if the endpoint needs it.">
                <Text value={f.str("orders_api_body", "")} onChange={(v) => f.set("orders_api_body", v)} mono />
              </Row>
            )}
          </div>
        )}

        {source === "sql" && (
          <div className="space-y-3">
            <Row label="Database connection" hint="Stored encrypted, e.g. mysql+pymysql://user:pass@host/db">
              <Secret field={f.field("orders_sql_url")} value={f.vals.orders_sql_url as string | null} onChange={(v) => f.set("orders_sql_url", v)} placeholder="mysql+pymysql://user:pass@host/db" />
            </Row>
            <Row label="Query" hint="Read-only. It should return one row per order item.">
              <textarea className="input w-full font-mono text-xs" rows={3} value={f.str("orders_sql_query", "")} onChange={(e) => f.set("orders_sql_query", e.target.value)} placeholder="SELECT * FROM bom_ppc_status" />
              {f.errors.orders_sql_query && <div className="text-xs text-rose-600 mt-0.5">{f.errors.orders_sql_query}</div>}
            </Row>
          </div>
        )}

        {source === "file" && (
          <Row label="File path on the server" hint="Any of .json, .csv, .xlsx or .html.">
            <Text value={f.str("orders_file_path", "")} onChange={(v) => f.set("orders_file_path", v)} mono placeholder="D:\\PPC\\orders.xlsx" error={f.errors.orders_file_path} />
          </Row>
        )}

        <div className="grid sm:grid-cols-2 gap-3">
          <Row label="File format" hint="Leave on automatic unless it guesses wrong.">
            <Choice value={f.str("orders_format", "auto")} choices={f.field("orders_format")?.choices ?? []} onChange={(v) => f.set("orders_format", v)}
              labels={{ auto: "Detect automatically", json: "JSON", csv: "CSV", xlsx: "Excel", html: "HTML table" }} />
          </Row>
          {(f.str("orders_format", "auto") === "xlsx" || f.str("orders_format", "auto") === "auto") && (
            <Row label="Excel sheet (optional)" hint="Leave empty for the first sheet."><Text value={f.str("orders_sheet", "")} onChange={(v) => f.set("orders_sheet", v)} /></Row>
          )}
        </div>

        <div className="border-t border-slate-100 pt-3 grid sm:grid-cols-2 gap-3">
          <Row label="Check for new data every" hint="Minutes. Lower means fresher status, more calls to your PPC system.">
            <div className="flex items-center gap-2"><Num value={f.num("order_refresh_minutes", 5)} onChange={(v) => f.set("order_refresh_minutes", v)} error={f.errors.order_refresh_minutes} /><span className="text-sm text-slate-500">minutes</span></div>
          </Row>
          <Row label="Warn me if data is older than" hint="Raises an alert and shows a red badge on the Overview.">
            <div className="flex items-center gap-2"><Num value={f.num("orders_stale_minutes", 30)} onChange={(v) => f.set("orders_stale_minutes", v)} error={f.errors.orders_stale_minutes} /><span className="text-sm text-slate-500">minutes</span></div>
          </Row>
        </div>

        <div className="flex flex-wrap gap-2 items-center border-t border-slate-100 pt-3">
          <button className="btn-ghost" disabled={f.busy} onClick={runTest}>Test connection</button>
          <button className="btn-primary" disabled={f.busy || !f.dirty} onClick={() => f.save()}>Save</button>
          <button className="btn-ghost" disabled={running} onClick={refreshNow}>{running ? "Loading…" : "Load data now"}</button>
          {f.msg && <span className="text-sm text-slate-600">{f.msg}</span>}
        </div>
        <div className="text-xs text-slate-500">{f.data.env_note}</div>
      </div>

      <div className="card space-y-3">
        <div className="font-medium">Test result</div>
        {!test ? (
          <div className="text-sm text-slate-500">Press <b>Test connection</b> to see the columns your system returns, then match them below.</div>
        ) : (
          <>
            <ResultBox ok={test.ok}>
              {test.ok ? `Read ${test.total_raw} rows from the source, ${test.mapped} usable.` : test.error || "Could not read the data."}
              {test.source && <div className="text-xs opacity-80 mt-0.5 break-all">{test.source}</div>}
            </ResultBox>
            {test.warnings?.length > 0 && <div className="text-xs text-amber-700">{test.warnings.join(" · ")}</div>}
            {headers.length > 0 && <div className="text-xs text-slate-500">Columns found: {headers.join(" | ")}</div>}
          </>
        )}

        <div className="border-t border-slate-100 pt-3">
          <div className="font-medium text-sm mb-1">Match the columns</div>
          <p className="text-xs text-slate-500 mb-2">
            Tell the bot which column in your table means what. The three marked * are required; add the others
            when your table has them, and remove one the bot should ignore.
          </p>
          <div className="space-y-2">
            {shownColumns(f.data.column_fields, f.data.required_columns, (k) => !!(map[k] ?? "").trim(), added).map((k) => {
              const required = f.data!.required_columns.includes(k);
              return (
                <div key={k} className="grid grid-cols-[1fr_1fr_auto] gap-2 items-center">
                  <div className="text-sm">{COLUMN_LABELS[k] || k}{required && <span className="text-rose-600"> *</span>}</div>
                  <ColumnPicker value={map[k] || ""} headers={headers} onChange={(v) => setMap(k, v)} required={required} />
                  {required ? <span className="w-8" /> : (
                    <button className="btn-ghost !h-8 !w-8 !px-0 text-rose-600" title={`Remove ${COLUMN_LABELS[k] || k}`}
                      aria-label={`Remove ${COLUMN_LABELS[k] || k}`}
                      onClick={() => { setMap(k, ""); setAdded((a) => a.filter((x) => x !== k)); }}>✕</button>
                  )}
                </div>
              );
            })}
          </div>
          <AddColumn labels={COLUMN_LABELS} onAdd={(k) => setAdded((a) => [...a, k])}
            options={f.data.column_fields.filter((k) => !f.data!.required_columns.includes(k)
              && !(map[k] ?? "").trim() && !added.includes(k))} />
          {f.errors.orders_column_map && <div className="text-xs text-rose-600 mt-1">{f.errors.orders_column_map}</div>}
          <div className="text-xs text-slate-500 mt-2">
            The item description is what a customer reads next to an item code, so a workflow can show
            "FG-2001 — Spice pouch 200g". Connection status stays inside the system — it is never sent to a customer.
          </div>
        </div>

        {test?.sample?.length ? (
          <div className="border-t border-slate-100 pt-3 overflow-auto">
            <div className="font-medium text-sm mb-1">First rows, as the bot reads them</div>
            <table className="w-full">
              <thead><tr>{Object.keys(test.sample[0]).map((k) => <th key={k} className="th">{k}</th>)}</tr></thead>
              <tbody>{test.sample.map((r, i) => <tr key={i}>{Object.values(r).map((v, j) => <td key={j} className="td font-mono text-xs">{v === null ? "—" : String(v)}</td>)}</tr>)}</tbody>
            </table>
          </div>
        ) : null}
      </div>
    </div>
  );
}

// ---------------- customers ----------------
const CRON_DAILY = /^(\d{1,2}) (\d{1,2}) \* \* \*$/;

function CustomersConnection() {
  const f = useConnForm();
  const [test, setTest] = useState<CustomerTest | null>(null);
  const [running, setRunning] = useState(false);
  const [advanced, setAdvanced] = useState(false);

  const source = f.str("customers_source", "local");
  const headers = test?.headers ?? [];
  const [added, setAdded] = useState<string[]>([]);
  const cron = f.str("customer_sync_cron", "10 6 * * *");
  const daily = CRON_DAILY.exec(cron);
  const time = daily ? `${daily[2].padStart(2, "0")}:${daily[1].padStart(2, "0")}` : "";

  const runTest = async () => {
    f.setBusy(true); f.setMsg(null);
    try { setTest(await api.testCustomers(f.draft())); } catch (e) { f.setMsg((e as Error).message); } finally { f.setBusy(false); }
  };
  const importNow = async () => {
    setRunning(true);
    try { const r = await api.importCustomers(); f.setMsg(r.ok ? `Imported ${r.accepted} customers, ${r.rejected} rejected.` : `Failed: ${r.error}`); }
    catch (e) { f.setMsg((e as Error).message); } finally { setRunning(false); }
  };

  if (f.error) return <ErrorBox msg={f.error} />;
  if (!f.data) return <div className="text-slate-500">Loading…</div>;

  return (
    <div className="grid xl:grid-cols-2 gap-4">
      <div className="card space-y-3">
        <div className="font-medium">Where the SAP customer Excel is</div>
        <div className="grid sm:grid-cols-3 gap-2">
          <SourceCard active={source === "local"} title="Folder or network share" note="A path on this server, e.g. D:\SAP\customers.xlsx" onClick={() => f.set("customers_source", "local")} />
          <SourceCard active={source === "dropbox"} title="Dropbox" note="Picked up from your Dropbox account" onClick={() => f.set("customers_source", "dropbox")} />
          <SourceCard active={source === "url"} title="Download link" note="SharePoint, OneDrive or any HTTPS link" onClick={() => f.set("customers_source", "url")} />
        </div>

        {source === "local" && (
          <Row label="File path" hint={<>Full path on the machine running the bot. A network share works too: <span className="font-mono">\\\\server\\sap\\customers.xlsx</span></>}>
            <Text value={f.str("customers_file_path", "")} onChange={(v) => f.set("customers_file_path", v)} mono placeholder="D:\SAP\exports\customers.xlsx" error={f.errors.customers_file_path} />
          </Row>
        )}

        {source === "dropbox" && (
          <div className="space-y-3">
            <div className="text-xs text-slate-500">Create a scoped app at dropbox.com/developers with the <span className="font-mono">files.content.read</span> permission, then paste its details here.</div>
            <Row label="App key"><Text value={f.str("dropbox_app_key", "")} onChange={(v) => f.set("dropbox_app_key", v)} mono error={f.errors.dropbox_app_key} /></Row>
            <div className="grid sm:grid-cols-2 gap-3">
              <Row label="App secret"><Secret field={f.field("dropbox_app_secret")} value={f.vals.dropbox_app_secret as string | null} onChange={(v) => f.set("dropbox_app_secret", v)} /></Row>
              <Row label="Refresh token"><Secret field={f.field("dropbox_refresh_token")} value={f.vals.dropbox_refresh_token as string | null} onChange={(v) => f.set("dropbox_refresh_token", v)} /></Row>
            </div>
            <Row label="File path inside Dropbox"><Text value={f.str("dropbox_file_path", "")} onChange={(v) => f.set("dropbox_file_path", v)} mono placeholder="/SAP/customers.xlsx" /></Row>
          </div>
        )}

        {source === "url" && (
          <div className="space-y-3">
            <Row label="Download link" hint="Must download the file directly. A SharePoint share link usually needs &download=1 at the end.">
              <Text value={f.str("customers_url", "")} onChange={(v) => f.set("customers_url", v)} placeholder="https://company.sharepoint.com/.../customers.xlsx?download=1" error={f.errors.customers_url} />
            </Row>
            <div className="grid sm:grid-cols-3 gap-3">
              <Row label="Key (optional)"><Secret field={f.field("customers_url_key")} value={f.vals.customers_url_key as string | null} onChange={(v) => f.set("customers_url_key", v)} /></Row>
              <Row label="Send it as"><Choice value={f.str("customers_url_key_in", "header")} choices={f.field("customers_url_key_in")?.choices ?? []} onChange={(v) => f.set("customers_url_key_in", v)}
                labels={{ header: "A header", query: "Part of the URL", bearer: "Bearer token" }} /></Row>
              {f.str("customers_url_key_in", "header") !== "bearer" && (
                <Row label="Key name"><Text value={f.str("customers_url_key_name", "")} onChange={(v) => f.set("customers_url_key_name", v)} mono /></Row>
              )}
            </div>
          </div>
        )}

        <Row label="Excel sheet (optional)" hint="Leave empty to use the first sheet."><Text value={f.str("customers_sheet", "")} onChange={(v) => f.set("customers_sheet", v)} /></Row>

        <div className="border-t border-slate-100 pt-3">
          <Row label="Import every day at" hint="Set this a few minutes after SAP writes the export.">
            {!advanced && daily ? (
              <div className="flex items-center gap-2">
                <input type="time" className="input" value={time} onChange={(e) => { const [h, m] = e.target.value.split(":"); f.set("customer_sync_cron", `${Number(m)} ${Number(h)} * * *`); }} />
                <button type="button" className="btn-ghost text-xs" onClick={() => setAdvanced(true)}>Advanced</button>
              </div>
            ) : (
              <div className="flex items-center gap-2">
                <Text value={cron} onChange={(v) => f.set("customer_sync_cron", v)} mono />
                <button type="button" className="btn-ghost text-xs" onClick={() => { f.set("customer_sync_cron", "10 6 * * *"); setAdvanced(false); }}>Simple</button>
              </div>
            )}
          </Row>
          {f.errors.customer_sync_cron && <div className="text-xs text-rose-600">{f.errors.customer_sync_cron}</div>}
        </div>

        <div className="flex flex-wrap gap-2 items-center border-t border-slate-100 pt-3">
          <button className="btn-ghost" disabled={f.busy} onClick={runTest}>Test connection</button>
          <button className="btn-primary" disabled={f.busy || !f.dirty} onClick={() => f.save()}>Save</button>
          <button className="btn-ghost" disabled={running} onClick={importNow}>{running ? "Importing…" : "Import now"}</button>
          {f.msg && <span className="text-sm text-slate-600">{f.msg}</span>}
        </div>
      </div>

      <div className="card space-y-3">
        <div className="font-medium">Test result</div>
        {!test ? (
          <div className="text-sm text-slate-500">Press <b>Test connection</b> to read the file and check the column names — nothing is imported.</div>
        ) : (
          <>
            <ResultBox ok={test.ok}>
              {test.ok ? `${test.accepted} customers would be imported, ${test.rejected} rejected.` : test.error || "Could not read the file."}
              {test.source && <div className="text-xs opacity-80 mt-0.5 break-all">{test.source}</div>}
            </ResultBox>
            {headers.length > 0 && <div className="text-xs text-slate-500">Columns found: {headers.join(" | ")}</div>}
          </>
        )}

        <div className="border-t border-slate-100 pt-3">
          <div className="font-medium text-sm mb-1">Match the columns</div>
          <p className="text-xs text-slate-500 mb-2">
            The number and the name are required — without them there is nobody to answer, and no name to match
            orders on. The customer code is optional: add it if your list has one, remove it if it does not.
          </p>
          <div className="space-y-2">
            {shownColumns(CUSTOMER_COLUMNS, CUSTOMER_REQUIRED, (k) => !!f.str(k, "").trim(), added).map((k) => {
              const required = CUSTOMER_REQUIRED.includes(k);
              return (
                <div key={k} className="grid grid-cols-[1fr_1fr_auto] gap-2 items-center">
                  <div className="text-sm">{CUSTOMER_LABELS[k]}{required && <span className="text-rose-600"> *</span>}</div>
                  <ColumnPicker value={f.str(k, "")} headers={headers} onChange={(v) => f.set(k, v)}
                    required={required} error={f.errors[k]} />
                  {required ? <span className="w-8" /> : (
                    <button className="btn-ghost !h-8 !w-8 !px-0 text-rose-600" title={`Remove ${CUSTOMER_LABELS[k]}`}
                      aria-label={`Remove ${CUSTOMER_LABELS[k]}`}
                      onClick={() => { f.set(k, ""); setAdded((a) => a.filter((x) => x !== k)); }}>✕</button>
                  )}
                </div>
              );
            })}
          </div>
          <AddColumn labels={CUSTOMER_LABELS} onAdd={(k) => setAdded((a) => [...a, k])}
            options={CUSTOMER_COLUMNS.filter((k) => !CUSTOMER_REQUIRED.includes(k) && !f.str(k, "").trim()
              && !added.includes(k))} />
        </div>

        {test?.sample?.length ? (
          <div className="border-t border-slate-100 pt-3 overflow-auto">
            <div className="font-medium text-sm mb-1">First customers, as the bot reads them</div>
            <table className="w-full">
              <thead><tr><th className="th">WhatsApp number</th><th className="th">Code</th><th className="th">Name (exact)</th><th className="th">From the file</th></tr></thead>
              <tbody>{test.sample.map((r, i) => (
                <tr key={i}><td className="td font-mono text-xs">{r.phone}</td><td className="td text-xs">{r.code}</td><td className="td font-mono text-xs">"{r.name}"</td><td className="td font-mono text-xs">{r.raw_contact}</td></tr>
              ))}</tbody>
            </table>
          </div>
        ) : null}

        {test?.rejected_rows?.length ? (
          <div className="border-t border-slate-100 pt-3 overflow-auto">
            <div className="font-medium text-sm mb-1 text-amber-700">Rows that would be skipped</div>
            <table className="w-full">
              <thead><tr>{Object.keys(test.rejected_rows[0]).map((k) => <th key={k} className="th">{k}</th>)}</tr></thead>
              <tbody>{test.rejected_rows.map((r, i) => <tr key={i}>{Object.values(r).map((v, j) => <td key={j} className="td text-xs">{String(v ?? "")}</td>)}</tr>)}</tbody>
            </table>
          </div>
        ) : null}
      </div>
    </div>
  );
}

// ---------------- history ----------------
function History() {
  const [kind, setKind] = useState("");
  const { data, error, reload } = usePoll(() => api.imports(kind || undefined), 10000, [kind]);
  const [open, setOpen] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const file = useRef<HTMLInputElement>(null);

  const upload = async () => {
    const fl = file.current?.files?.[0];
    if (!fl) return;
    setBusy(true);
    try { const r = await api.importCustomers(fl); setOpen(r.id); reload(); }
    finally { setBusy(false); if (file.current) file.current.value = ""; }
  };

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <select className="input" value={kind} onChange={(e) => setKind(e.target.value)}>
          <option value="">All</option><option value="customers">Customers</option><option value="orders">Orders</option>
        </select>
        <div className="ml-auto flex items-center gap-2">
          <input ref={file} type="file" accept=".xlsx,.xls" className="text-sm" />
          <button className="btn-primary" disabled={busy} onClick={upload}>{busy ? "Importing…" : "Upload an Excel once"}</button>
        </div>
      </div>
      <ErrorBox msg={error} />
      <div className="card p-0 overflow-auto">
        {!data || data.length === 0 ? (
          <Empty>No imports yet.</Empty>
        ) : (
          <table className="w-full">
            <thead><tr><th className="th">Finished</th><th className="th">Kind</th><th className="th">Result</th><th className="th">Source</th><th className="th">Rows</th><th className="th">Accepted</th><th className="th">Rejected</th><th className="th"></th></tr></thead>
            <tbody>
              {data.map((r) => (
                <>
                  <tr key={r.id} className="hover:bg-slate-50">
                    <td className="td text-xs whitespace-nowrap">{fmt(r.finished_at)}</td>
                    <td className="td capitalize">{r.kind}</td>
                    <td className="td"><Badge tone={r.ok ? "green" : "red"}>{r.ok ? "ok" : "failed"}</Badge></td>
                    <td className="td text-xs break-all max-w-xs">{r.source}</td>
                    <td className="td">{r.total_rows}</td>
                    <td className="td">{r.accepted}</td>
                    <td className="td">{r.rejected ? <span className="text-amber-600 font-medium">{r.rejected}</span> : 0}</td>
                    <td className="td"><button className="btn-ghost" onClick={() => setOpen(open === r.id ? null : r.id)}>{open === r.id ? "Hide" : "Details"}</button></td>
                  </tr>
                  {open === r.id && (
                    <tr key={`${r.id}-d`}>
                      <td className="td bg-slate-50" colSpan={8}>
                        {r.error && <div className="text-rose-700 text-sm mb-2">Error: {r.error}</div>}
                        {r.warnings.length > 0 && <div className="text-amber-700 text-sm mb-2">Warnings: {r.warnings.join(" · ")}</div>}
                        <div className="text-xs text-slate-500 mb-2">Columns found: {r.raw_headers.join(" | ") || "—"}</div>
                        {r.rejected_rows.length > 0 ? (
                          <table className="w-full bg-white">
                            <thead><tr>{Object.keys(r.rejected_rows[0]).map((k) => <th key={k} className="th">{k}</th>)}</tr></thead>
                            <tbody>{r.rejected_rows.map((row, i) => <tr key={i}>{Object.values(row).map((v, j) => <td key={j} className="td text-xs">{String(v ?? "")}</td>)}</tr>)}</tbody>
                          </table>
                        ) : <div className="text-sm text-slate-500">No rejected rows.</div>}
                      </td>
                    </tr>
                  )}
                </>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
