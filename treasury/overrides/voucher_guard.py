"""Protect Payment Entries / Journal Entries created by Treasury from direct cancellation or deletion.

Wired through `doc_events` only; the vouchers must be cancelled from their Treasury source document,
which calls `treasury.accounting.posting.cancel_voucher` (sets `frappe.flags.treasury_allow_cancel`).
"""

import frappe
from frappe import _


def is_managed(doc) -> bool:
	return bool(doc.get("treasury_source_doctype") and doc.get("treasury_source_name"))


def throw_managed(doc):
	frappe.throw(
		_("This voucher is managed by {0} {1}. Cancel it from there.").format(
			_(doc.treasury_source_doctype), frappe.bold(doc.treasury_source_name)
		),
		title=_("Not Allowed"),
	)


def before_cancel(doc, method=None):
	if is_managed(doc) and not frappe.flags.treasury_allow_cancel:
		throw_managed(doc)


def on_cancel(doc, method=None):
	"""After ERPNext's own `on_cancel` (which sets the voucher's `ignore_linked_doctypes`, and before Frappe's
	back-link check): the Treasury doctypes `posting.cancel_voucher` was told to ignore."""
	extra = doc.flags.get("treasury_ignore_linked_doctypes")
	if extra and frappe.flags.treasury_allow_cancel and is_managed(doc):
		current = doc.get("ignore_linked_doctypes") or ()
		if isinstance(current, str):
			current = (current,)
		doc.ignore_linked_doctypes = (*current, *extra)


def on_trash(doc, method=None):
	if doc.docstatus > 0 and is_managed(doc) and not frappe.flags.treasury_allow_cancel:
		throw_managed(doc)
