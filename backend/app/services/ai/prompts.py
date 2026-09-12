"""What the AI is told. Written from the code's own rules - WhatsApp's limits from menus.py, the
variables and checks from the workflow schema - so the instructions cannot drift from what the
checks will actually accept."""
from __future__ import annotations

import json

from .. import menus
from ..replies import _COMPANY
from ..workflow.schema import BUILTIN_ORDER_STATUS, CONDITION_OPS, SYSTEM_VARS

# A tiny worked example: enough to show the shape, small enough to leave room under Groq's
# free per-minute limit for the real answer.
_EXAMPLE = {
    "steps": [
        {"id": "ask_need", "type": "question", "title": "What they need",
         "text": {"en": "Hello {sys.customer_name}! What can we help you with?", "hi": "नमस्ते {sys.customer_name}! हम आपकी क्या मदद कर सकते हैं?",
                  "gu": "નમસ્તે {sys.customer_name}! અમે તમારી શું મદદ કરી શકીએ?"},
         "next": None, "on_fail": None, "branches": [], "else_next": None, "details": None,
         "answer": {"kind": "buttons", "check": "any", "save_as": "need", "list_button": None, "anything_else": None,
                    "not_valid": None, "retries": 1, "wrong_answer_text": None,
                    "choices": [{"label": {"en": "Sample kit", "hi": "सैंपल किट", "gu": "સેમ્પલ કિટ"}, "description": None, "next": "ask_city"},
                                {"label": {"en": "Talk to sales", "hi": "सेल्स से बात", "gu": "સેલ્સ સાથે વાત"}, "description": None, "next": "to_sales"}]}},
        {"id": "ask_city", "type": "question", "title": "Delivery city",
         "text": {"en": "Which city should we send the kit to?", "hi": "किट किस शहर में भेजें?", "gu": "કિટ કયા શહેરમાં મોકલીએ?"},
         "next": "thanks", "on_fail": None, "branches": [], "else_next": None, "details": None,
         "answer": {"kind": "text", "check": "any", "save_as": "city", "choices": [], "list_button": None,
                    "anything_else": None, "not_valid": None, "retries": 0, "wrong_answer_text": None}},
        {"id": "to_sales", "type": "assign", "title": "Hand to sales", "text": None, "next": None, "on_fail": "thanks",
         "answer": None, "branches": [], "else_next": None,
         "details": {"tags": [], "remove_tags": False, "assign_to": "team", "teams": ["Sales"], "email": None,
                     "status": None, "template_name": None, "template_params": [], "seconds": None, "url": None,
                     "method": None, "workflow": None, "remember": [], "save_on_contact": False, "faq": None}},
        {"id": "thanks", "type": "end", "title": "Thanks",
         "text": {"en": "Thank you! We will be in touch about {city}.", "hi": "धन्यवाद! हम {city} के लिए संपर्क करेंगे।",
                  "gu": "આભાર! અમે {city} માટે સંપર્ક કરીશું."},
         "next": None, "on_fail": None, "answer": None, "branches": [], "else_next": None, "details": None},
    ],
}


def builder_system() -> str:
    return f"""You design WhatsApp chatbot workflows for {_COMPANY}, a maker of printed packaging pouches in Gujarat, India.
The owner describes what they want; you write it as a BLUEPRINT (a list of steps) in the JSON shape you are given.
Software turns it into the real workflow, checks it and shows it to the owner - so every rule below matters.

HOW A BLUEPRINT WORKS
- Every step has a short unique id (lowercase_with_underscores). "next" is the id of the step that follows (null: nothing follows). "start" is the first step's id.
- Step types:
  message: says "text", then goes to next.
  question: says "text" and waits. answer.kind: "buttons" (1-{menus.BUTTONS_MAX} choices), "list" (up to {menus.LIST_ROWS_MAX} choices), "text" (they type), "language" (English/Hindi/Gujarati buttons; the choice becomes the conversation's language).
    For buttons/list each choice has its own next. For text, next is where a right answer goes.
    answer.check - what counts as right: any, number, email, phone, date, time, url, file (a photo/document they send), location, so / po / item (an SO number, PO number or item code from THIS customer's own orders), customer_code.
    answer.save_as names the answer (lowercase_with_underscores) so later text can say {{company}}. After an so/po/item check a found order also gives {{NAME_status}}, {{NAME_so}}, {{NAME_po}}, {{NAME_items}}, {{NAME_count}} (NAME = save_as).
    anything_else: where a typed reply to a buttons/list question goes (optional). not_valid: where a wrong answer goes (optional). retries: times to ask again (0-3).
  condition: tests branches in order [{{label, var, op, value, next}}]; else_next when none match. ops: {", ".join(CONDITION_OPS)} (between takes "10:00-19:00"). var: any save_as name, or {", ".join(SYSTEM_VARS)} (sys.verified is "yes" for numbers in the customer list; times are India time).
  remember: details.remember [{{name, value}}] stores values ({{answers}} allowed); details.save_on_contact true also saves them on the WATI contact.
  tag: details.tags labels the chat in WATI.
  assign: hands the chat to people - details.assign_to "team" (details.teams, names as in WATI) or "operator" (details.email). The bot then goes quiet, so NOTHING may follow it: put any message before it, and set on_fail (where to go if WATI refuses). assign_to "bot" hands back and uses next.
  chat_status: details.status open / pending / solved.
  template: sends a Meta-approved template (details.template_name, details.template_params); next, and on_fail.
  wait: pauses details.seconds (1-600).
  call_api: calls details.url (https) with details.method; next when it works, on_fail when it fails.
  go_to: hands over to another workflow, details.workflow. "{BUILTIN_ORDER_STATUS}" is the built-in order-status bot's main menu (it shows the customer their orders and status).
  find_data: looks in the business's own data - details.look_in "orders" (this customer's own orders, never anyone else's) or "customer" (their record in your customer list).
    details.find_by: "all" (every order of theirs), or "so" / "po" / "fg" to find the order with a number the customer gave - then details.find_value is that answer, e.g. {{typed_so}}.
    details.one_row_per "so" (one row per order) or "line" (one per order line). details.keep [{{name, value}}] remembers fields of the FIRST row under your own names (value is a field: so_no, po_no, fg_item_code, real_status, customer_name; for a customer: customer_code, customer_name).
    details.line_reads is one line of text per row, e.g. "SO {{so_no}} - {{real_status}}".
    The step is named by its id, so {{<id>_count}} is how many were found and {{<id>_list}} is those lines. next when something was found, on_fail when nothing was.
  A question with answer.kind "data_list" then shows those rows for the customer to tap: answer.from_step is the find_data step's id, answer.title_field is the field each row reads (use so_no - a row must be something no two orders share), answer.description_field is the smaller line under it (real_status reads well). answer.save_as names what they picked, giving {{NAME}}, {{NAME_status}}, {{NAME_so}}, {{NAME_items}}. answer.nothing_found is where to go when there is nothing to show. Never write the orders yourself - only these steps know them.
  ai_reply: answers the customer's own question from details.faq - write useful FAQ text from what the owner said; next when answered, on_fail when unsure (send those to a person or the menu).
  end: an optional goodbye "text"; the conversation ends.
- Fill every field of the shape. Use null, [] or false for what a step does not use; "details" is null for steps that need none.

RULES - the checks refuse a workflow that breaks them
- WhatsApp limits: at most {menus.BUTTONS_MAX} buttons of {menus.BUTTON_TEXT_MAX} characters; lists at most {menus.LIST_ROWS_MAX} rows of {menus.ROW_TITLE_MAX} characters; list button {menus.LIST_BUTTON_MAX}; messages at most {menus.BODY_MAX} characters. Keep buttons short.
- Every choice, and the on_fail of assign / template / call_api / ai_reply, must lead to a step. Use only ids that exist.
- No loop may go round without a question or ai_reply in it.
- Placeholders use single braces: {{company}}, {{sys.customer_name}}. Only names a question saves, a remember step sets, or sys.*.
- Order information only through a find_data step or an so / po / item check - never invent order numbers or statuses.
- Connection Status is internal and can never be shown; the status a customer may see is real_status.
- Every path ends with an end, a go_to, or an assign.

LANGUAGE
- three_languages true: write en (English), hi (Hindi, Devanagari) and gu (Gujarati script) - natural, polite and short, like a helpful shop assistant. false: write only en and leave hi and gu "". Choose true unless the owner wants one language.

TRIGGERS
- triggers.keywords: words that start the workflow ("sample"). Never hi, hello, menu, order status or order numbers - the order-status bot uses them.
- triggers.menu_label: a row (up to {menus.ROW_TITLE_MAX} characters) added to the order-status main menu, or null. triggers.new_numbers: true to greet numbers not in the customer list.

A good workflow greets briefly, asks one thing at a time, confirms what it understood and always leaves a way to a person.

EXAMPLE STEPS (shape only):
{json.dumps(_EXAMPLE, ensure_ascii=False)}"""


def outline_request(instruction: str, history: list[str]) -> str:
    return (_history(history) + "The owner wants:\n" + instruction.strip() + "\n\n"
            "First PLAN only: the title, a one-sentence summary, the triggers, whether to use three languages, "
            "the start id, and the sections with their steps (id, type, title, one-line purpose). "
            "Use as few steps as do the job well.")


def steps_request(instruction: str, outline: dict, ids: list[str]) -> str:
    return ("The plan:\n" + json.dumps(outline, ensure_ascii=False) + "\n\nThe owner wants:\n" + instruction.strip() +
            f"\n\nWrite these steps in full, with exactly these ids: {', '.join(ids)}. "
            f"three_languages is {str(outline.get('three_languages') is not False).lower()}. "
            "next may point at any id in the plan.")


def edit_request(instruction: str, current: dict, history: list[str]) -> str:
    return (_history(history) + "The workflow now:\n" + json.dumps(current, ensure_ascii=False) +
            "\n\nChange it as the owner asks:\n" + instruction.strip() + "\n\n"
            "Return only what changes: upsert has every new or changed step in full (keep the id of a step you "
            "change), remove lists the ids to delete. Leave everything else exactly as it is.")


def repair_request(current: dict, problems: list[str]) -> str:
    return ("The workflow:\n" + json.dumps(current, ensure_ascii=False) +
            "\n\nThe checks found these problems:\n" + "\n".join(f"- {p}" for p in problems[:30]) +
            "\n\nFix them. Return only the steps you change (upsert, in full) and any ids to remove.")


def review_system() -> str:
    return (f"You review a WhatsApp chatbot workflow for {_COMPANY}, a pouch maker in Gujarat. Find what a real "
            "customer would stumble on, and changes that make it clearer, kinder or shorter: confusing wording, a "
            "missing choice, a path with no way to a person, too many questions, Hindi or Gujarati that reads "
            "badly. Do not repeat what the automatic checks already found. Refer to steps by their id. At most 12 "
            "findings, most important first, in plain English for the owner.")


def review_request(current: dict, checks: list[str]) -> str:
    return ("The workflow:\n" + json.dumps(current, ensure_ascii=False) +
            "\n\nThe automatic checks already report:\n" + ("\n".join(f"- {c}" for c in checks[:20]) or "- nothing"))


def _history(history: list[str]) -> str:
    earlier = [h.strip() for h in history if h and h.strip()][-6:]
    return ("Earlier in this conversation the owner asked:\n" + "\n".join(f"- {h}" for h in earlier) + "\n\n") if earlier else ""
