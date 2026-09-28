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


def on_trash(doc, method=None):
	if doc.docstatus > 0 and is_managed(doc) and not frappe.flags.treasury_allow_cancel:
		throw_managed(doc)
