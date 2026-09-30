# Copyright (c) 2026, Ahmed Abdelmageed and contributors
# For license information, please see license.txt

"""Cost of Financing (PLAN.md §8, spec 7g §4.9): interest, profit, Ijarah rentals, commission, fees, penalties
and late payment charity per facility and period.

Read from the GL (company currency): the expense rows of the Payment Entries and Journal Entries (one query per
voucher doctype) linked to a Facility Drawdown, a Facility Repayment or an installment cheque of a drawdown
(`treasury_source_*`; a cheque's party posting and its reversal: events `pdc_party_posting` /
`pdc_party_posting_reversal`, which net to 0 after a bounce), dated in [From Date, To Date], cancelled ones
excluded. A row is, in this order:
- Fees: the upfront fees voucher of a drawdown, or its fees account on a drawdown voucher;
- Commission: the facility's Commission Expense Account;
- Late Payment (Charity): the charity Journal Entry, or the facility's Late Payment (Charity) Account;
- Penalty: the Penalty Account of a repayment;
- Ijarah Rentals (Profit): any other row of an Ijarah drawdown;
- Profit: any other row of an Islamic facility;
- Interest: every other expense row (the interest accruals and the interest paid by repayments).
Rows on the company's Exchange Gain / Loss account (rounding of foreign currency vouchers) are not a cost of
financing and are left out. Overdraft interest charged on the bank statement is not linked to a facility and
is not included; neither are the charges of ERPNext's Invoice Discounting (not posted by Treasury).

Tree: Facility (its totals) → one row per period (Monthly / Quarterly / Yearly).
"""

import frappe
from frappe import _
from frappe.query_builder.functions import Sum
from frappe.utils import add_months, flt, formatdate, get_last_day, getdate

from treasury.bank_facilities import accounting
from treasury.bank_facilities.report_utils import DRAWDOWN, FACILITY, get_facilities, report_today
from treasury.pdc.report_utils import CTX, column, get_precision, validate_company, validate_date_range

REPAYMENT = "Facility Repayment"
CHEQUE = "PDC Cheque"
CHEQUE_EVENTS = ("pdc_party_posting", "pdc_party_posting_reversal")
MONTHLY, QUARTERLY, YEARLY = "Monthly", "Quarterly", "Yearly"
PERIOD_MONTHS = {MONTHLY: 1, QUARTERLY: 3, YEARLY: 12}
CATEGORIES = ("interest", "profit", "ijarah", "commission", "fees", "penalty", "charity")
VOUCHER_DOCTYPES = ("Payment Entry", "Journal Entry")


def execute(filters=None):
	filters = frappe._dict(filters or {})
	company = validate_company(filters.company, FACILITY)
	today = report_today()
	from_date = getdate(filters.from_date or today.replace(month=1, day=1))
	to_date = getdate(filters.to_date or today)
	validate_date_range(from_date, to_date)
	periodicity = filters.periodicity or MONTHLY
	if periodicity not in PERIOD_MONTHS:
		frappe.throw(
			_("Periodicity must be one of: {0}.").format(", ".join(_(p, context=CTX) for p in PERIOD_MONTHS))
		)

	conditions = {"company": company}
	for fieldname, value in (
		("bank", filters.bank),
		("name", filters.facility),
		("lender_type", filters.lender_type),
	):
		if value:
			conditions[fieldname] = value
	facilities = {
		f.name: f
		for f in get_facilities(
			conditions,
			fields=[
				"name",
				"facility_name",
				"bank",
				"lender_name",
				"currency",
				"is_islamic",
				"commission_expense_account",
				"late_payment_charity_account",
			],
		)
	}
	costs = get_costs(company, facilities, from_date, to_date)

	precision = get_precision()
	data, totals = [], dict.fromkeys(CATEGORIES, 0.0)
	company_currency = frappe.get_cached_value("Company", company, "default_currency")
	for name in sorted(facilities):
		by_period: dict = {}
		for row in costs.get(name, []):
			start = period_start(row.posting_date, periodicity)
			bucket = by_period.setdefault(start, dict.fromkeys(CATEGORIES, 0.0))
			bucket[row.category] += row.amount
		if not by_period:
			continue
		facility = facilities[name]
		lender = facility.lender_name or facility.bank
		parent = frappe._dict(
			row_id=name,
			indent=0,
			label=facility.facility_name,
			facility=name,
			bank=facility.bank,
			lender_name=lender,
			currency=company_currency,
			**dict.fromkeys(CATEGORIES, 0.0),
		)
		children = []
		for start in sorted(by_period):
			end = getdate(get_last_day(add_months(start, PERIOD_MONTHS[periodicity] - 1)))
			child = frappe._dict(
				row_id=f"{name}-{start}",
				parent_row_id=name,
				indent=1,
				label=f"{formatdate(start)} - {formatdate(end)}",
				period_start=start,
				period_end=end,
				facility=name,
				bank=facility.bank,
				lender_name=lender,
				currency=company_currency,
			)
			for category in CATEGORIES:
				child[category] = flt(by_period[start][category], precision)
				parent[category] = flt(parent[category] + child[category], precision)
			child.total = flt(sum(child[c] for c in CATEGORIES), precision)
			children.append(child)
		parent.total = flt(sum(parent[c] for c in CATEGORIES), precision)
		for category in CATEGORIES:
			totals[category] = flt(totals[category] + parent[category], precision)
		data += [parent, *children]
	return get_columns(), data, None, None, get_summary(totals, company_currency)


def period_start(date, periodicity: str):
	date = getdate(date)
	months = PERIOD_MONTHS[periodicity]
	month = ((date.month - 1) // months) * months + 1
	return date.replace(month=month, day=1)


def get_costs(company: str, facilities: dict, from_date, to_date) -> dict[str, list[frappe._dict]]:
	"""{facility: [{posting_date, category, amount}]} from the GL (see module docstring)."""
	if not facilities:
		return {}
	drawdowns = {
		d.name: d
		for d in frappe.get_all(
			DRAWDOWN,
			filters={"facility": ["in", list(facilities)], "docstatus": ["!=", 0]},
			fields=["name", "facility", "limit_type", "fees_account"],
		)
	}
	if not drawdowns:
		return {}
	repayments = {
		r.name: r
		for r in frappe.get_all(
			REPAYMENT,
			filters={"drawdown": ["in", list(drawdowns)], "docstatus": ["!=", 0]},
			fields=["name", "drawdown", "penalty_account"],
		)
	}
	cheques = {
		c.name: c.facility_drawdown
		for c in frappe.get_all(
			CHEQUE,
			filters={"facility_drawdown": ["in", list(drawdowns)]},
			fields=["name", "facility_drawdown"],
		)
	}
	# (source doctype, source name) -> drawdown
	sources = {(DRAWDOWN, name): name for name in drawdowns}
	sources.update({(REPAYMENT, name): r.drawdown for name, r in repayments.items()})
	sources.update({(CHEQUE, name): drawdown for name, drawdown in cheques.items()})

	exchange_account = frappe.get_cached_value("Company", company, "exchange_gain_loss_account")
	costs: dict[str, list] = {}
	for row in _expense_rows(company, sources, from_date, to_date):
		key = (row.treasury_source_doctype, row.treasury_source_name)
		drawdown = drawdowns.get(sources.get(key))
		if not drawdown or row.account == exchange_account or not flt(row.amount):
			continue
		if row.treasury_source_doctype == CHEQUE and row.treasury_event not in CHEQUE_EVENTS:
			continue
		facility = facilities[drawdown.facility]
		category = _category(row, facility, drawdown, repayments)
		costs.setdefault(drawdown.facility, []).append(
			frappe._dict(posting_date=getdate(row.posting_date), category=category, amount=flt(row.amount))
		)
	return costs


def _expense_rows(company: str, sources: dict, from_date, to_date) -> list[frappe._dict]:
	"""The net Expense GL rows per (voucher source, event, account, date) of the sources' vouchers."""
	gle = frappe.qb.DocType("GL Entry")
	account = frappe.qb.DocType("Account")
	names = sorted({s[1] for s in sources})
	rows = []
	for voucher_type in VOUCHER_DOCTYPES:
		voucher = frappe.qb.DocType(voucher_type)
		query = (
			frappe.qb.from_(gle)
			.join(voucher)
			.on(voucher.name == gle.voucher_no)
			.join(account)
			.on(account.name == gle.account)
			.select(
				voucher.treasury_source_doctype,
				voucher.treasury_source_name,
				voucher.treasury_event,
				gle.account,
				gle.posting_date,
				Sum(gle.debit - gle.credit).as_("amount"),
			)
			.where(
				(gle.company == company)
				& (gle.voucher_type == voucher_type)
				& (gle.is_cancelled == 0)
				& (account.root_type == "Expense")
				& (gle.posting_date >= getdate(from_date))
				& (gle.posting_date <= getdate(to_date))
				& (voucher.treasury_source_doctype.isin([DRAWDOWN, REPAYMENT, CHEQUE]))
				& (voucher.treasury_source_name.isin(names))
			)
			.groupby(
				voucher.treasury_source_doctype,
				voucher.treasury_source_name,
				voucher.treasury_event,
				gle.account,
				gle.posting_date,
			)
		)
		rows += query.run(as_dict=True)
	return rows


def _category(row, facility, drawdown, repayments: dict) -> str:
	"""The cost category of an expense row (spec 7g §4.9, first matching rule)."""
	if row.treasury_event == accounting.EVENT_FEES or (
		row.treasury_source_doctype == DRAWDOWN and drawdown.fees_account and row.account == drawdown.fees_account
	):
		return "fees"
	if facility.commission_expense_account and row.account == facility.commission_expense_account:
		return "commission"
	if row.treasury_event == accounting.EVENT_CHARITY or (
		facility.late_payment_charity_account and row.account == facility.late_payment_charity_account
	):
		return "charity"
	repayment = repayments.get(row.treasury_source_name) if row.treasury_source_doctype == REPAYMENT else None
	if repayment and repayment.penalty_account and row.account == repayment.penalty_account:
		return "penalty"
	if drawdown.limit_type == "Ijarah":
		return "ijarah"
	if facility.is_islamic:
		return "profit"
	return "interest"


def get_summary(totals: dict, currency: str) -> list[dict]:
	precision = get_precision()

	def card(value, label, indicator):
		return {
			"label": label,
			"value": flt(value, precision),
			"datatype": "Currency",
			"currency": currency,
			"indicator": indicator,
		}

	return [
		card(totals["interest"] + totals["profit"] + totals["ijarah"], _("Interest / Profit"), "Red"),
		card(totals["commission"], _("Commission"), "Orange"),
		card(totals["fees"], _("Fees"), "Orange"),
		card(totals["penalty"] + totals["charity"], _("Penalties"), "Red"),
		card(sum(totals.values()), _("Total Cost of Financing"), "Blue"),
	]


def get_columns() -> list[dict]:
	def money(fieldname, label, width=130):
		return column(fieldname, label, "Currency", width, options="currency")

	return [
		column("label", _("Facility / Period"), width=240),
		column("facility", _("Bank Facility"), "Link", 150, options=FACILITY),
		column("lender_name", _("Lender"), width=150),
		column("bank", _("Bank", context="Treasury"), "Link", 130, options="Bank", hidden=1),
		money("interest", _("Interest")),
		money("profit", _("Profit")),
		money("ijarah", _("Ijarah Rentals (Profit)"), 150),
		money("commission", _("Commission")),
		money("fees", _("Fees")),
		money("penalty", _("Penalties")),
		money("charity", _("Late Payment (Charity)"), 150),
		money("total", _("Total", context=CTX)),
		column("currency", _("Currency"), "Link", 70, options="Currency", hidden=1),
	]
