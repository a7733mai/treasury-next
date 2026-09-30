"""Istisna'a drawdowns (spec 7g §3.8): the financier pays the manufacturer / contractor in stages.

The drawdown's submit posts only its upfront fees; each stage is disbursed by the Disburse Stage action
(Treasury Manager): a voucher per stage (accounting.post_stage: Dr Bank or the disbursement account such as
Capital Work in Progress / Cr the Istisna'a liability, event `facility_stage_disbursement`, cycle = the stage
row), the stage marked Disbursed. Utilisation: the ledger (stages disbursed less the principal repaid) plus the
stages still pending (`get_commitment_utilisation`, hook `treasury_facility_utilisation_sources`): the whole
amount counts against the sub-limit from the submit on.
"""

import erpnext
import frappe
from frappe import _
from frappe.utils import flt, formatdate, getdate, today

from treasury.accounting import posting
from treasury.bank_facilities import accounting, instruments
from treasury.bank_facilities.utilisation import refresh_utilisation

DRAWDOWN = "Facility Drawdown"
STAGE = "Facility Disbursement Stage"
LIMIT_TYPE = "Istisna'a"
MANAGER = "Treasury Manager"
PENDING, DISBURSED = "Pending", "Disbursed"


def disburse_stage(
	drawdown: str, stage: str, disbursement_date=None, exchange_rate=None, remarks=None
) -> dict:
	"""Disburse one Pending stage of a submitted Istisna'a drawdown on `disbursement_date` (default today)."""
	if not drawdown or not frappe.db.get_value(DRAWDOWN, drawdown, "name", for_update=True):
		frappe.throw(_("{0} {1} does not exist").format(_(DRAWDOWN), drawdown), frappe.DoesNotExistError)
	doc = frappe.get_doc(DRAWDOWN, drawdown)
	doc.check_permission("write")
	if MANAGER not in frappe.get_roles():
		frappe.throw(
			_("This action requires one of the roles: {0}.").format(_(MANAGER)),
			frappe.PermissionError,
			title=_("Not Permitted"),
		)
	if doc.docstatus != 1 or doc.limit_type != LIMIT_TYPE:
		frappe.throw(_("Only a submitted Istisna'a drawdown is disbursed in stages."))
	row = next((r for r in doc.disbursement_stages if r.name == stage or str(r.idx) == str(stage)), None)
	if not row:
		frappe.throw(_("Stage {0} does not exist on drawdown {1}.").format(stage, frappe.bold(doc.name)))
	if row.status == DISBURSED:
		frappe.throw(_("Stage {0} is already disbursed.").format(row.idx))
	date = getdate(disbursement_date or today())
	disbursed = [getdate(r.disbursed_on) for r in doc.disbursement_stages if r.disbursed_on]
	latest = max([getdate(doc.drawdown_date), *disbursed])
	if date < latest:
		frappe.throw(_("The date ({0}) cannot be before {1}.").format(formatdate(date), formatdate(latest)))
	facility = frappe.get_doc("Bank Facility", doc.facility)
	instruments.validate_facility_open(facility, date, _(LIMIT_TYPE, context="Bank Facility"))
	if flt(exchange_rate) < 0:
		frappe.throw(_("Exchange Rate cannot be negative."))
	if doc.currency == erpnext.get_company_currency(doc.company):
		# never a client rate on a company currency stage (it would multiply the liability and the bank)
		rate = 1.0
	else:
		rate = flt(exchange_rate) or posting.get_cheque_exchange_rate(doc.currency, doc.company, date)

	voucher_type, voucher_no = accounting.post_stage(doc, row, date, rate)
	frappe.db.set_value(
		STAGE,
		row.name,
		{"status": DISBURSED, "disbursed_on": date, "voucher_type": voucher_type, "voucher_no": voucher_no},
		update_modified=False,
	)
	disbursed_amount = flt(
		sum(flt(r.amount) for r in doc.disbursement_stages if r.status == DISBURSED or r.name == row.name),
		doc.precision("amount"),
	)
	doc.db_set("disbursed_amount", disbursed_amount, update_modified=False)
	if remarks:
		doc.add_comment("Info", _("Stage {0} disbursed: {1}").format(row.idx, remarks))
	refresh_utilisation(doc.facility)
	return {
		"name": doc.name,
		"stage": row.idx,
		"voucher_type": voucher_type,
		"voucher_no": voucher_no,
		"disbursed_amount": disbursed_amount,
	}


def get_commitment_utilisation(facility, as_on=None) -> list[dict]:
	"""Hook `treasury_facility_utilisation_sources`: the Istisna'a stages of the facility's submitted drawdowns
	not disbursed yet (on `as_on`: not disbursed by then, of the drawdowns dated up to it)."""
	dd = frappe.qb.DocType(DRAWDOWN)
	stage = frappe.qb.DocType(STAGE)
	query = (
		frappe.qb.from_(stage)
		.join(dd)
		.on((dd.name == stage.parent) & (stage.parenttype == DRAWDOWN))
		.select(stage.amount, stage.status, stage.disbursed_on)
		.where((dd.facility == facility.name) & (dd.docstatus == 1) & (dd.limit_type == LIMIT_TYPE))
	)
	if as_on:
		query = query.where(dd.drawdown_date <= getdate(as_on))
	amount = 0.0
	for row in query.run(as_dict=True):
		pending = row.status != DISBURSED
		if as_on and not pending:
			pending = not row.disbursed_on or getdate(row.disbursed_on) > getdate(as_on)
		if pending:
			amount += flt(row.amount)
	return [{"limit_type": LIMIT_TYPE, "amount": amount}] if amount else []
