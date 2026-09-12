"""Customer-facing text. Section 8 of the spec.

The reply builder receives ONLY {template, real_status, so_no, po_no, fg_code, n_items, value,
customer_name, language, support}. `connection_status` must never reach this module.
"""
from __future__ import annotations

from dataclasses import dataclass

LANGS = ("en", "hi", "gu")

_COMPANY = "Gujarat Printpack Publication Private Limited"

_T: dict[str, dict[str, str]] = {
    # ---- first contact in a new window (language not known yet: one text for everyone) ----
    "welcome_first": {
        "en": f"Hello, thank you for contacting {_COMPANY}. We are happy to help you with your order status.",
        "hi": f"Hello, thank you for contacting {_COMPANY}. We are happy to help you with your order status.",
        "gu": f"Hello, thank you for contacting {_COMPANY}. We are happy to help you with your order status.",
    },
    "ask_language": {
        "en": "Please choose your language.\nकृपया अपनी भाषा चुनें।\nકૃપા કરીને તમારી ભાષા પસંદ કરો.",
        "hi": "Please choose your language.\nकृपया अपनी भाषा चुनें।\nકૃપા કરીને તમારી ભાષા પસંદ કરો.",
        "gu": "Please choose your language.\nकृपया अपनी भाषा चुनें।\nકૃપા કરીને તમારી ભાષા પસંદ કરો.",
    },
    # ---- main menu ----
    "main_menu": {
        "en": "Hello {customer_name}, how can we help you today?",
        "hi": "नमस्ते {customer_name}, आज हम आपकी कैसे मदद कर सकते हैं?",
        "gu": "નમસ્તે {customer_name}, આજે અમે તમારી કેવી રીતે મદદ કરી શકીએ?",
    },
    "contact_us": {
        "en": "You can reach our team at {support}. We will be happy to help.",
        "hi": "आप हमारी टीम से {support} पर संपर्क कर सकते हैं। हम आपकी सहायता करेंगे।",
        "gu": "તમે અમારી ટીમનો {support} પર સંપર્ક કરી શકો છો. અમે તમારી મદદ કરીશું.",
    },
    # ---- order status ----
    "ask_so_list": {
        "en": "Please select your SO number below, or type it.",
        "hi": "कृपया नीचे से अपना SO नंबर चुनें, या लिखें।",
        "gu": "કૃપા કરીને નીચેથી તમારો SO નંબર પસંદ કરો, અથવા લખો.",
    },
    "ask_so": {
        "en": "Please send your SO number.",
        "hi": "कृपया अपना SO नंबर भेजें।",
        "gu": "કૃપા કરીને તમારો SO નંબર મોકલો.",
    },
    "so_none": {
        "en": "Hello {customer_name}, we could not find any orders for your account right now. Please send your SO number to check, or contact our team at {support}.",
        "hi": "नमस्ते {customer_name}, अभी आपके खाते में कोई ऑर्डर नहीं मिला। जांचने के लिए कृपया अपना SO नंबर भेजें, या हमारी टीम से {support} पर संपर्क करें।",
        "gu": "નમસ્તે {customer_name}, હાલ તમારા ખાતામાં કોઈ ઓર્ડર મળ્યો નથી. તપાસવા માટે કૃપા કરીને તમારો SO નંબર મોકલો, અથવા અમારી ટીમનો {support} પર સંપર્ક કરો.",
    },
    "ask_fg_list": {
        "en": "SO {so_no} has {n_items} items. Please select the item below, or type the FG item code.",
        "hi": "SO {so_no} में {n_items} आइटम हैं। कृपया नीचे से आइटम चुनें, या FG आइटम कोड लिखें।",
        "gu": "SO {so_no} માં {n_items} આઇટમ છે. કૃપા કરીને નીચેથી આઇટમ પસંદ કરો, અથવા FG આઇટમ કોડ લખો.",
    },
    "ask_fg": {
        "en": "SO {so_no} has {n_items} items. Please send the FG item code.",
        "hi": "SO {so_no} में {n_items} आइटम हैं। कृपया FG आइटम कोड भेजें।",
        "gu": "SO {so_no} માં {n_items} આઇટમ છે. કૃપા કરીને FG આઇટમ કોડ મોકલો.",
    },
    "ask_fg_retry": {
        "en": "Item code {fg_code} is not in SO {so_no}. Please select the correct item, or type the FG item code.",
        "hi": "आइटम कोड {fg_code} SO {so_no} में नहीं है। कृपया सही आइटम चुनें, या FG आइटम कोड लिखें।",
        "gu": "આઇટમ કોડ {fg_code} SO {so_no} માં નથી. કૃપા કરીને સાચો આઇટમ પસંદ કરો, અથવા FG આઇટમ કોડ લખો.",
    },
    "confirm_so": {
        "en": "Did you mean SO number {value}? Reply Yes or send the correct number.",
        "hi": "क्या आपका मतलब SO नंबर {value} है? Yes लिखें या सही नंबर भेजें।",
        "gu": "શું તમારો મતલબ SO નંબર {value} છે? Yes લખો અથવા સાચો નંબર મોકલો.",
    },
    "confirm_po": {
        "en": "Did you mean PO number {value}? Reply Yes or send the correct number.",
        "hi": "क्या आपका मतलब PO नंबर {value} है? Yes लिखें या सही नंबर भेजें।",
        "gu": "શું તમારો મતલબ PO નંબર {value} છે? Yes લખો અથવા સાચો નંબર મોકલો.",
    },
    "confirm_fg": {
        "en": "Did you mean FG item code {value}? Reply Yes or send the correct code.",
        "hi": "क्या आपका मतलब FG आइटम कोड {value} है? Yes लिखें या सही कोड भेजें।",
        "gu": "શું તમારો મતલબ FG આઇટમ કોડ {value} છે? Yes લખો અથવા સાચો કોડ મોકલો.",
    },
    "result": {
        "en": f"Hello {{customer_name}},\n\nOrder: SO {{so_no}}{{item}}\nReal Status: {{real_status}}\n\nThank you for contacting {_COMPANY}.",
        "hi": f"नमस्ते {{customer_name}},\n\nऑर्डर: SO {{so_no}}{{item}}\nReal Status: {{real_status}}\n\n{_COMPANY} से संपर्क करने के लिए धन्यवाद।",
        "gu": f"નમસ્તે {{customer_name}},\n\nઓર્ડર: SO {{so_no}}{{item}}\nReal Status: {{real_status}}\n\n{_COMPANY} નો સંપર્ક કરવા બદલ આભાર.",
    },
    "not_found": {
        "en": "Sorry {customer_name}, we could not find that SO number / item code under your account. Please check and try again, or contact our team at {support}.",
        "hi": "क्षमा करें {customer_name}, यह SO नंबर / आइटम कोड आपके खाते में नहीं मिला। कृपया जांच कर दोबारा प्रयास करें, या हमारी टीम से {support} पर संपर्क करें।",
        "gu": "માફ કરશો {customer_name}, આ SO નંબર / આઇટમ કોડ તમારા ખાતામાં મળ્યો નથી. કૃપા કરીને તપાસીને ફરી પ્રયાસ કરો, અથવા અમારી ટીમનો {support} પર સંપર્ક કરો.",
    },
    "bye": {
        "en": f"Thank you {{customer_name}}! Message us anytime to check your order status.\n{_COMPANY}",
        "hi": f"धन्यवाद {{customer_name}}! अपने ऑर्डर का स्टेटस जानने के लिए कभी भी संदेश भेजें।\n{_COMPANY}",
        "gu": f"આભાર {{customer_name}}! તમારા ઓર્ડરનું સ્ટેટસ જાણવા માટે ગમે ત્યારે સંદેશ મોકલો.\n{_COMPANY}",
    },
    "voice_off": {
        "en": "Sorry {customer_name}, we cannot listen to voice messages. Please type your SO number instead, or choose an option below.",
        "hi": "क्षमा करें {customer_name}, हम वॉइस मैसेज नहीं सुन सकते। कृपया अपना SO नंबर लिखकर भेजें, या नीचे से विकल्प चुनें।",
        "gu": "માફ કરશો {customer_name}, અમે વોઇસ મેસેજ સાંભળી શકતા નથી. કૃપા કરીને તમારો SO નંબર લખીને મોકલો, અથવા નીચેથી વિકલ્પ પસંદ કરો.",
    },
    # ---- system messages (outside the conversation) ----
    "verify_failed": {
        "en": "Sorry, we could not verify your details for this number. Please contact our team at {support} and we'll be happy to help.",
        "hi": "क्षमा करें, इस नंबर से आपकी जानकारी सत्यापित नहीं हो सकी। कृपया हमारी टीम से {support} पर संपर्क करें, हम आपकी सहायता करेंगे।",
        "gu": "માફ કરશો, આ નંબર પરથી તમારી વિગતો ચકાસી શકાઈ નથી. કૃપા કરીને અમારી ટીમનો {support} પર સંપર્ક કરો, અમે તમારી મદદ કરીશું.",
    },
    "new_customer_pending": {
        "en": "Welcome back! We have your details and our team will be in touch soon. Once your account is set up you can check your order status here. For anything urgent, contact {support}.",
        "hi": "फिर से स्वागत है! हमारे पास आपकी जानकारी है और हमारी टीम जल्द ही आपसे संपर्क करेगी। आपका खाता बनने के बाद आप यहीं अपने ऑर्डर का स्टेटस देख सकेंगे। किसी भी ज़रूरी काम के लिए {support} पर संपर्क करें।",
        "gu": "ફરી સ્વાગત છે! અમારી પાસે તમારી વિગતો છે અને અમારી ટીમ ટૂંક સમયમાં તમારો સંપર્ક કરશે. તમારું ખાતું બન્યા પછી તમે અહીં જ તમારા ઓર્ડરનું સ્ટેટસ જોઈ શકશો. કોઈ પણ તાત્કાલિક કામ માટે {support} પર સંપર્ક કરો.",
    },
    "too_many_repeats": {
        "en": "You have asked about this a few times and the status is still the same. Our team will "
              "look into it and come back to you. For anything urgent, contact {support}.",
        "hi": "आपने यह कई बार पूछा है और स्टेटस अभी वही है। हमारी टीम इसे देखकर आपसे संपर्क करेगी। किसी भी ज़रूरी काम के लिए "
              "{support} पर संपर्क करें।",
        "gu": "તમે આ ઘણી વાર પૂછ્યું છે અને સ્ટેટસ હજી એ જ છે. અમારી ટીમ તે જોઈને તમારો સંપર્ક કરશે. કોઈ પણ તાત્કાલિક કામ માટે "
              "{support} પર સંપર્ક કરો.",
    },
    "service_down": {
        "en": "Sorry, our system is temporarily unavailable. Please try again in a few minutes.",
        "hi": "क्षमा करें, हमारा सिस्टम अस्थायी रूप से उपलब्ध नहीं है। कृपया कुछ मिनट बाद पुनः प्रयास करें।",
        "gu": "માફ કરશો, અમારી સિસ્ટમ હાલ પૂરતી ઉપલબ્ધ નથી. કૃપા કરીને થોડી મિનિટ પછી ફરી પ્રયાસ કરો.",
    },
    "rate_limited": {
        "en": "You have sent too many messages. Please wait a few minutes and try again.",
        "hi": "आपने बहुत अधिक संदेश भेजे हैं। कृपया कुछ मिनट प्रतीक्षा करें और पुनः प्रयास करें।",
        "gu": "તમે ઘણા બધા સંદેશા મોકલ્યા છે. કૃપા કરીને થોડી મિનિટ રાહ જુઓ અને ફરી પ્રયાસ કરો.",
    },
}

# These are always sent in all three languages, stacked (spec section 8).
TRILINGUAL = {"verify_failed", "service_down"}
# Sent before the customer has chosen a language: one text for everyone (the "en" slot is used).
NEUTRAL = {"welcome_first", "ask_language"}

TEMPLATES = tuple(_T.keys())


@dataclass(frozen=True)
class ReplyContext:
    real_status: str | None = None
    so_no: str | None = None
    po_no: str | None = None
    fg_code: str | None = None
    n_items: int | None = None
    value: str | None = None
    customer_name: str | None = None
    support: str = "[phone/email]"


DEFAULTS = _T  # code defaults; admin overrides come from services.templates.registry


def render_text(text: str, lang: str, ctx: ReplyContext) -> str:
    item = ""
    if ctx.fg_code:
        item = {"en": f", item {ctx.fg_code}", "hi": f", आइटम {ctx.fg_code}", "gu": f", આઇટમ {ctx.fg_code}"}[lang]
    return text.format(
        real_status=ctx.real_status or "",
        so_no=ctx.so_no or "",
        po_no=ctx.po_no or "",
        fg_code=ctx.fg_code or "",
        n_items=ctx.n_items if ctx.n_items is not None else "",
        value=ctx.value or "",
        customer_name=(ctx.customer_name or "").strip(),
        support=ctx.support,
        item=item,
    )


def _render(template: str, lang: str, ctx: ReplyContext) -> str:
    from .templates import registry  # lazy: templates imports DEFAULTS from here

    if template.startswith("custom:"):
        return render_text(registry.custom_text(template[7:], lang), lang, ctx)
    return render_text(registry.text(template, lang), lang, ctx)


def build(template: str, ctx: ReplyContext, language: str = "en") -> str:
    if template not in _T and not template.startswith("custom:"):
        raise KeyError(f"unknown template {template}")
    lang = language if language in LANGS else "en"
    if template in TRILINGUAL:
        return "\n\n".join(_render(template, lg, ctx) for lg in LANGS)
    if template in NEUTRAL:
        return _render(template, "en", ctx)
    return _render(template, lang, ctx)
