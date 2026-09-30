"""The "Payment Entries and Vouchers" summary of a Treasury document, rendered on the server.

Every Treasury document (PDC Cheque, Letter of Credit / Guarantee, Facility Drawdown / Repayment, Cheque
Discounting, Security Cheque, Bank Transfer, Bank Charge) shows the Payment Entries and Journal Entries the posting service created for it
(`treasury.accounting.posting.get_voucher_rows`): Payment Entries first, the paper-filing key, then Journal
Entries, each oldest first. The form script only places the returned HTML (`__onload.voucher_summary`); every
value is escaped by the template (`|e`), labels are translated here.

`payment_entries` gives the submitted Payment Entries as data, for the PDC Cheque headline and the prints.
"""

from urllib.parse import quote

import erpnext
import frappe
from frappe import _
from frappe.utils import cint, fmt_money, formatdate

from treasury.accounting import posting

TEMPLATE = "treasury/templates/includes/treasury_vouchers.html"
# Translation context of the summary's own words (column headers, statuses, events), so their Arabic entries
# never change Frappe / ERPNext's wording of the same English words elsewhere.
VOUCHER_CTX = "Treasury Voucher"
PAYMENT_ENTRY = "Payment Entry"
REVERSAL_SUFFIX = "_reversal"
ACCRUAL_PREFIX = "interest_accrual:"

EVENT_LABELS = {
	"pdc_party_posting": "Party Posting",
	"pdc_under_collection": "Under Collection",
	"pdc_bank_posting": "Bank Settlement",
	"pdc_bank_charges": "Bank Charges",
	"pdc_bounce_charges": "Bounce Charges",
	"pdc_discount_settlement": "Discount Settlement",
	"pdc_discount_recourse": "Discount Recourse",
	"facility_drawdown": "Drawdown",
	"facility_upfront_fees": "Upfront Fees",
	"facility_repayment": "Repayment",
	"cheque_discounting": "Discounting",
	"lg_margin": "Cash Margin",
	"lg_commission": "Commission",
	"lg_extension_commission": "Extension Commission",
	"lg_margin_release": "Margin Release",
	"lg_claim": "Claim",
	"lg_claim_repayment": "Claim Repayment",
	"lc_margin": "Cash Margin",
	"lc_commission": "Commission",
	"lc_margin_topup": "Margin Top-up",
	"lc_margin_release": "Margin Release",
	"lc_amendment_charges": "Amendment Charges",
	"lc_document_charges": "Document Charges",
	"lc_settlement": "Settlement",
	"bt_transfer": "Transfer",
	"bank_charge": "Bank Charge",
}

# docstatus -> (label, indicator colour): Treasury always submits, so a draft is an interrupted posting.
STATUSES = {0: ("Draft", "orange"), 1: ("Submitted", "blue"), 2: ("Cancelled", "red")}


def event_label(event: str | None) -> str:
	"""The translated label of a `treasury_event` ("Reversal of …" for a reversal, the raw event if unknown)."""
	event = event or ""
	if event.endswith(REVERSAL_SUFFIX):
		return _("Reversal of {0}").format(event_label(event[: -len(REVERSAL_SUFFIX)]))
	if event.startswith(ACCRUAL_PREFIX):
		return _("Interest Accrual {0}", context=VOUCHER_CTX).format(event[len(ACCRUAL_PREFIX) :])
	label = EVENT_LABELS.get(event)
	return _(label, context=VOUCHER_CTX) if label else event


def render(source_doctype: str, source_names: list[str], cycle: int | None = None) -> str:
	"""HTML table of the vouchers of `source_names` (documents of `source_doctype`).

	`cycle`: the current cycle of a PDC Cheque; rows of earlier cycles are dimmed and, when the cheque has
	several cycles, every event names its cycle. The caller has read permission on the source document."""
	return frappe.render_template(TEMPLATE, {"rows": summary_rows(source_doctype, source_names, cycle)})


def render_cheques(cheque_names: list[str]) -> str:
	"""One summary per PDC Cheque (e.g. the cheque a Security Cheque was encashed into and its replacements),
	each under a heading naming the cheque."""
	sections = []
	for name in cheque_names:
		cycle = frappe.db.get_value("PDC Cheque", name, "cycle")
		sections.append({"cheque": name, "rows": summary_rows("PDC Cheque", [name], cint(cycle) or 1)})
	return frappe.render_template(TEMPLATE, {"sections": sections})


def summary_rows(source_doctype: str, source_names: list[str], cycle: int | None = None) -> list[dict]:
	"""The display values of each voucher (Payment Entries first, then Journal Entries, oldest first)."""
	vouchers = posting.get_voucher_rows(source_doctype, list(source_names or []))
	vouchers.sort(key=lambda v: v.doctype != PAYMENT_ENTRY)  # stable: keeps the creation order
	several_cycles = cycle is not None and len({cint(v.treasury_cycle) or 1 for v in vouchers}) > 1
	rows = []
	for voucher in vouchers:
		voucher_cycle = cint(voucher.treasury_cycle) or 1
		status, colour = STATUSES.get(voucher.docstatus, STATUSES[0])
		kind = _(voucher.doctype, context=VOUCHER_CTX)
		if voucher.payment_type:
			kind = f"{kind} · {_(voucher.payment_type, context=VOUCHER_CTX)}"
		currency = voucher.currency or erpnext.get_company_currency(voucher.company)
		rows.append(
			{
				"doctype": voucher.doctype,
				"name": voucher.name,
				"route": f"/app/{frappe.scrub(voucher.doctype).replace('_', '-')}/{quote(voucher.name)}",
				"kind": kind,
				"event": event_label(voucher.treasury_event),
				"cycle": _("Cycle {0}").format(voucher_cycle) if several_cycles else "",
				"posting_date": formatdate(voucher.posting_date),
				"mode_of_payment": voucher.mode_of_payment or "",
				"amount": fmt_money(voucher.amount, currency=currency),
				"clearance_date": formatdate(voucher.clearance_date) if voucher.clearance_date else "",
				"status": _(status, context=VOUCHER_CTX),
				"status_colour": colour,
				"muted": cycle is not None and voucher_cycle < cint(cycle),
				"cancelled": voucher.docstatus == 2,
			}
		)
	return rows


def payment_entries(source_doctype: str, source_names: list[str], cycle: int | None = None) -> list[dict]:
	"""The submitted Payment Entries of the sources (of `cycle` only, when given), oldest first:
	[{name, posting_date, clearance_date, event, event_label}]."""
	return [
		{
			"name": v.name,
			"posting_date": v.posting_date,
			"clearance_date": v.clearance_date,
			"event": v.treasury_event,
			"event_label": event_label(v.treasury_event),
		}
		for v in posting.get_voucher_rows(source_doctype, list(source_names or []))
		if v.doctype == PAYMENT_ENTRY
		and v.docstatus == 1
		and (cycle is None or (cint(v.treasury_cycle) or 1) == cint(cycle))
	]


def cheque_chain(first: str | None) -> list[str]:
	"""`first` and the cheques that replace it, in order (PDC Cheque `replaced_by`)."""
	chain = []
	name = first
	while name and name not in chain:
		chain.append(name)
		name = frappe.db.get_value("PDC Cheque", name, "replaced_by")
	return chain
