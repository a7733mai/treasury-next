"""Treasury Dashboard data (spec 7h §3.1), read only: posts nothing.

`get_overview` gathers, for one company (optionally some of its bank accounts) and a horizon of 7, 14 or 30
days, the figures of the Treasury reports and number cards through their own code, so every KPI equals the
report it drills down to:

- positions: one Bank Position Forecast engine call of 30 days (`bank_positions.collect_positions`; the 7- and
  14-day positions are its first days, F9) with the Balance Thresholds (Bank Balance Outlook);
- cheques: the number-card totals (`pdc.dashboard.open_cheque_totals`) and this month's bounces (Bounced and
  Postponed Cheques);
- transfers awaiting the bank's confirmation, this month's bank charges (Bank Charges Analysis);
- company-wide (not per bank account): security cheques (Security Exposure), facilities (Facility
  Utilisation, at most MAX_FACILITIES), installments (the facility cards), expiring Letters of Guarantee /
  Credit (LG / LC Register).

A section is left out (and listed in `hidden`) when the user may not read its DocType for the company, or when
it raises a PermissionError (e.g. a User Permission on a Bank Account). Amounts in the company currency are
converted at today's rate; per-bank figures stay in the bank currency.
"""

import erpnext
import frappe
from frappe import _
from frappe.utils import add_days, flt, formatdate, get_first_day, getdate
from frappe.utils.messages import clear_last_message

from treasury.accounting import posting
from treasury.balance_alerts.positions import (
	CRITICAL,
	WARNING,
	apply_thresholds,
	get_thresholds,
	level_order,
	with_gl_siblings,
)
from treasury.pdc.bank_positions import collect_positions
from treasury.pdc.report.bank_balance_outlook.bank_balance_outlook import LEVEL_CTX
from treasury.pdc.report.bank_position_forecast.bank_position_forecast import (
	BANK_CURRENCY,
	COMPANY_CURRENCY,
	DAILY,
	IN,
	WEEKLY,
	build_periods,
)
from treasury.pdc.report_utils import (
	AmountConverter,
	as_list,
	get_bank_accounts,
	get_precision,
	is_permitted_value,
	report_today,
)

HORIZONS = (7, 14, 30)
DEFAULT_HORIZON = 7
PROJECTION_DAY = 7  # "projected in 7 days" = the closing of today + 6
MATURITY_DAYS = 30
EXPIRY_WINDOW_DAYS = 30
MAX_FACILITIES = 20
NEXT_INSTALLMENTS = 5
NEXT_INSTRUMENTS = 5
MAX_DASHBOARD_QUERIES = 200  # test ceiling (warm cache), spec 7h §7 C
CTX = "Treasury Dashboard"
SECTIONS = {  # section key -> DocType whose read permission (and Company user permission) gates it
	"positions": "PDC Cheque",
	"cheques": "PDC Cheque",
	"transfers": "Bank Transfer",
	"charges": "Bank Charge",
	"security": "Security Cheque",
	"facilities": "Bank Facility",
	"installments": "Facility Drawdown",
	"guarantees": "Letter of Guarantee",
	"credits": "Letter of Credit",
}
SETTINGS = "Treasury Company Settings"
OUTLOOK = ["query-report", "Bank Balance Outlook"]
FORECAST = ["query-report", "Bank Position Forecast"]
# maturities chart: flow category by source document (PDC Cheque: by direction)
MATURITY_CATEGORIES = ("Cheques In", "Cheques Out", "Installments", "Transfers", "Letters of Credit", "Other")
CATEGORY_BY_DOCTYPE = {
	"Facility Drawdown": "Installments",
	"Bank Transfer": "Transfers",
	"Letter of Credit": "Letters of Credit",
}


def readable_sections(company: str) -> list[str]:
	"""The section keys whose DocType the user may read, for `company` (User Permissions on Company)."""
	allowed: dict[str, bool] = {}
	for doctype in dict.fromkeys(SECTIONS.values()):
		allowed[doctype] = bool(frappe.has_permission(doctype, "read")) and is_permitted_value(
			"Company", company, doctype
		)
	return [key for key, doctype in SECTIONS.items() if allowed[doctype]]


def get_overview(company, bank_accounts=None, horizon=DEFAULT_HORIZON, today=None) -> frappe._dict:
	if not company:
		frappe.throw(_("Company is required."))
	if not frappe.db.exists("Company", company):
		frappe.throw(_("{0} {1} does not exist").format(_("Company"), company), frappe.DoesNotExistError)
	if horizon not in HORIZONS:
		frappe.throw(_("Horizon must be 7, 14 or 30 days."))
	sections = readable_sections(company)
	if not sections:
		frappe.throw(
			_("You are not permitted to view the Treasury Dashboard of company {0}.").format(
				frappe.bold(company)
			),
			frappe.PermissionError,
			title=_("Not Permitted"),
		)
	names = list(dict.fromkeys(as_list(bank_accounts)))
	banks = None
	if names:
		get_bank_accounts(company, names)  # another company's bank or a forbidden one raises
		banks = with_gl_siblings(company, names)

	ctx = frappe._dict(
		company=company,
		banks=banks,
		horizon=horizon,
		today=getdate(today or report_today()),
		company_currency=erpnext.get_company_currency(company),
		precision=get_precision(),
		rates={},
	)
	ctx.month_start = getdate(get_first_day(ctx.today))
	ctx.rate = lambda currency: _rate(ctx, currency)

	result = frappe._dict(
		company=company,
		company_currency=ctx.company_currency,
		today=str(ctx.today),
		horizon=horizon,
		bank_accounts=banks or [],
		sections=frappe._dict(),
		hidden=[],
		setup=get_setup(company),
	)
	for key, build in SECTION_BUILDERS.items():
		if key not in sections:
			result.hidden.append(key)
			continue
		try:
			result.sections[key] = build(ctx)
		except frappe.PermissionError:
			clear_last_message()
			result.hidden.append(key)
	return result


def _rate(ctx, currency: str | None) -> float:
	if not currency or currency == ctx.company_currency:
		return 1.0
	if currency not in ctx.rates:
		ctx.rates[currency] = flt(posting.get_cheque_exchange_rate(currency, ctx.company, ctx.today))
	return ctx.rates[currency]


def get_setup(company: str) -> frappe._dict | None:
	"""The Setup Checklist summary (treasury.treasury_setup.checklist), only for a user who may read the
	company's settings."""
	from treasury.treasury_setup import checklist

	if frappe.db.exists(SETTINGS, company):
		allowed = frappe.has_permission(SETTINGS, "read", doc=company)
	else:
		allowed = frappe.has_permission(SETTINGS, "read")
	return checklist.get_status(company) if allowed else None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def one_bank(ctx) -> dict:
	"""{"bank_account": bank} when exactly one bank is selected (the list reports take a single bank)."""
	return {"bank_account": ctx.banks[0]} if ctx.banks and len(ctx.banks) == 1 else {}


def bank_list(ctx) -> dict:
	"""{"bank_account": [banks]} for the reports taking several banks (Outlook, Bank Position Forecast)."""
	return {"bank_account": list(ctx.banks)} if ctx.banks else {}


def kpi(ctx, count: int, amount: float, route: list, route_options: dict) -> frappe._dict:
	return frappe._dict(
		count=count, amount=flt(amount, ctx.precision), route=route, route_options=route_options
	)


def iso(value) -> str | None:
	return str(getdate(value)) if value else None


def amount_converter(ctx) -> AmountConverter:
	"""The reports' cheque converter at the dashboard's today, sharing the call's rate cache (one lookup per
	currency and call, whatever the number of rows)."""
	converter = AmountConverter(ctx.company, ctx.today)
	converter._rates = ctx.rates
	return converter


def company_converter(ctx):
	from treasury.bank_facilities.report_utils import CompanyConverter

	converter = CompanyConverter(ctx.company, ctx.today)
	converter._rates = ctx.rates
	return converter


# ---------------------------------------------------------------------------
# Positions
# ---------------------------------------------------------------------------


def positions_section(ctx) -> frappe._dict:
	positions30, flows30 = collect_positions(ctx.company, MATURITY_DAYS, ctx.banks, ctx.today)
	thresholds = get_thresholds(ctx.company)
	horizon = ctx.horizon
	positions = []
	for position in positions30:
		p = frappe._dict(position, days=position.days[:horizon])
		apply_thresholds(p, [thresholds[b] for b in p.bank_accounts if b in thresholds])
		positions.append(p)

	rows = sorted(
		(position_row(ctx, p) for p in positions), key=lambda r: (level_order(r.level), r.bank_account)
	)
	to_date = getdate(add_days(ctx.today, horizon - 1))
	forecast_options = {
		"company": ctx.company,
		"from_date": str(ctx.today),
		"to_date": str(to_date),
		"periodicity": DAILY,
		"currency_view": BANK_CURRENCY,
		"include_overdue": 1,
	}

	def outlook_options(days, **values):
		return {"company": ctx.company, "horizon_days": days, **values, **bank_list(ctx)}

	def level_kpi(level):
		return frappe._dict(
			count=sum(1 for r in rows if r.level == level),
			route=OUTLOOK,
			route_options=outlook_options(horizon, level=level),
		)

	totals = frappe._dict(
		current_company=flt(sum(r.current_company for r in rows), ctx.precision),
		projected_7_company=flt(sum(r.projected_7_company for r in rows), ctx.precision),
		# design 7h P5: the "Projected in {H} Days" card follows the chosen horizon
		closing_horizon_company=flt(sum(r.closing_horizon_company for r in rows), ctx.precision),
		horizon_route=frappe._dict(route=FORECAST, route_options={**forecast_options, **bank_list(ctx)}),
		below_minimum=level_kpi(CRITICAL),
		below_warning=level_kpi(WARNING),
		cash_route=frappe._dict(route=OUTLOOK, route_options=outlook_options(PROJECTION_DAY)),
		projected_route=frappe._dict(
			route=FORECAST,
			route_options={
				**forecast_options,
				"to_date": str(getdate(add_days(ctx.today, PROJECTION_DAY - 1))),
				**bank_list(ctx),
			},
		),
	)
	return frappe._dict(
		banks=rows,
		totals=totals,
		unassigned_flows=positions30[0].unassigned_flows if positions30 else 0,
		charts=frappe._dict(
			balance_curve=balance_curve(ctx, positions),
			cash_flows=cash_flows(ctx, positions),
			maturities=maturities(ctx, positions30, flows30),
		),
		route=FORECAST,
		route_options={**forecast_options, **bank_list(ctx)},
	)


def position_row(ctx, p) -> frappe._dict:
	"""A bank's figures with the arithmetic of the Bank Balance Outlook row (`BankBalanceOutlook.row`)."""
	lowest = p.lowest or frappe._dict(date=None, closing=p.current_balance)
	minimum = p.minimum
	shortfall = max(flt(minimum) - flt(lowest.closing), 0) if minimum is not None else 0
	rate = ctx.rate(p.currency)
	projected = p.days[min(PROJECTION_DAY, len(p.days)) - 1].closing
	closing_horizon = p.days[-1].closing if p.days else p.current_balance
	return frappe._dict(
		bank_account=p.bank_account,
		bank_accounts=p.bank_accounts,
		bank=p.bank,
		currency=p.currency,
		current_balance=p.current_balance,
		projected_7=projected,
		closing_horizon=closing_horizon,
		lowest=frappe._dict(
			date=iso(lowest.date),
			closing=lowest.closing,
			closing_company=flt(flt(lowest.closing) * rate, ctx.precision),
		),
		level=p.level,
		level_label=_(p.level, context=LEVEL_CTX),
		minimum=minimum,
		warning=p.warning,
		first_breach=iso(p.first_breach),
		shortfall=flt(shortfall, ctx.precision),
		current_company=flt(flt(p.current_balance) * rate, ctx.precision),
		projected_7_company=flt(flt(projected) * rate, ctx.precision),
		closing_horizon_company=flt(flt(closing_horizon) * rate, ctx.precision),
		route=FORECAST,
		route_options={
			"company": ctx.company,
			"bank_account": list(p.bank_accounts),
			"from_date": str(ctx.today),
			"to_date": str(getdate(add_days(ctx.today, ctx.horizon - 1))),
			"periodicity": DAILY,
			"currency_view": BANK_CURRENCY,
			"include_overdue": 1,
		},
	)


def balance_curve(ctx, positions) -> frappe._dict:
	"""Projected closing per bank and day (company currency, today's rate)."""
	labels = [formatdate(add_days(ctx.today, i)) for i in range(ctx.horizon)]
	datasets = []
	for p in positions:
		rate = ctx.rate(p.currency)
		datasets.append(
			{"name": p.bank_account, "values": [flt(d.closing * rate, ctx.precision) for d in p.days]}
		)
	return frappe._dict(
		data={"labels": labels, "datasets": datasets}, type="line", currency=ctx.company_currency
	)


def cash_flows(ctx, positions) -> frappe._dict:
	"""Inflows / outflows (actual + expected; day 0 with the overdue flows) per day (7, 14 days) or week."""
	to_date = getdate(add_days(ctx.today, ctx.horizon - 1))
	periods = build_periods(ctx.today, to_date, DAILY if ctx.horizon <= 14 else WEEKLY)
	inflows, outflows = [0.0] * len(periods), [0.0] * len(periods)
	for p in positions:
		rate = ctx.rate(p.currency)
		for day in p.days:
			index = next(i for i, period in enumerate(periods) if period.start <= day.date <= period.end)
			inflows[index] += (flt(day.actual_in) + flt(day.expected_in)) * rate
			outflows[index] += (flt(day.actual_out) + flt(day.expected_out)) * rate
	inflows = [flt(v, ctx.precision) for v in inflows]
	outflows = [flt(v, ctx.precision) for v in outflows]
	return frappe._dict(
		data={
			"labels": [period.label for period in periods],
			"datasets": [
				{"name": _("Inflows"), "chartType": "bar", "values": inflows},
				{"name": _("Outflows"), "chartType": "bar", "values": outflows},
				{
					"name": _("Net", context="Treasury"),
					"chartType": "line",
					"values": [flt(i - o, ctx.precision) for i, o in zip(inflows, outflows, strict=True)],
				},
			],
		},
		type="axis-mixed",
		currency=ctx.company_currency,
	)


def maturity_category(flow) -> str:
	if flow.get("reference_doctype") == "PDC Cheque":
		return "Cheques In" if flow.direction == IN else "Cheques Out"
	return CATEGORY_BY_DOCTYPE.get(flow.get("reference_doctype"), "Other")


def maturities(ctx, positions30, flows30) -> frappe._dict:
	"""The expected flows of the next 30 days by week and category, signed (in +, out -), company currency;
	an "Overdue" bucket first when flows are overdue. Flows without a bank count when no bank is selected."""
	periods = build_periods(ctx.today, add_days(ctx.today, MATURITY_DAYS - 1), WEEKLY)
	currency_of = {b: p.currency for p in positions30 for b in p.bank_accounts}
	overdue = any(f.date < ctx.today for f in flows30)
	labels = ([_("Overdue", context="Treasury")] if overdue else []) + [p.label for p in periods]
	values = {category: [0.0] * len(labels) for category in MATURITY_CATEGORIES}
	for flow in flows30:
		if flow.date < ctx.today:
			index = 0
		else:
			index = next(
				(i for i, period in enumerate(periods) if period.start <= flow.date <= period.end), None
			)
			if index is None:
				continue
			index += 1 if overdue else 0
		currency = flow.get("currency") or currency_of.get(flow.get("bank_account")) or ctx.company_currency
		amount = flt(flow.amount) * ctx.rate(currency)
		values[maturity_category(flow)][index] += amount if flow.direction == IN else -amount
	categories = [category for category in MATURITY_CATEGORIES if any(values[category])]
	datasets = [
		{"name": _(category, context=CTX), "values": [flt(v, ctx.precision) for v in values[category]]}
		for category in categories
	]
	return frappe._dict(
		data={"labels": labels, "datasets": datasets},
		keys=categories,  # the untranslated category of each dataset: the page gives each a fixed colour
		overdue=1 if overdue else 0,
		type="bar",
		barOptions={"stacked": 1},
		currency=ctx.company_currency,
		route=FORECAST,
		route_options={
			"company": ctx.company,
			"from_date": str(ctx.today),
			"to_date": str(getdate(add_days(ctx.today, MATURITY_DAYS - 1))),
			"periodicity": WEEKLY,
			"currency_view": COMPANY_CURRENCY,
			**bank_list(ctx),
		},
	)


# ---------------------------------------------------------------------------
# Cheques, transfers, charges
# ---------------------------------------------------------------------------


def cheques_section(ctx) -> frappe._dict:
	from treasury.pdc.dashboard import DUE_7_DAYS, OUTSTANDING, open_cheque_totals
	from treasury.pdc.report.bounced_and_postponed_cheques.bounced_and_postponed_cheques import (
		BOUNCED,
		get_bounced_rows,
	)

	converter = amount_converter(ctx)
	totals = open_cheque_totals(ctx.company, ctx.banks, ctx.today, converter=converter)
	register = ["query-report", "PDC Register"]

	def card(direction, scope):
		value = totals[direction][scope]
		return kpi(ctx, value.count, value.amount, register, value.route_options)

	conditions = {"company": ctx.company}
	if ctx.banks:
		conditions["bank_account"] = ["in", list(ctx.banks)]
	bounces = get_bounced_rows(conditions, ctx.month_start, ctx.today)
	return frappe._dict(
		due_out=card("Outgoing", DUE_7_DAYS),
		due_in=card("Incoming", DUE_7_DAYS),
		issued=card("Outgoing", OUTSTANDING),
		in_hand=card("Incoming", OUTSTANDING),
		bounced_month=kpi(
			ctx,
			len(bounces),
			sum(converter.company_amount(row) for row in bounces),
			["query-report", "Bounced and Postponed Cheques"],
			{
				"company": ctx.company,
				"view": BOUNCED,
				"from_date": str(ctx.month_start),
				"to_date": str(ctx.today),
				**one_bank(ctx),
			},
		),
	)


def transfers_section(ctx) -> frappe._dict:
	or_filters = None
	if ctx.banks:
		or_filters = {"bank_account": ["in", list(ctx.banks)], "to_bank_account": ["in", list(ctx.banks)]}
	rows = frappe.get_list(
		"Bank Transfer",
		filters={"company": ctx.company, "docstatus": 1, "status": "Sent"},
		or_filters=or_filters,
		fields=["name", "base_amount"],
	)
	return frappe._dict(
		awaiting=kpi(
			ctx,
			len(rows),
			sum(flt(r.base_amount) for r in rows),
			["List", "Bank Transfer"],
			{"company": ctx.company, "status": "Sent", "docstatus": 1, **one_bank(ctx)},
		)
	)


def charges_section(ctx) -> frappe._dict:
	from treasury.bank_transfers.report.bank_charges_analysis.bank_charges_analysis import (
		CATEGORY,
		get_rows,
		group_rows,
	)

	rows = get_rows(ctx.company, ctx.month_start, ctx.today, frappe._dict())
	if ctx.banks:
		rows = [r for r in rows if r.bank_account in ctx.banks]
	charges = sum(r.amount for r in rows if r.amount > 0)
	refunds = -sum(r.amount for r in rows if r.amount < 0)
	groups = group_rows(rows, CATEGORY, ctx.company_currency)
	route_options = {
		"company": ctx.company,
		"from_date": str(ctx.month_start),
		"to_date": str(ctx.today),
		"group_by": CATEGORY,
		**one_bank(ctx),
	}
	return frappe._dict(
		month=frappe._dict(
			net=flt(charges - refunds, ctx.precision),
			charges=flt(charges, ctx.precision),
			refunds=flt(refunds, ctx.precision),
			vouchers=len(rows),
			route=["query-report", "Bank Charges Analysis"],
			route_options=route_options,
		),
		by_category=frappe._dict(
			data={
				"labels": [g.group for g in groups],
				"datasets": [{"name": _("Net", context="Treasury"), "values": [g.net for g in groups]}],
			},
			type="bar",
			currency=ctx.company_currency,
		),
	)


# ---------------------------------------------------------------------------
# Company-wide sections
# ---------------------------------------------------------------------------


def security_section(ctx) -> frappe._dict:
	from treasury.security_cheques.lifecycle import INCOMING, OUTGOING
	from treasury.security_cheques.report.security_exposure import security_exposure
	from treasury.security_cheques.report_utils import get_security_cheques

	cheques = [
		c
		for c in get_security_cheques(security_exposure.get_filters(frappe._dict(), ctx.company))
		if security_exposure.is_live(c)
	]
	rows = security_exposure.group_by_party(cheques, amount_converter(ctx))
	totals = security_exposure.get_totals(rows)

	def count(direction):
		return sum(r.fixed_count + r.open_count for r in rows if r.direction == direction)

	return frappe._dict(
		totals,
		count_held=count(INCOMING),
		count_given=count(OUTGOING),
		route=["query-report", "Security Exposure"],
		route_options={"company": ctx.company},
	)


def facilities_section(ctx) -> frappe._dict:
	from treasury.bank_facilities.facility import CLOSED, DRAFT
	from treasury.bank_facilities.report_utils import get_facilities
	from treasury.bank_facilities.utilisation import get_utilisation

	route = ["query-report", "Facility Utilisation"]
	facilities = get_facilities({"company": ctx.company, "status": ["not in", [DRAFT, CLOSED]]})
	converter = company_converter(ctx)
	rows = []
	for facility in facilities[:MAX_FACILITIES]:
		u = get_utilisation(facility.name)
		rate = converter.rate(facility.currency)
		rows.append(
			frappe._dict(
				facility=facility.name,
				facility_name=facility.facility_name,
				lender=facility.lender_name or facility.bank,
				currency=facility.currency,
				limit=u.total_limit,
				utilised=u.utilised,
				available=u.available,
				percent_used=u.percent_used,
				limit_company=flt(flt(u.total_limit) * rate, ctx.precision),
				utilised_company=flt(flt(u.utilised) * rate, ctx.precision),
				route=route,
				route_options={"company": ctx.company, "facility": facility.name},
			)
		)
	rows.sort(key=lambda r: (-flt(r.percent_used), r.facility))
	limit_company = flt(sum(r.limit_company for r in rows), ctx.precision)
	utilised_company = flt(sum(r.utilised_company for r in rows), ctx.precision)
	return frappe._dict(
		rows=rows,
		truncated=max(len(facilities) - MAX_FACILITIES, 0),
		totals=frappe._dict(
			limit_company=limit_company,
			utilised_company=utilised_company,
			percent_used=flt(utilised_company * 100 / limit_company, 2) if limit_company else 0,
			route=route,
			route_options={"company": ctx.company},
		),
	)


def installments_section(ctx) -> frappe._dict:
	from treasury.bank_facilities.report_utils import get_installments
	from treasury.bank_facilities.repayment import OVERDUE

	route = ["query-report", "Facility Repayment Schedule Report"]
	until = getdate(add_days(ctx.today, MATURITY_DAYS))
	rows = get_installments(ctx.company, to_date=until, as_of=ctx.today)
	converter = company_converter(ctx)
	overdue = [r for r in rows if r.status == OVERDUE]
	due = [r for r in rows if ctx.today <= getdate(r.due_date) <= until]
	return frappe._dict(
		overdue=kpi(
			ctx,
			len(overdue),
			sum(converter.convert(r.unpaid, r.currency) for r in overdue),
			route,
			{"company": ctx.company, "status": OVERDUE},
		),
		due_30=kpi(
			ctx,
			len(due),
			sum(converter.convert(r.unpaid, r.currency) for r in due),
			route,
			{
				"company": ctx.company,
				"from_date": str(ctx.today),
				"to_date": str(until),
				"include_overdue": 0,
			},
		),
		next=[
			frappe._dict(
				due_date=iso(r.due_date),
				drawdown=r.drawdown,
				lender_name=r.lender_name or r.bank,
				installment_no=r.installment_no,
				amount_company=converter.convert(r.unpaid, r.currency),
				display_status=r.display_status,
			)
			for r in due[:NEXT_INSTALLMENTS]
		],
	)


def guarantees_section(ctx) -> frappe._dict:
	from treasury.bank_facilities.report.lg_register import lg_register

	filters = {"company": ctx.company, "open_only": 1, "expiring_within": EXPIRY_WINDOW_DAYS}
	data = lg_register.execute(dict(filters))[1]
	return frappe._dict(
		count=len(data),
		amount=flt(sum(flt(r.open_company) for r in data), ctx.precision),
		next=[
			frappe._dict(
				name=r.name,
				lg_no=r.lg_no,
				beneficiary_name=r.beneficiary_name,
				effective_expiry_date=iso(r.effective_expiry_date),
				days_to_expiry=r.days_to_expiry,
				open_company=r.open_company,
			)
			for r in data[:NEXT_INSTRUMENTS]
		],
		route=["query-report", "LG Register"],
		route_options=filters,
	)


def credits_section(ctx) -> frappe._dict:
	from treasury.bank_facilities.report.lc_register import lc_register

	filters = {"company": ctx.company, "open_only": 1, "expiring_within": EXPIRY_WINDOW_DAYS}
	data = lc_register.execute(dict(filters))[1]
	data = sorted(data, key=lambda r: (getdate(r.effective_expiry_date), r.name))
	return frappe._dict(
		count=len(data),
		amount=flt(sum(flt(r.open_company) for r in data), ctx.precision),
		next=[
			frappe._dict(
				name=r.name,
				lc_no=r.lc_no,
				supplier_name=r.supplier_name,
				effective_expiry_date=iso(r.effective_expiry_date),
				days_to_expiry=r.days_to_expiry,
				open_company=r.open_company,
			)
			for r in data[:NEXT_INSTRUMENTS]
		],
		route=["query-report", "LC Register"],
		route_options=filters,
	)


SECTION_BUILDERS = {
	"positions": positions_section,
	"cheques": cheques_section,
	"transfers": transfers_section,
	"charges": charges_section,
	"security": security_section,
	"facilities": facilities_section,
	"installments": installments_section,
	"guarantees": guarantees_section,
	"credits": credits_section,
}
