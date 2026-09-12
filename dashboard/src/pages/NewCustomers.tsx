import { Link } from "react-router-dom";
import { Download, Trash2 } from "lucide-react";
import { api } from "../api";
import { ago, Empty, ErrorBox, fmt, usePoll } from "../ui";
import { downloadCsv } from "../workflow/files";

/** Numbers that signed up through a workflow. Kept apart from the customer Excel on purpose: that
 *  list decides who may see orders, and nothing typed into a chat may ever grant that. */
export default function NewCustomers() {
  const { data, error, reload } = usePoll(() => api.newCustomers(), 10000);
  const rows = data ?? [];
  // every answer any workflow collected, as columns
  const fields = Array.from(new Set(rows.flatMap((r) => Object.keys(r.details))));

  const exportCsv = () => downloadCsv(`new-customers-${new Date().toISOString().slice(0, 10)}.csv`, [
    ["Phone", "Name", "WhatsApp name", ...fields, "Workflow", "Signed up", "In customer list"],
    ...rows.map((r) => [r.phone, r.name, r.whatsapp_name, ...fields.map((f) => r.details[f] ?? ""), r.workflow,
      r.created_at ?? "", r.in_customer_list ? "yes" : "no"]),
  ]);

  const remove = async (phone: string) => {
    if (!confirm(`Remove ${phone} from New customers? If they write again, the new-numbers workflow greets them again.`)) return;
    await api.deleteNewCustomer(phone);
    await reload();
  };

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-start gap-3">
        <div>
          <h1 className="text-xl font-semibold">New customers</h1>
          <p className="text-sm text-slate-500 max-w-3xl">
            Numbers that finished the workflow for new numbers, with what they told it. They are not in your customer
            Excel yet, so they cannot see any orders; the bot welcomes them back instead of asking them to sign up again.
            Add them to SAP and the next customer sync makes them customers like any other.
          </p>
        </div>
        <button className="btn-ghost ml-auto" disabled={!rows.length} onClick={exportCsv}>
          <Download className="w-4 h-4" /> Download CSV
        </button>
      </div>
      <ErrorBox msg={error} />
      <div className="card p-0 overflow-auto">
        {rows.length === 0 ? (
          <Empty>
            Nobody yet. Switch on <b>Numbers that are not in your customer list</b> under <b>Start</b> in a workflow, and
            everyone who finishes it appears here.
          </Empty>
        ) : (
          <table className="w-full">
            <thead>
              <tr>
                <th className="th">Phone</th>
                <th className="th">Name</th>
                <th className="th">What they told us</th>
                <th className="th">Workflow</th>
                <th className="th">Signed up</th>
                <th className="th">Status</th>
                <th className="th"></th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.phone}>
                  <td className="td font-mono">{r.phone}</td>
                  <td className="td">
                    <div className="font-medium">{r.name || "—"}</div>
                    {r.whatsapp_name && r.whatsapp_name !== r.name && <div className="text-xs text-slate-500">WhatsApp: {r.whatsapp_name}</div>}
                  </td>
                  <td className="td text-xs">
                    {Object.entries(r.details).map(([k, v]) => (
                      <div key={k}><span className="font-mono text-slate-400">{k}</span> {v}</div>
                    ))}
                  </td>
                  <td className="td text-xs">
                    {r.workflow ? <Link className="text-brand-700 hover:underline" to={`/workflows/${r.workflow}`}>{r.workflow}</Link> : "—"}
                  </td>
                  <td className="td text-xs text-slate-500" title={fmt(r.created_at)}>{ago(r.created_at)}</td>
                  <td className="td">
                    {r.in_customer_list
                      ? <span className="badge bg-emerald-100 text-emerald-700">now a customer</span>
                      : <span className="badge bg-amber-100 text-amber-800">not in SAP yet</span>}
                  </td>
                  <td className="td text-right">
                    <button className="btn-ghost !h-8 !w-8 !px-0 text-rose-600" aria-label={`Remove ${r.phone}`}
                      onClick={() => remove(r.phone)}><Trash2 className="w-4 h-4" /></button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
