"""Party payment rules of a custodian (hook `treasury_party_payment_hooks`, see posting.get_party_rules).

Custody accepts a custodian only on a Pay / Receive Payment Entry of a Custody Request, on the request's
custody account, without references or deductions (F1); it refuses any Journal Entry row of a custodian (F3),
so such a Payment Entry is undone only by cancelling it (F3, F4).
"""

import frappe
from frappe import _

from treasury.integrations.custody import PARTY_TYPE, REQUEST, documents, is_installed


def party_rules(ctx) -> dict | None:
	if ctx.party_type != PARTY_TYPE or not is_installed():
		return None
	base = {
		"allow_references": False,
		"allow_deductions": False,
		"undo_by_cancel": True,
		"allow_journal_entry": False,
	}
	if ctx.action != "payment":
		return base
	request = documents.request_of(ctx.source_doctype, ctx.source_name)
	if not request:
		frappe.throw(
			_(
				"A payment to custodian {0} must be made from its Custody Request (Treasury buttons on the request)."
			).format(frappe.bold(ctx.party))
		)
	req = frappe.db.get_value(REQUEST, request, ["custodian", "company", "custody_account"], as_dict=True)
	if not req or req.custodian != ctx.party or req.company != ctx.company:
		frappe.throw(
			_("Custody Request {0} belongs to another custodian or company.").format(frappe.bold(request))
		)
	# `custody_purpose` is not set: custody's validate always derives it (F1)
	return {**base, "party_account": req.custody_account, "fields": {"custody_request": request}}
