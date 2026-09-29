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


def treasury_party_account_no(doc) -> str | None:
	"""The account number of a Bank Transfer's beneficiary / payer bank account, for its print: only for a user
	who may read the transfer and the party (the number is not stored on the transfer; any Jinja template may
	call this helper)."""
	from treasury.utils.party import can_read_party

	if getattr(doc, "doctype", None) != "Bank Transfer" or not doc.get("party_bank_account"):
		return None
	if not (doc.get("name") and frappe.has_permission("Bank Transfer", "read", doc=doc.name)):
		return None
	if not can_read_party(doc.get("party_type"), doc.get("party")):
		return None
	account = frappe.db.get_value(
		"Bank Account", doc.party_bank_account, ["party_type", "party", "bank_account_no"], as_dict=True
	)
	if not account or (account.party_type, account.party) != (doc.party_type, doc.party):
		return None
	return account.bank_account_no


def treasury_payment_entries(doc) -> list[dict]:
	"""The submitted Payment Entries of a PDC Cheque (its current cycle) or a Bank Transfer, for its print: the
	numbers the paper is filed under. [{name, posting_date, event_label}]; [] for any other document, and for
	a document the user cannot read (any Jinja template may call this helper)."""
	from treasury.utils.voucher_summary import payment_entries

	doctype = getattr(doc, "doctype", None)
	if doctype not in ("PDC Cheque", "Bank Transfer") or not getattr(doc, "name", None):
		return []
	if not frappe.has_permission(doctype, "read", doc=doc.name):
		return []
	cycle = (doc.get("cycle") or 1) if doctype == "PDC Cheque" else None
	return [
		{"name": pe["name"], "posting_date": pe["posting_date"], "event_label": pe["event_label"]}
		for pe in payment_entries(doctype, [doc.name], cycle=cycle)
	]
