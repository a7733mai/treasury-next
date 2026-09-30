"""Cheque printing on bank layouts (spec 7f §3.3): the leaf of an outgoing PDC Cheque, in Arabic or English.

A `Cheque Layout` says where each item is printed on the leaves of one bank account (mm from the top-left corner
of the physical leaf). `build_leaf` turns a layout and a cheque into the texts and boxes to print (pure: no
database write); `render_leaf` renders them with `templates/includes/treasury_cheque_leaf.html`.

Printing is registered before the leaf renders: "Print Cheque" (a POST, `lifecycle.record_print`) counts the
print, logs "Printed" / "Reprinted" on the cheque, stamps a print job id and the fingerprint of the printed
content, and returns the print format and the job to open. The print formats (`treasury_cheque_leaf`,
`treasury_batch_cheque_leaves`) render a leaf only (`leaf_refusal`):
- for a user holding one of the PRINT_ROLES (as the registration does);
- from the saved cheque, never from a document sent by the client (printview `doc=`);
- for the print job of the URL, registered by this same user;
- while the printed content (payee, amount, currency, date, bank, number) is the registered one.
A printview URL opened again later by the same user is not counted (a GET cannot write): see README
"Cheque printing".

Nothing here posts anything, and security cheques are not printed on layouts.
"""

import hashlib
import math
import re

import frappe
from frappe import _
from frappe.locale import get_number_format
from frappe.model.document import Document
from frappe.utils import cint, escape_html, flt, fmt_money, getdate, today
from markupsafe import Markup

from treasury.pdc.report_utils import is_permitted_value
from treasury.utils.amount_in_words import treasury_amount_in_words
from treasury.utils.context import real_user

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
# Share of the base font size a box without its own size prints in.
BASE_FONT_SCALE = {"crossing": 0.9, "signature": 0.85}
# PDC Cheque fields whose value appears on (or identifies) the printed leaf: locked by a form / REST save once
# the cheque was printed (PDCCheque._validate_printed_fields). The lifecycle may still move the date
# (Postpone): the fingerprint then differs and the leaf renders again only after a registered reprint.
PRINTED_FIELDS = (
	"company",
	"bank_account",
	"cheque_book",
	"cheque_no",
	"party_type",
	"party",
	"payee_on_cheque",
	"amount",
	"currency",
	"original_due_date",
)
# Fields a leaf is rendered from (and fingerprinted on): read from the database, never from the client.
LEAF_FIELDS = (
	"name",
	"direction",
	"company",
	"bank_account",
	"cheque_no",
	"party",
	"party_name",
	"payee_on_cheque",
	"amount",
	"currency",
	"effective_due_date",
	"original_due_date",
	"operational_status",
	"print_count",
	"last_printed_by",
	"last_print_job",
	"printed_fingerprint",
)
# The request argument of a printview URL naming the registration it prints (lifecycle.record_print).
PRINT_JOB_ARG = "print_job"
PRINT_JOB_PATTERN = re.compile(r"^[0-9a-f]{10,40}$")
# Layout font family: font names, commas, spaces and quotes only (it is written into a style attribute).
FONT_FAMILY_PATTERN = re.compile(r"^[\w\s,'\"-]{1,100}$")
DEFAULT_FONT_FAMILY = "Arial, Tahoma, sans-serif"


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


def resolve_print_format(company: str | None, bank_account: str | None, layout=None) -> str:
	"""The print format of an outgoing cheque: its bank's Cheque Layout, else the PDC Account Mapping format,
	else the legacy "PDC Cheque Print". `layout`: the bank's layout when the caller already loaded it."""
	if layout or has_layout(bank_account):
		return FORMAT_CHEQUE
	return mapping_print_format(company, bank_account) or LEGACY_FORMAT


def lang_code(language: str | None, layout=None) -> str:
	""" "ar" / "en" from a code or a language name; the layout's default language when none is given."""
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
	"""Decimals of a currency from its fraction units (1000 → 3, 100 → 2, 10 → 1; none, or units that are not
	a power of 10, e.g. 5 → 2)."""
	units = cint(frappe.get_cached_value("Currency", currency, "fraction_units")) if currency else 0
	if units <= 1:
		return 2
	decimals = round(math.log10(units))
	return decimals if 10**decimals == units else 2


# ---------------------------------------------------------------------------
# Print registration: job and fingerprint (lifecycle.record_print writes them, the formats check them)
# ---------------------------------------------------------------------------


def new_print_job() -> str:
	return frappe.generate_hash(length=20)


def payee_text(cheque) -> str:
	return cheque.get("payee_on_cheque") or cheque.get("party_name") or cheque.get("party") or ""


def cheque_date(cheque):
	return cheque.get("effective_due_date") or cheque.get("original_due_date")


def printed_fingerprint(cheque) -> str:
	"""A hash of what the leaf shows (and the leaf it is printed on): a print renders only while the saved
	cheque still has the fingerprint registered with the print."""
	date = cheque_date(cheque)
	values = [
		cheque.get("company"),
		cheque.get("bank_account"),
		cheque.get("cheque_no"),
		payee_text(cheque),
		f"{flt(cheque.get('amount'), 9):.9f}",
		cheque.get("currency"),
		getdate(date) if date else None,
	]
	return hashlib.sha256("\x1f".join(str(v or "") for v in values).encode()).hexdigest()


def request_print_job() -> str | None:
	"""The print job named by the printview URL (`print_job=`), if it looks like one."""
	job = (getattr(frappe.local, "form_dict", None) or {}).get(PRINT_JOB_ARG)
	return job if isinstance(job, str) and PRINT_JOB_PATTERN.match(job) else None


def has_print_role() -> bool:
	return bool(set(PRINT_ROLES) & set(frappe.get_roles()))


def print_role_notice() -> Markup:
	return notice(
		_("This action requires one of the roles: {0}.").format(", ".join(_(r) for r in PRINT_ROLES))
	)


def leaf_refusal(cheque, job: str | None) -> str | None:
	"""Why the saved `cheque` (LEAF_FIELDS) may not render for this print now (None: it may). The leaf renders
	only for the registered print job of the URL, registered by this user, while the content is unchanged."""
	if cheque.operational_status not in PRINTABLE_STATUSES:
		return _("Cheque {0} is {1}; it cannot be printed.").format(cheque.name, _(cheque.operational_status))
	if (
		not cint(cheque.print_count)
		or not job
		or cheque.last_print_job != job
		or cheque.last_printed_by != real_user()
	):
		return _('Use "Print Cheque" on the cheque to print it.')
	if cheque.printed_fingerprint != printed_fingerprint(cheque):
		return _(
			'Cheque {0} changed after it was printed. Print it again with "Print Cheque" (a reprint).'
		).format(cheque.name)
	return None


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
	for token, part in (
		("yyyy", f"{value.year:04d}"),
		("mm", f"{value.month:02d}"),
		("dd", f"{value.day:02d}"),
	):
		text = text.replace(token, part)
	return text


def format_figures(amount, decimals: int) -> str:
	"""The amount in figures with the system number format and exactly `decimals` decimals, no symbol.

	`fmt_money` drops the third decimal of a 3-decimal amount whose last digit is 0 when no currency is given
	(KWD 1.250 → "1.25"), and giving the currency adds its symbol: the missing zeros are padded here."""
	text = fmt_money(amount, precision=decimals)
	separator = get_number_format().decimal_separator
	if decimals <= 2 or not separator:
		return text
	whole, found, fraction = text.rpartition(separator)
	if not found:
		whole, fraction = text, ""
	return f"{whole}{separator}{fraction.ljust(decimals, '0')}"


def _mm(value) -> str:
	return f"{flt(value, 2):.2f}mm"


def _mm_label(value) -> str:
	"""A layout value as the user typed it (the test grid label "Payee 25,22")."""
	return f"{flt(value, 1):g}"


def build_leaf(layout, *, payee, amount, currency, date, company, lang) -> frappe._dict:
	"""Texts and boxes of one leaf (pure: reads the Currency master only)."""
	arabic = lang == "ar"
	indic = arabic and layout.digits == "Arabic-Indic"

	date_text = format_cheque_date(date, layout.date_format)
	guard = layout.figures_guard or ""
	amount_text = format_figures(amount, currency_decimals(currency))
	if indic:
		date_text, amount_text = date_text.translate(ARABIC_INDIC), amount_text.translate(ARABIC_INDIC)
	figures = f"{guard}{amount_text}{guard}"

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
	boxes = frappe._dict()
	for name, prefix, font_field, height_field in BOXES:
		font = flt(layout.get(font_field)) if font_field else 0
		top, left = flt(layout.get(f"{prefix}_top_mm")), flt(layout.get(f"{prefix}_left_mm"))
		boxes[name] = frappe._dict(
			top=_mm(top + flt(layout.nudge_y_mm) + offset_top),
			left=_mm(left + flt(layout.nudge_x_mm) + offset_left),
			width=_mm(layout.get(f"{prefix}_width_mm")),
			height=_mm(layout.get(height_field)) if height_field else None,
			# the crossing and the signature print smaller than the base size unless a size is set (design §7)
			font_size=f"{flt(font or base_font * BASE_FONT_SCALE.get(name, 1), 1)}pt",
			rotation=flt(layout.crossing_rotation_deg) if name == "crossing" else 0,
			# test grid: "left,top" as typed in the layout, i.e. the numbers to change
			position=f"{_mm_label(left)},{_mm_label(top)}",
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
		figures_guard=guard,
		figures_amount=amount_text,
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
		font_family=safe_font_family(layout.font_family),
		font_size=f"{flt(base_font, 1)}pt",
	)


def is_valid_font_family(value: str | None) -> bool:
	"""Font names, commas, spaces and balanced quotes only: the value is written into a style attribute."""
	return bool(
		value and FONT_FAMILY_PATTERN.match(value) and value.count('"') % 2 == 0 and value.count("'") % 2 == 0
	)


def safe_font_family(value: str | None) -> str:
	"""The layout's font family, or the default one for a value ChequeLayout.validate would refuse (e.g. saved
	before that check): nothing else ever reaches the style attribute."""
	return value if is_valid_font_family(value) else DEFAULT_FONT_FAMILY


def render_leaf(layout, leaf: frappe._dict, *, test_grid=False) -> Markup:
	return Markup(frappe.render_template(TEMPLATE, {"leaf": leaf, "test_grid": test_grid}))


def cheque_leaf(layout, cheque, lang: str, test_grid=False) -> Markup:
	"""The rendered leaf of a PDC Cheque (its effective due date is the cheque date)."""
	leaf = build_leaf(
		layout,
		payee=payee_text(cheque),
		amount=cheque.amount,
		currency=cheque.currency,
		date=cheque_date(cheque),
		company=cheque.company,
		lang=lang,
	)
	return render_leaf(layout, leaf, test_grid=test_grid)


def notice(message: str) -> Markup:
	"""Printed instead of the leaf: nothing else prints, so a leaf fed by mistake receives no amount."""
	return Markup(
		'<div class="treasury-cheque-notice" style="margin:10mm auto;max-width:150mm;padding:4mm;'
		'border:1px dashed #777;color:#777;font-size:12pt;text-align:center;">{0}</div>'
	).format(message)


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


def saved_doc(doctype: str, doc):
	"""The saved document behind a print format's `doc`: printview also renders a document sent by the
	client (`doc=` JSON), which must never be printed from. None when it does not exist."""
	name = doc.get("name") if getattr(doc, "doctype", None) == doctype else None
	if not isinstance(name, str) or not frappe.db.exists(doctype, name):
		return None
	return frappe.get_doc(doctype, name)


def treasury_cheque_leaf(doc) -> Markup:
	"""The leaf of an outgoing PDC Cheque for its registered print (print format "Treasury Cheque"), in the
	print language. Rendered from the saved cheque only; see `leaf_refusal`."""
	cheque = saved_doc(CHEQUE, doc)
	if not cheque or not frappe.has_permission(CHEQUE, "print", doc=cheque.name):
		return Markup("")
	if not has_print_role():
		return print_role_notice()
	if cheque.direction != "Outgoing":
		return Markup("")
	if printed_fingerprint(doc) != printed_fingerprint(cheque):
		# an unsaved (or forged) document: what would print is not what was registered
		return notice(_("Save the cheque before printing it."))
	layout = get_layout(cheque.bank_account)
	if not layout:
		return notice(
			_("Bank Account {0} has no Cheque Layout. Create one first.").format(cheque.bank_account or "")
		)
	if refusal := leaf_refusal(cheque, request_print_job()):
		return notice(refusal)
	return wrap_page(cheque_leaf(layout, cheque, current_lang()))


def treasury_batch_cheque_leaves(doc) -> Markup:
	"""One page per cheque of a Cheque Batch registered in the print job of the URL (by this user), in batch
	row order (print format "Cheque Batch Leaves"). Cheques not in the job never print: "Print Unprinted
	Only" prints exactly the leaves it registered. Rendered from the saved batch and cheques only."""
	from treasury.pdc.batch import get_batch_cheques

	batch = saved_doc(BATCH, doc)
	if not batch or not frappe.has_permission(BATCH, "print", doc=batch.name):
		return Markup("")
	if not has_print_role():
		return print_role_notice()
	layout = get_layout(batch.bank_account)
	job = request_print_job()
	if batch.direction != "Outgoing" or not layout or not job:
		return notice(_('Use "Print Cheque" on the cheque to print it.'))
	cheques = in_row_order(
		batch,
		[
			c
			for c in get_batch_cheques(batch.name, fields=[*LEAF_FIELDS, "cheque_batch_row"], as_user=True)
			if c.last_print_job == job
		],
	)
	lang = current_lang()
	# a cheque of the job that changed since (or became unprintable) prints a notice on its leaf: the pages
	# still match the leaves loaded, and that leaf receives no amount
	pages = [
		wrap_page(notice(refusal) if (refusal := leaf_refusal(c, job)) else cheque_leaf(layout, c, lang))
		for c in cheques
	]
	if not pages:
		return notice(_('Use "Print Cheque" on the cheque to print it.'))
	return Markup("").join(pages)


def treasury_cheque_layout_test(doc) -> Markup:
	"""A sample leaf with the grid and every box outlined, on blank paper (print format "Cheque Layout Test
	Print"). Nothing is counted. Only for a bank account the user may see (`bank_access_error`)."""
	if getattr(doc, "doctype", None) != LAYOUT:
		return Markup("")
	saved = saved_doc(LAYOUT, doc)
	if not frappe.has_permission(LAYOUT, "read", doc=saved.name if saved else None):
		return Markup("")
	if error := bank_access_error(doc.bank_account):
		return notice(error)
	return render_test(doc, current_lang())


def bank_access_error(bank_account: str | None) -> str | None:
	"""Why the user may not see `bank_account` in a Cheque Layout (None: they may): it must be a company bank
	account, and User Permissions on Company / Bank Account (for Cheque Layout) must allow it. Treasury roles
	hold no role permission on Bank Account (install.py), so User Permissions are what restricts them."""
	if not bank_account:
		return None
	company = frappe.db.get_value("Bank Account", {"name": bank_account, "is_company_account": 1}, "company")
	if not company:
		return _("Bank Account {0} must be a company bank account with a GL account.").format(bank_account)
	permitted = is_permitted_value("Company", company, LAYOUT) and is_permitted_value(
		"Bank Account", bank_account, LAYOUT
	)
	if not permitted:
		return _("You are not permitted to view Bank Account {0}.").format(bank_account)
	return None


def check_bank_access(bank_account: str | None):
	"""PermissionError unless the user may see `bank_account` in a Cheque Layout (the message is plain text:
	the value may come from the client)."""
	if error := bank_access_error(bank_account):
		frappe.throw(escape_html(error), frappe.PermissionError, title=_("Not Permitted"))


def render_test(layout, lang: str) -> Markup:
	"""The test grid leaf. The company and currency always come from the saved bank account (callers checked
	`bank_access_error`), never from the layout values."""
	from treasury.security_cheques.lifecycle import bank_account_currency

	currency = bank_account_currency(layout.bank_account) if layout.bank_account else None
	company = (
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


def in_row_order(batch, cheques: list) -> list:
	"""Cheques of a batch in batch row order (a replacement follows its row; stable otherwise)."""
	row_idx = {row.name: row.idx for row in batch.cheques}
	return sorted(cheques, key=lambda c: row_idx.get(c.cheque_batch_row, len(row_idx) + 1))


def reprint_requires_manager(company: str | None) -> bool:
	"""Treasury Company Settings: only a Treasury Manager may reprint a cheque (lifecycle.check_reprint)."""
	return bool(
		company
		and cint(frappe.db.get_value("Treasury Company Settings", company, "cheque_reprint_requires_manager"))
	)


def layout_info(layout, company: str | None) -> dict | None:
	"""What the forms need to offer the print dialog (onload of PDC Cheque and Cheque Batch)."""
	if not layout:
		return None
	return {
		"name": layout.name,
		"default_language": layout.default_language,
		"reprint_requires_manager": reprint_requires_manager(company),
	}


def batch_print_summary(batch) -> dict | None:
	"""The printable cheques of an outgoing submitted batch the user may read, for the "Print All Cheques"
	dialog (design 7f P16): the same selection as ChequeBatch.print_cheques, in batch row order."""
	from treasury.pdc.batch import get_batch_cheques

	if batch.docstatus != 1 or batch.direction != "Outgoing":
		return None
	cheques = in_row_order(
		batch,
		[
			c
			for c in get_batch_cheques(
				batch.name,
				fields=[
					"name",
					"cheque_no",
					"amount",
					"operational_status",
					"print_count",
					"cheque_batch_row",
				],
				as_user=True,
			)
			if c.operational_status in PRINTABLE_STATUSES
		],
	)
	unprinted = [c for c in cheques if not cint(c.print_count)]
	return {
		"layout": layout_info(get_layout(batch.bank_account), batch.company),
		"printable": len(cheques),
		"unprinted": len(unprinted),
		"printed": len(cheques) - len(unprinted),
		"total": flt(sum(flt(c.amount) for c in cheques)),
		"leaves": [c.cheque_no for c in cheques],
		"unprinted_leaves": [c.cheque_no for c in unprinted],
	}


@frappe.whitelist()
def get_cheque_text(name: str, language: str | None = None) -> dict:
	"""The texts a cheque's leaf would print (payee, date, figures, words, crossing), escaped, for the last check
	in the "Print Cheque" dialog (design 7f P14). Read only: registers nothing."""
	frappe.has_permission(CHEQUE, "print", doc=name, throw=True)
	cheque = frappe.get_doc(CHEQUE, name)
	if error := printable_error(cheque):
		frappe.throw(error)
	layout = get_layout(cheque.bank_account)
	if not layout:
		frappe.throw(
			_("Bank Account {0} has no Cheque Layout. Create one first.").format(
				frappe.bold(cheque.bank_account)
			)
		)
	lang = lang_code(language, layout)
	leaf = build_leaf(
		layout,
		payee=payee_text(cheque),
		amount=cheque.amount,
		currency=cheque.currency,
		date=cheque_date(cheque),
		company=cheque.company,
		lang=lang,
	)
	texts = {key: escape_html(leaf[key] or "") for key in ("payee", "date", "figures", "words1", "words2")}
	texts["crossing"] = escape_html(leaf.crossing if leaf.print_crossing else "")
	return {**texts, "lang": lang, "dir": leaf.dir}


@frappe.whitelist()
def preview_layout(doc: str | dict, language: str | None = None) -> str:
	"""The test print of an unsaved Cheque Layout (the form's live preview). Never saves anything. Only for a
	bank account the user may see: the preview shows its company name and currency."""
	frappe.has_permission(LAYOUT, "read", throw=True)
	values = frappe.parse_json(doc)
	if not isinstance(values, dict) or values.get("doctype") != LAYOUT:
		frappe.throw(_("Invalid document."))
	layout = frappe.get_doc(values)
	check_bank_access(layout.bank_account)
	return str(render_test(layout, lang_code(language, layout)))


# ---------------------------------------------------------------------------
# Batch registration
# ---------------------------------------------------------------------------


def register_prints(
	names: list[str], language: str | None, reprint=0, reason: str | None = None, only_unprinted=0
) -> dict:
	"""Register the print of several cheques, all or nothing: every cheque is checked first; with
	`only_unprinted` the printed ones are skipped, otherwise one ChequeAlreadyPrintedError names them all.

	All the cheques printed share one print job (returned as `print_job`): the batch print renders exactly
	those, never a skipped one."""
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

	job = new_print_job()
	printed = []
	for cheque in cheques:
		if cheque.name not in skipped:
			lifecycle.record_print(cheque.name, language, reprint=reprint, reason=reason, job=job)
			printed.append(cheque.name)
	return {"printed": printed, "skipped": skipped, "print_job": job if printed else None}
