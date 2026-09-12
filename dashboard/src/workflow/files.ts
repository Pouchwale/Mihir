/** Save a file from the browser - how an export reaches the owner's computer. */
export function downloadText(filename: string, text: string, type: string): void {
  const blob = new Blob([text], { type });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export function downloadJson(filename: string, data: unknown): void {
  downloadText(filename, JSON.stringify(data, null, 2), "application/json");
}

/** A spreadsheet-friendly file. The byte-order mark makes Excel read Hindi and Gujarati correctly. */
export function downloadCsv(filename: string, rows: (string | number | boolean | null | undefined)[][]): void {
  const cell = (v: string | number | boolean | null | undefined) => {
    const s = v === null || v === undefined ? "" : String(v);
    return /[",\r\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  downloadText(filename, "﻿" + rows.map((r) => r.map(cell).join(",")).join("\r\n"), "text/csv;charset=utf-8");
}

/** Read a .json file the owner picked, with a message a person can act on instead of a parser error. */
export async function readJsonFile(file: File): Promise<unknown> {
  if (file.size > 10 * 1024 * 1024) throw new Error("That file is over 10 MB — too big to be a workflow.");
  const text = await file.text();
  try {
    return JSON.parse(text);
  } catch {
    throw new Error(`“${file.name}” is not valid JSON, so it cannot be a workflow export.`);
  }
}
