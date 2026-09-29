"""Small helpers for the Treasury print formats (exposed through hooks.py `jinja`)."""

import frappe
from frappe.utils import formatdate, nowdate

RTL_LANGUAGES = ("ar", "he", "fa", "ps", "ur")


def treasury_print_direction() -> str:
	"""`rtl` or `ltr` for the current (print) language."""
	lang = (getattr(frappe.local, "lang", None) or "en").split("-")[0]
	return "rtl" if lang in RTL_LANGUAGES else "ltr"


def treasury_print_date() -> str:
	"""Today's date, formatted: the date the document is printed on (never stored)."""
	return formatdate(nowdate())


def treasury_batch_receipt(doc) -> dict:
	"""The cheques of a Cheque Batch to print on its receipt, and their total (see
	`treasury.pdc.batch.get_receipt_cheques`). The print permission on the batch was checked by the print."""
	from treasury.pdc.batch import get_receipt_cheques

	if getattr(doc, "doctype", None) != "Cheque Batch":
		return {"cheques": [], "total": 0, "count": 0}
	return get_receipt_cheques(doc)


def treasury_payment_entries(doc) -> list[dict]:
	"""The submitted Payment Entries of the current cycle of a PDC Cheque, for its receipt: the numbers the
	paper is filed under. [{name, posting_date, event_label}]; [] for any other document, and for a cheque the
	user cannot read (any Jinja template may call this helper)."""
	from treasury.utils.voucher_summary import payment_entries

	if getattr(doc, "doctype", None) != "PDC Cheque" or not getattr(doc, "name", None):
		return []
	if not frappe.has_permission("PDC Cheque", "read", doc=doc.name):
		return []
	return [
		{"name": pe["name"], "posting_date": pe["posting_date"], "event_label": pe["event_label"]}
		for pe in payment_entries("PDC Cheque", [doc.name], cycle=doc.get("cycle") or 1)
	]
