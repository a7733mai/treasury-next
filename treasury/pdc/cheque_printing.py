"""Cheque printing on bank layouts (spec 7f §3.3): the leaf of an outgoing PDC Cheque, in Arabic or English.

A `Cheque Layout` says where each item is printed on the leaves of one bank account (mm from the top-left corner
of the physical leaf). `build_leaf` turns a layout and a cheque into the texts and boxes to print (pure: no
database write); `render_leaf` renders them with `templates/includes/treasury_cheque_leaf.html`.

Printing is registered before the leaf renders: "Print Cheque" (a POST, `lifecycle.record_print`) counts the
print, logs "Printed" / "Reprinted" on the cheque and returns the print format to open. The print format itself
(`treasury_cheque_leaf`) refuses to render a cheque that was never registered. A printview URL opened again
later is not counted (a GET cannot write): see README "Cheque printing".

Nothing here posts anything, and security cheques are not printed on layouts.
"""

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, flt, fmt_money, getdate, today
from markupsafe import Markup

from treasury.utils.amount_in_words import treasury_amount_in_words

LANGUAGES = {"Arabic": "ar", "English": "en"}
PRINTABLE_STATUSES = ("Created", "Delivered")
PRINT_ROLES = ("Treasury Manager", "Treasury User", "System Manager")
FORMAT_CHEQUE, FORMAT_BATCH, FORMAT_TEST, LEGACY_FORMAT = (
	"Treasury Cheque",
	"Cheque Batch Leaves",
	"Cheque Layout Test Print",
	"PDC Cheque Print",
)
LAYOUT = "Cheque Layout"
CHEQUE = "PDC Cheque"
BATCH = "Cheque Batch"
TEMPLATE = "treasury/templates/includes/treasury_cheque_leaf.html"
A4_CARRIER = "A4 Carrier"
A4_SIZE = (210, 297)
ARABIC_INDIC = str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")
# Boxes of a leaf: (name, prefix of the top / left / width fields, font size field, height field)
BOXES = (
	("date", "date", "date_font_size_pt", None),
	("payee", "payee", "payee_font_size_pt", None),
	("figures", "figures", "figures_font_size_pt", None),
	("words1", "words1", "words_font_size_pt", None),
	("words2", "words2", "words_font_size_pt", None),
	("crossing", "crossing", "crossing_font_size_pt", None),
	("signature", "signature", None, "signature_height_mm"),
)


class ChequeAlreadyPrintedError(frappe.ValidationError):
	pass


# ---------------------------------------------------------------------------
# Layouts
# ---------------------------------------------------------------------------


def get_layout(bank_account: str | None) -> Document | None:
	"""The enabled Cheque Layout of a bank account: the default one, else the newest."""
	if not bank_account:
		return None
	names = frappe.get_all(
		LAYOUT,
		filters={"bank_account": bank_account, "disabled": 0},
		order_by="is_default desc, creation desc, name desc",
		pluck="name",
		limit=1,
	)
	return frappe.get_doc(LAYOUT, names[0]) if names else None


def has_layout(bank_account: str | None) -> bool:
	return bool(bank_account) and bool(
		frappe.db.exists(LAYOUT, {"bank_account": bank_account, "disabled": 0})
	)


def mapping_print_format(company: str | None, bank_account: str | None) -> str | None:
	"""The (legacy) Cheque Print Format of the bank's PDC Account Mapping row, if any."""
	if not (company and bank_account):
		return None
	formats = frappe.get_all(
		"PDC Account Mapping",
		filters={"parenttype": "Treasury Company Settings", "parent": company, "bank_account": bank_account},
		pluck="cheque_print_format",
		limit=1,
	)
	return formats[0] if formats else None


def resolve_print_format(company: str | None, bank_account: str | None) -> str:
	"""The print format of an outgoing cheque: its bank's Cheque Layout, else the PDC Account Mapping format,
	else the legacy "PDC Cheque Print"."""
	if has_layout(bank_account):
		return FORMAT_CHEQUE
	return mapping_print_format(company, bank_account) or LEGACY_FORMAT


def lang_code(language: str | None, layout=None) -> str:
	""""ar" / "en" from a code or a language name; the layout's default language when none is given."""
	if not language:
		return LANGUAGES.get(layout.default_language, "en") if layout else "en"
	if language in LANGUAGES.values():
		return language
	if language in LANGUAGES:
		return LANGUAGES[language]
	frappe.throw(_("Language must be Arabic or English."))


def language_label(code: str) -> str:
	return next(name for name, value in LANGUAGES.items() if value == code)


def currency_decimals(currency: str | None) -> int:
	"""Decimals of a currency from its fraction units (1000 → 3, 100 → 2; none → 2)."""
	units = cint(frappe.get_cached_value("Currency", currency, "fraction_units")) if currency else 0
	if units <= 1:
		return 2
	return max(len(str(units)) - 1, 0)


# ---------------------------------------------------------------------------
# Building and rendering a leaf
# ---------------------------------------------------------------------------


def split_words(text: str, max_chars: int) -> tuple[str, str]:
	"""The amount in words over two lines: line 1 at most `max_chars`, cut at the last space before it."""
	text = (text or "").strip()
	max_chars = cint(max_chars)
	if len(text) <= max_chars:
		return text, ""
	cut = text.rfind(" ", 0, max_chars + 1)
	if cut <= 0:
		cut = max_chars
	return text[:cut].strip(), text[cut:].strip()


def format_cheque_date(date, date_format: str) -> str:
	value = getdate(date)
	text = date_format or "dd/mm/yyyy"
	for token, part in (("yyyy", f"{value.year:04d}"), ("mm", f"{value.month:02d}"), ("dd", f"{value.day:02d}")):
		text = text.replace(token, part)
	return text


def _mm(value) -> str:
	return f"{flt(value, 2):.2f}mm"


def build_leaf(layout, *, payee, amount, currency, date, company, lang) -> frappe._dict:
	"""Texts and boxes of one leaf (pure: reads the Currency master only)."""
	arabic = lang == "ar"
	indic = arabic and layout.digits == "Arabic-Indic"

	date_text = format_cheque_date(date, layout.date_format)
	guard = layout.figures_guard or ""
	figures = f"{guard}{fmt_money(amount, precision=currency_decimals(currency))}{guard}"
	if indic:
		date_text, figures = date_text.translate(ARABIC_INDIC), figures.translate(ARABIC_INDIC)

	currency_name = (layout.currency_name_ar if arabic else layout.currency_name_en) or None
	fraction_name = (layout.fraction_name_ar if arabic else layout.fraction_name_en) or None
	words = treasury_amount_in_words(
		amount,
		currency,
		lang,
		only=bool(cint(layout.add_only_suffix)),
		currency_name=currency_name or (None if cint(layout.show_currency_name) else ""),
		fraction_name=fraction_name,
		fraction_style=layout.fraction_style or "Words",
	)
	words1, words2 = split_words(words, layout.words1_max_chars)

	crossing = (layout.crossing_text_ar if arabic else layout.crossing_text_en) or _(
		"A/C Payee Only", lang=lang
	)
	if layout.signature_label == "Company Name":
		signature = company or ""
	elif layout.signature_label == "Custom Text":
		signature = layout.signature_text or ""
	else:
		signature = ""

	carrier = layout.paper == A4_CARRIER
	offset_top = flt(layout.offset_top_mm) if carrier else 0
	offset_left = flt(layout.offset_left_mm) if carrier else 0
	base_font = flt(layout.base_font_size_pt) or 11
	boxes = {}
	for name, prefix, font_field, height_field in BOXES:
		font = flt(layout.get(font_field)) if font_field else 0
		boxes[name] = frappe._dict(
			top=_mm(flt(layout.get(f"{prefix}_top_mm")) + flt(layout.nudge_y_mm) + offset_top),
			left=_mm(flt(layout.get(f"{prefix}_left_mm")) + flt(layout.nudge_x_mm) + offset_left),
			width=_mm(layout.get(f"{prefix}_width_mm")),
			height=_mm(layout.get(height_field)) if height_field else None,
			font_size=f"{flt(font or base_font, 1)}pt",
			rotation=flt(layout.crossing_rotation_deg) if name == "crossing" else 0,
		)

	page_width, page_height = A4_SIZE if carrier else (layout.leaf_width_mm, layout.leaf_height_mm)
	return frappe._dict(
		lang=lang,
		dir="rtl" if arabic else "ltr",
		date=date_text,
		date_chars=list(date_text) if flt(layout.date_char_spacing_mm) > 0 else [],
		date_char_spacing=_mm(layout.date_char_spacing_mm) if flt(layout.date_char_spacing_mm) > 0 else None,
		payee=payee or "",
		figures=figures,
		words=words,
		words1=words1,
		words2=words2,
		crossing=crossing,
		print_crossing=cint(layout.print_crossing),
		signature=signature,
		boxes=boxes,
		page_width=_mm(page_width),
		page_height=_mm(page_height),
		leaf_top=_mm(offset_top),
		leaf_left=_mm(offset_left),
		leaf_width=_mm(layout.leaf_width_mm),
		leaf_height=_mm(layout.leaf_height_mm),
		font_family=layout.font_family or "Arial, Tahoma, sans-serif",
		font_size=f"{flt(base_font, 1)}pt",
	)


def render_leaf(layout, leaf: frappe._dict, *, test_grid=False) -> Markup:
	return Markup(frappe.render_template(TEMPLATE, {"leaf": leaf, "test_grid": test_grid}))


def cheque_leaf(layout, cheque, lang: str, test_grid=False) -> Markup:
	"""The rendered leaf of a PDC Cheque (its effective due date is the cheque date)."""
	leaf = build_leaf(
		layout,
		payee=cheque.get("payee_on_cheque") or cheque.party_name or cheque.party,
		amount=cheque.amount,
		currency=cheque.currency,
		date=cheque.effective_due_date or cheque.original_due_date,
		company=cheque.company,
		lang=lang,
	)
	return render_leaf(layout, leaf, test_grid=test_grid)


def notice(message: str) -> Markup:
	return Markup('<div class="treasury-cheque-notice">{0}</div>').format(message)


def current_lang() -> str:
	return "ar" if (getattr(frappe.local, "lang", None) or "en").startswith("ar") else "en"


def printable_error(cheque) -> str | None:
	"""Why a PDC Cheque cannot be printed on its bank's layout now (None if it can)."""
	if cheque.direction != "Outgoing":
		return _("Only outgoing cheques can be printed.")
	if cheque.operational_status not in PRINTABLE_STATUSES:
		return _("Cheque {0} is {1}; only Created or Delivered cheques can be printed.").format(
			cheque.name, _(cheque.operational_status)
		)
	return None


# ---------------------------------------------------------------------------
# Jinja helpers (hooks.py `jinja.methods`): any template may call them
# ---------------------------------------------------------------------------


def treasury_cheque_leaf(doc) -> Markup:
	"""The leaf of a registered outgoing PDC Cheque (print format "Treasury Cheque"), in the print language."""
	if getattr(doc, "doctype", None) != CHEQUE or not doc.get("name"):
		return Markup("")
	if not frappe.has_permission(CHEQUE, "print", doc=doc.name) or doc.direction != "Outgoing":
		return Markup("")
	layout = get_layout(doc.bank_account)
	if not layout:
		return notice(
			_("Bank Account {0} has no Cheque Layout. Create one first.").format(doc.bank_account or "")
		)
	if not cint(doc.print_count):
		return notice(_('Use "Print Cheque" on the cheque to print it.'))
	if doc.operational_status not in PRINTABLE_STATUSES:
		return notice(
			_("Cheque {0} is {1}; it cannot be printed.").format(doc.name, _(doc.operational_status))
		)
	return wrap_page(cheque_leaf(layout, doc, current_lang()))


def treasury_batch_cheque_leaves(doc) -> Markup:
	"""One page per registered, printable cheque of a Cheque Batch the user may read, in batch row order
	(print format "Cheque Batch Leaves")."""
	from treasury.pdc.batch import get_batch_cheques

	if getattr(doc, "doctype", None) != BATCH or not doc.get("name"):
		return Markup("")
	if not frappe.has_permission(BATCH, "print", doc=doc.name):
		return Markup("")
	layout = get_layout(doc.bank_account)
	if doc.direction != "Outgoing" or not layout:
		return notice(_('Use "Print Cheque" on the cheque to print it.'))
	row_idx = {row.name: row.idx for row in doc.cheques}
	cheques = sorted(
		get_batch_cheques(
			doc.name,
			fields=[
				"name",
				"direction",
				"company",
				"party",
				"party_name",
				"payee_on_cheque",
				"amount",
				"currency",
				"effective_due_date",
				"original_due_date",
				"operational_status",
				"print_count",
				"cheque_batch_row",
			],
			as_user=True,
		),
		key=lambda c: row_idx.get(c.cheque_batch_row, len(row_idx) + 1),
	)
	lang = current_lang()
	pages = [
		wrap_page(cheque_leaf(layout, c, lang))
		for c in cheques
		if c.operational_status in PRINTABLE_STATUSES and cint(c.print_count)
	]
	if not pages:
		return notice(_('Use "Print Cheque" on the cheque to print it.'))
	return Markup("").join(pages)


def treasury_cheque_layout_test(doc) -> Markup:
	"""A sample leaf with the grid and every box outlined, on blank paper (print format "Cheque Layout Test
	Print"). Nothing is counted."""
	if getattr(doc, "doctype", None) != LAYOUT:
		return Markup("")
	if doc.get("name") and not doc.is_new() and not frappe.has_permission(LAYOUT, "read", doc=doc.name):
		return Markup("")
	return render_test(doc, current_lang())


def render_test(layout, lang: str) -> Markup:
	from treasury.security_cheques.lifecycle import bank_account_currency

	currency = bank_account_currency(layout.bank_account) if layout.bank_account else None
	company = layout.company or (
		frappe.db.get_value("Bank Account", layout.bank_account, "company") if layout.bank_account else None
	)
	leaf = build_leaf(
		layout,
		payee=_("Sample Payee Name Trading Company", lang=lang),
		amount=flt(layout.test_amount),
		currency=currency,
		date=today(),
		company=company,
		lang=lang,
	)
	return render_leaf(layout, leaf, test_grid=True)


def wrap_page(html: Markup) -> Markup:
	return Markup('<div class="treasury-cheque-page">{0}</div>').format(html)


@frappe.whitelist()
def preview_layout(doc: str | dict, language: str | None = None) -> str:
	"""The test print of an unsaved Cheque Layout (the form's live preview). Never saves anything."""
	frappe.has_permission(LAYOUT, "read", throw=True)
	values = frappe.parse_json(doc)
	if not isinstance(values, dict) or values.get("doctype") != LAYOUT:
		frappe.throw(_("Invalid document."))
	layout = frappe.get_doc(values)
	return str(render_test(layout, lang_code(language, layout)))


# ---------------------------------------------------------------------------
# Batch registration
# ---------------------------------------------------------------------------


def register_prints(
	names: list[str], language: str | None, reprint=0, reason: str | None = None, only_unprinted=0
) -> dict:
	"""Register the print of several cheques, all or nothing: every cheque is checked first; with
	`only_unprinted` the printed ones are skipped, otherwise one ChequeAlreadyPrintedError names them all."""
	from treasury.pdc import lifecycle

	# steps 1-5 of lifecycle.record_print for every cheque before anything is written
	cheques = [lifecycle.begin_print(name) for name in names]
	for cheque in cheques:
		lang_code(language, lifecycle.check_printable(cheque))
	printed_before = [c for c in cheques if cint(c.print_count)]
	skipped = [c.name for c in printed_before] if cint(only_unprinted) else []
	if printed_before and not skipped:
		if not cint(reprint):
			frappe.throw(
				_("These cheques were already printed: {0}.").format(
					", ".join(f"{c.cheque_no} ({c.name})" for c in printed_before)
				),
				ChequeAlreadyPrintedError,
			)
		for cheque in printed_before:
			lifecycle.check_reprint(cheque, reason)

	printed = []
	for cheque in cheques:
		if cheque.name not in skipped:
			lifecycle.record_print(cheque.name, language, reprint=reprint, reason=reason)
			printed.append(cheque.name)
	return {"printed": printed, "skipped": skipped}
