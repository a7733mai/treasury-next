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
