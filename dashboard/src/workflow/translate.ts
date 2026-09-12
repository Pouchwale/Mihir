import { createContext, useContext } from "react";

/** "empty": fill only the Hindi and Gujarati nobody has written (or that auto-translate wrote last);
 *  "overwrite": replace them from the English. */
export type FillMode = "empty" | "overwrite";

/** What the editor offers every text field for translation. Provided by WorkflowEditor, so a field
 *  can translate itself without knowing how the document is stored. */
export interface TranslateApi {
  /** three languages per step, and not the read-only published view */
  enabled: boolean;
  /** fill Hindi and Gujarati as soon as an English text is finished */
  auto: boolean;
  setAuto: (on: boolean) => void;
  fill: (nodeId: string, path: string, mode: FillMode) => Promise<void>;
  fillStep: (nodeId: string) => Promise<void>;
  /** `${nodeId}|${path}` being translated, `${nodeId}|*` for a step, "*" for the whole workflow */
  busy: string | null;
}

const OFF: TranslateApi = {
  enabled: false, auto: false, setAuto: () => undefined,
  fill: async () => undefined, fillStep: async () => undefined, busy: null,
};

export const TranslateContext = createContext<TranslateApi>(OFF);
/** The step the inspector is showing, so a field knows where its text lives. */
export const NodeContext = createContext<string>("");

export const useTranslate = () => useContext(TranslateContext);
export const useNodeId = () => useContext(NodeContext);
