"""Amount in words in a given language (exposed to print formats through hooks.py `jinja`).

Frappe's `money_in_words` uses the current language through num2words, but puts the currency code first
("EGP One Thousand only") and switches to Indian grouping with some number formats. Cheques need the
wording of the print language, e.g. "فقط ألف جنيه مصري لا غير", so the phrase is built here from
num2words (which supports Arabic) and our own translatable pieces.
"""

import frappe
from frappe import _
from frappe.utils import cint, flt

# Translation context of the pieces below (msgctxt in ar.po), so that they never override the Frappe /
# ERPNext translation of the same words elsewhere.
CONTEXT = "Amount in words"
# Fraction units of a currency whose master has none (cheque_printing.currency_decimals: 2 decimals).
DEFAULT_FRACTION_UNITS = 100

# Display names of common currencies (the Currency DocType only holds the code); translated in ar.po.
CURRENCY_NAMES = {
	"EGP": "Egyptian Pound",
	"USD": "US Dollar",
	"EUR": "Euro",
	"GBP": "Pound Sterling",
	"SAR": "Saudi Riyal",
	"AED": "UAE Dirham",
	"KWD": "Kuwaiti Dinar",
	"QAR": "Qatari Riyal",
	"BHD": "Bahraini Dinar",
	"OMR": "Omani Rial",
	"JOD": "Jordanian Dinar",
}


def treasury_amount_in_words(
	amount,
	currency: str | None = None,
	lang: str | None = None,
	*,
	only: bool = True,
	currency_name: str | None = None,
	fraction_name: str | None = None,
	fraction_style: str = "Words",
) -> str:
	"""`amount` of `currency` in words, in `lang` (default: the current / print language).

	The defaults give the voucher wording ("... only"). A Cheque Layout may drop the "only" wrapper (`only`),
	name the currency and its fraction itself (`currency_name` / `fraction_name`; `currency_name=""` leaves
	the currency out, None means the translated default) and print the fraction in figures
	(`fraction_style="Figures"`: "... and 50/100")."""
	lang = lang or getattr(frappe.local, "lang", None) or "en"
	amount = flt(amount)
	if amount < 0:
		return ""

	default_fraction, fraction_units = None, 0
	if currency:
		values = frappe.get_cached_value("Currency", currency, ["fraction", "fraction_units"], as_dict=True)
		if values:
			default_fraction, fraction_units = values.fraction, cint(values.fraction_units)
	# a currency without fraction units still has cents: its fraction is worded, never dropped
	fraction_units = fraction_units or DEFAULT_FRACTION_UNITS

	# round to the currency's decimals first: 10.999 is 11.00, never "ten and 100/100"
	amount = flt(amount, len(str(fraction_units)) - 1)
	main = int(amount)
	fraction = round((amount - main) * fraction_units) if fraction_units else 0
	if fraction_units and fraction >= fraction_units:
		main, fraction = main + 1, 0

	# Arabic number-noun agreement applies to the known currencies' own names (not to a Cheque Layout's)
	arabic = (lang or "en").split("-")[0].split("_")[0] == "ar"
	forms = ARABIC_FORMS.get(currency or "") if arabic else None
	main_forms = forms[0] if forms and currency_name is None else None
	fraction_forms = forms[1] if forms and fraction_name is None else None

	if currency_name is None:
		currency_name = CURRENCY_NAMES.get(currency, currency or "")
		currency_name = _(currency_name, lang=lang, context=CONTEXT) if currency_name else ""
	# a fraction-only amount (0.75) is the fraction alone: "seventy five dirhams", never "zero riyals and ..."
	fraction_only = main == 0 and fraction > 0 and fraction_style != "Figures"
	if fraction_only:
		text = ""
	elif main_forms:
		text = counted_in_arabic(main, main_forms)
	else:
		text = f"{number_in_words(main, lang)} {currency_name}".strip()
	if fraction and fraction_style == "Figures":
		text = _("{0} and {1}/{2}", lang=lang, context=CONTEXT).format(text, fraction, fraction_units)
	elif fraction:
		if fraction_name is None:
			fraction_name = _(default_fraction, lang=lang, context=CONTEXT) if default_fraction else ""
		if fraction_forms:
			fraction_text = counted_in_arabic(fraction, fraction_forms)
		else:
			fraction_text = f"{number_in_words(fraction, lang)} {fraction_name}".strip()
		if fraction_only:
			text = fraction_text
		elif arabic:
			text = f"{text} و{fraction_text}"  # the Arabic conjunction attaches to the word it introduces
		else:
			text = _("{0} and {1} {2}", lang=lang, context=CONTEXT).format(text, fraction_text, "")
	text = text.strip()
	return _("{0} only", lang=lang, context=CONTEXT).format(text) if only else text


def number_in_words(number: int, lang: str = "en") -> str:
	"""A whole number in words (num2words), English when the language is not supported."""
	from num2words import num2words

	code = (lang or "en").split("-")[0].split("_")[0]
	try:
		words = num2words(int(number), lang=code)
	except (NotImplementedError, OverflowError):
		code = "en"
		words = num2words(int(number), lang=code)
	words = words.replace("-", " ")
	if code == "ar":
		words = words.replace(" و ", " و")  # the conjunction attaches to the word it introduces
	return words.title() if code == "en" else words


# Arabic forms of a currency and of its fraction unit, per currency code: (singular, dual, plural,
# accusative singular). Arabic counts nouns by number, not by a plural suffix: see `counted_in_arabic`.
ARABIC_FORMS = {
	"QAR": (
		("ريال قطري", "ريالان قطريان", "ريالات قطرية", "ريالاً قطرياً"),
		("درهم", "درهمان", "دراهم", "درهماً"),
	),
	"SAR": (
		("ريال سعودي", "ريالان سعوديان", "ريالات سعودية", "ريالاً سعودياً"),
		("هللة", "هللتان", "هللات", "هللةً"),
	),
	"AED": (
		("درهم إماراتي", "درهمان إماراتيان", "دراهم إماراتية", "درهماً إماراتياً"),
		("فلس", "فلسان", "فلوس", "فلساً"),
	),
	"KWD": (
		("دينار كويتي", "ديناران كويتيان", "دنانير كويتية", "ديناراً كويتياً"),
		("فلس", "فلسان", "فلوس", "فلساً"),
	),
	"USD": (
		("دولار أمريكي", "دولاران أمريكيان", "دولارات أمريكية", "دولاراً أمريكياً"),
		("سنت", "سنتان", "سنتات", "سنتاً"),
	),
	"EGP": (
		("جنيه مصري", "جنيهان مصريان", "جنيهات مصرية", "جنيهاً مصرياً"),
		("قرش", "قرشان", "قروش", "قرشاً"),
	),
}


def counted_in_arabic(number: int, forms: tuple[str, str, str, str]) -> str:
	"""`number` of a noun in Arabic, the noun in the form its number requires (`forms`: singular, dual,
	plural, accusative singular): 1 follows the noun ("ريال قطري واحد"); 2 is the dual alone; the last two
	digits 3-10 take the plural; 11-99 (and 1 or 2 after hundreds/thousands, "مئة وواحد ريالاً") the
	accusative singular; a round hundred, thousand or million, and zero, the genitive singular."""
	singular, dual, plural, accusative = forms
	tail = number % 100
	if number == 1:
		return f"{singular} {number_in_words(1, 'ar')}"
	if number == 2:
		return dual
	words = number_in_words(number, "ar")
	if 3 <= tail <= 10:
		return f"{words} {plural}"
	if tail == 0:
		return f"{words} {singular}"
	return f"{words} {accusative}"
