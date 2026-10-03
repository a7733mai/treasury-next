"""Per-bank daily projected positions (spec 7e §3.1), built on the Bank Position Forecast engine.

One engine call per company collects every expected flow of all the banks (cheques and the
`treasury_forecast_sources` hooks) in the bank account currency; the GL balances and movements are read with
one query each. Positions are per GL account: Bank Accounts sharing a GL account share their money, so they
are merged into one position (`bank_account` = the first name, `bank_accounts` = all of them, their flows
pooled). Bank Accounts without a GL account are skipped; flows without a bank account are only counted
(`unassigned_flows`, the same value on every position of the company).

Equality contract (tested): for a bank B and day d, `closing` equals the Daily "Projected Closing Balance" of
the report run with bank_account = [B], Bank Currency, include_overdue = 1, from = today, to = today + H - 1
(day 0: the "today" row, after the Overdue row).

Permissions are the caller's by default (the engine reads with `frappe.get_list`: User Permissions apply, so the
view may be partial). A check that protects the company's money (the entry-time balance guard, the alert
scheduler) must not run on a partial view: it passes `system=True`, the company-wide view (`frappe.get_all`)
that does not switch the session user.
"""

import frappe
from frappe.utils import add_days, cint, flt, getdate

from treasury.pdc.report.bank_position_forecast.bank_position_forecast import (
	DAILY,
	IN,
	OUT,
	BankPositionForecast,
)
from treasury.pdc.report_utils import (
	get_gl_balances_by_account,
	get_gl_movements_by_account,
	get_precision,
	report_today,
)


def get_bank_positions(
	company: str,
	horizon_days: int,
	bank_accounts: list[str] | None = None,
	today=None,
	system: bool = False,
) -> list[frappe._dict]:
	"""Positions sorted by `bank_account`: {bank_account, bank_accounts, account, currency, bank, disabled,
	opening, current_balance, overdue_in, overdue_out, unassigned_flows, days: [{date, actual_in, actual_out,
	expected_in, expected_out, net, closing, flows}]} for today .. today + horizon_days - 1."""
	return collect_positions(company, horizon_days, bank_accounts, today, system)[0]


def collect_positions(
	company: str,
	horizon_days: int,
	bank_accounts: list[str] | None = None,
	today=None,
	system: bool = False,
) -> tuple[list[frappe._dict], list[frappe._dict]]:
	"""(positions, flows): the positions of `get_bank_positions` and every expected flow of the engine dated up
	to today + horizon_days - 1 (overdue ones and flows without a bank account included).

	Positions are horizon-independent (spec 7h F9): day d's closing never depends on the horizon (flows after
	To Date are dropped, the days before are identical), so one 30-day call holds the 7- and 14-day positions
	in its first days (the Treasury Dashboard)."""
	today = getdate(today or report_today())
	to_date = getdate(add_days(today, max(cint(horizon_days), 1) - 1))
	engine = BankPositionForecast(
		{
			"company": company,
			"from_date": today,
			"to_date": to_date,
			"periodicity": DAILY,
			"include_overdue": 1,
			"include_created": 0,
			"bank_account": bank_accounts or None,
			"per_bank": 1,
			"today": today,
		},
		system=system,
	)
	flows = [f for f in engine.collect_flows() if f.date <= to_date]
	precision = get_precision()

	groups: dict[str, list] = {}
	for bank in engine.banks:  # sorted by name
		if bank.account:
			groups.setdefault(bank.account, []).append(bank)
	if not groups:
		return [], flows
	accounts = sorted(groups)
	balances_prev = get_gl_balances_by_account(company, accounts, add_days(today, -1))
	balances_today = get_gl_balances_by_account(company, accounts, today)
	moves: dict[tuple, frappe._dict] = {
		(row.account, getdate(row.posting_date)): row
		for row in get_gl_movements_by_account(company, accounts, today, to_date)
	}
	details = {
		d.name: d
		for d in frappe.get_all(
			"Bank Account",
			filters={"name": ["in", [b.name for banks in groups.values() for b in banks]]},
			fields=["name", "bank", "disabled"],
		)
	}

	account_of = {b.name: account for account, banks in groups.items() for b in banks}
	flows_by_account: dict[str, list] = {account: [] for account in accounts}
	unassigned = 0
	for flow in flows:
		if not flow.bank_account:
			unassigned += 1
		elif flow.bank_account in account_of:
			flows_by_account[account_of[flow.bank_account]].append(flow)

	positions = []
	for account in accounts:
		banks = groups[account]
		first = details.get(banks[0].name) or frappe._dict()
		position = frappe._dict(
			bank_account=banks[0].name,
			bank_accounts=[b.name for b in banks],
			account=account,
			currency=banks[0].currency,
			bank=first.get("bank"),
			disabled=all(cint((details.get(b.name) or {}).get("disabled")) for b in banks),
			opening=flt(balances_prev.get(account), precision),
			current_balance=flt(balances_today.get(account), precision),
			unassigned_flows=unassigned,
		)
		_build_days(position, flows_by_account[account], moves, today, to_date, precision)
		positions.append(position)
	return sorted(positions, key=lambda p: p.bank_account), flows


def _sums(flows) -> tuple[float, float]:
	return (
		sum(f.amount for f in flows if f.direction == IN),
		sum(f.amount for f in flows if f.direction == OUT),
	)


def _build_days(position, flows, moves, today, to_date, precision):
	"""Daily rows with the report's arithmetic (`BankPositionForecast.make_row`): the overdue flows are a row
	of their own before day 0, whose closing carries into day 0."""
	overdue = [f for f in flows if f.date < today]
	overdue_in, overdue_out = _sums(overdue)
	position.overdue_in = flt(overdue_in, precision)
	position.overdue_out = flt(overdue_out, precision)
	balance = position.opening
	if overdue:
		balance = flt(flt(balance) + (overdue_in - overdue_out), precision)

	by_date: dict = {}
	for flow in flows:
		if flow.date >= today:
			by_date.setdefault(flow.date, []).append(flow)
	days = []
	date = today
	while date <= to_date:
		day_flows = by_date.get(date, [])
		expected_in, expected_out = _sums(day_flows)
		move = moves.get((position.account, date)) or frappe._dict()
		actual_in, actual_out = flt(move.get("debit")), flt(move.get("credit"))
		net = flt(actual_in) - flt(actual_out) + expected_in - expected_out
		closing = flt(flt(balance) + net, precision)
		if date == today and overdue:
			day_flows = overdue + day_flows
			expected_in += overdue_in
			expected_out += overdue_out
			net += overdue_in - overdue_out
		days.append(
			frappe._dict(
				date=date,
				actual_in=flt(actual_in, precision),
				actual_out=flt(actual_out, precision),
				expected_in=flt(expected_in, precision),
				expected_out=flt(expected_out, precision),
				net=flt(net, precision),
				closing=closing,
				flows=day_flows,
			)
		)
		balance = closing
		date = getdate(add_days(date, 1))
	position.days = days
