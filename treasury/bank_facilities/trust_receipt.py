"""Trust Receipts (spec 7g §3.9): the bank finances the settlement of a Letter of Credit.

With "Finance by Trust Receipt", the LC settlement (treasury.bank_facilities.lc.settle) pays the supplier as
usual (the margin held first, then the bank), and the bank lends the rest: a Facility Drawdown of the facility's
Trust Receipt sub-limit is created and submitted in the same transaction, for the settled amount less the margin
held, dated the settlement date, on the LC's bank account (Dr Bank / Cr Trust Receipt liability: the bank's net
movement of the settlement is nil). Its schedule is one bullet installment after the tenor (default: the
sub-limit's Default Tenor), at the sub-limit's rate. The drawdown's own limit check applies; a failure rolls the
whole settlement back.
"""

import frappe
from frappe import _
from frappe.utils import add_months, cint, flt, getdate

DRAWDOWN = "Facility Drawdown"
LIMIT_TYPE = "Trust Receipt"


def get_limit(facility: str):
	doc = frappe.get_doc("Bank Facility", facility)
	return next((row for row in doc.limits if row.limit_type == LIMIT_TYPE), None)


def validate_financing(lc, amount: float, tenor_months=None) -> tuple[float, int]:
	"""(amount financed, tenor) of a settlement financed by Trust Receipt; throws when it cannot be."""
	precision = lc.precision("amount")
	tr_amount = flt(flt(amount) - flt(lc.margin_held), precision)
	if tr_amount <= 0:
		frappe.throw(_("The cash margin covers the settlement: nothing to finance by Trust Receipt."))
	limit = get_limit(lc.facility)
	if not limit:
		frappe.throw(_("Bank Facility {0} has no Trust Receipt sub-limit.").format(frappe.bold(lc.facility)))
	tenor = cint(tenor_months) or cint(limit.default_tenor_months)
	if tenor <= 0:
		frappe.throw(_("Enter the Trust Receipt tenor."))
	return tr_amount, tenor


def create_from_lc(
	lc, date, amount, rate, tenor_months, first_repayment_date=None, interest_rate=None
) -> str:
	"""Insert and submit the Trust Receipt drawdown of a settled Letter of Credit (the user's own permissions:
	the settlement is a Treasury Manager action). Returns its name."""
	limit = get_limit(lc.facility)
	date = getdate(date)
	doc = frappe.get_doc(
		{
			"doctype": DRAWDOWN,
			"facility": lc.facility,
			"limit_type": LIMIT_TYPE,
			"drawdown_date": date,
			"amount": flt(amount),
			"exchange_rate": flt(rate) or 1,
			"bank_account": lc.bank_account,
			"tenor_months": cint(tenor_months),
			"repayment_frequency": "Bullet",
			"repayment_method": "Bullet",
			"first_repayment_date": getdate(first_repayment_date)
			if first_repayment_date
			else getdate(add_months(date, cint(tenor_months))),
			"interest_rate": flt(interest_rate) if interest_rate not in (None, "") else flt(limit.interest_rate),
			"day_count": limit.day_count,
			"repayment_mode": "Manual",
			"letter_of_credit": lc.name,
			"remarks": _("Trust Receipt of {0} {1}").format(_("Letter of Credit"), lc.name),
		}
	)
	doc.flags.treasury_trust_receipt = True
	doc.insert()
	doc.submit()
	return doc.name
