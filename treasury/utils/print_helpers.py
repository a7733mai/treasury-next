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
