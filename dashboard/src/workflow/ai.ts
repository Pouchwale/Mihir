import { api } from "../api";
import type { AiJob } from "./types";

/** Follow an AI job until it finishes. A big workflow is several Groq calls, sometimes paused for the
 *  free plan's per-minute limit, so the job reports each stage and any wait as it goes. */
export async function waitForJob(id: string, onUpdate: (job: AiJob) => void,
  alive: () => boolean = () => true): Promise<AiJob | null> {
  for (;;) {
    const job = await api.aiJob(id);
    if (!alive()) return null;
    onUpdate(job);
    if (job.status !== "running") return job;
    await new Promise((r) => window.setTimeout(r, 1000));
  }
}

/** "Writing the steps (2 of 3)…", or "Waiting for Groq's per-minute limit — 7 s". */
export function stageText(job: AiJob | null): string {
  if (!job || job.status !== "running") return "";
  return job.wait_seconds > 0 ? `${job.stage} — ${job.wait_seconds} s` : `${job.stage}…`;
}

/** Starting points for the assistant - the kind of workflow a pouch maker actually needs. */
export const AI_EXAMPLES: { label: string; text: string }[] = [
  {
    label: "Sample kit request",
    text: "A sample kit request. Ask their name, company and city, then which pouch they want (stand-up, zipper, flat "
      + "or spout) and roughly how many pieces. Confirm what they said, tag the chat sample-request and hand it to the "
      + "Sales team.",
  },
  {
    label: "FAQ with a person as backup",
    text: "Answer customers' questions about our pouches from this FAQ: we make stand-up, zipper, flat and spout "
      + "pouches; minimum order is 500 pieces per design; delivery is 10-12 working days after artwork approval; prices "
      + "depend on size and quantity and we send a quote within a day. Let them ask as many questions as they like, and "
      + "hand anything the FAQ does not cover to the Sales team.",
  },
  {
    label: "New customer sign-up",
    text: "For numbers that are not in our customer list: greet them, ask their language, then their name, company, "
      + "city and GST number, save these on the contact, and tell them our team will call within a day.",
  },
  {
    label: "Working hours",
    text: "Between 10:00 and 19:00 India time hand the chat to the Support team; outside those hours say we are "
      + "closed, take their question, and promise a reply the next morning.",
  },
];
