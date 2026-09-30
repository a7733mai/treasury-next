# Copyright (c) 2026, Ahmed Abdelmageed and contributors
# For license information, please see license.txt

"""Bank Position Forecast (PLAN.md §5.1).

Opening = the actual GL balance of the selected banks' GL accounts on the day before From Date. The bank GL
moves only on the bank posting of a cheque, so the GL is the actual position and every cheque whose bank
posting is still to come is an expected flow (see `report_utils.forecast_date` for its date):

- incoming: Received / Deposited on the effective (possibly postponed) due date;
- outgoing: Delivered (plus Created with "Include Created") on the effective due date;
- both: Cleared but not bank posted yet (bank posting mode Manual) on the date the bank moved the money
  (bank transaction date, else actual clearance date), which may be well before the due date.

Actual movements are the GL movements of the bank accounts within the range, so past periods are exact and
a bank posting dated in the future (future clearance dates allowed) is shown where it is booked.

Expected flows dated before today are overdue: with "Include Overdue" they are shown in an "Overdue" row
counted as expected today (placed just before the period containing today, i.e. first when From Date is
today or later); otherwise they are left out. When From Date is after today, the flows expected from today
to the day before From Date are not in the (GL) opening either: they are shown in a "Brought Forward" row
before the first period. Other modules add flows through `treasury_forecast_sources` and summary cards
through `treasury_forecast_summary_sources` (treasury.pdc.forecast_sources).

With no Bank Account selected, every cheque of the company is counted (cheques without a bank account under
"Unassigned Bank"), while the opening / actual columns cover the company bank accounts with a GL account.

`horizon_weeks` (not shown in the filter bar) replaces To Date by From Date + that many weeks; the
"Expected Cash Flow" dashboard chart uses it for its 12-week horizon.

`per_bank` and `today` (internal, never in the filter bar) are set only by treasury.pdc.bank_positions, which
builds per-bank daily positions from `collect_flows`: every flow keeps its bank account currency amount (no
single view currency, so banks in different currencies are allowed) and "today" may be given.
"""

from bisect import bisect_right

import frappe
from frappe import _
from frappe.utils import add_days, cint, flt, formatdate, get_last_day, getdate

from treasury.pdc import forecast_sources
from treasury.pdc.report_utils import (
	BANK_POSTED,
	CTX,
	DOCTYPE,
	INCOMING,
	OUTGOING,
	AmountConverter,
	as_list,
	expected_statuses,
	forecast_date,
	get_bank_accounts,
	get_cheques,
	get_gl_balance,
	get_gl_movements,
	get_precision,
	report_today,
	validate_company,
	validate_date_range,
)

DAILY, WEEKLY, MONTHLY = "Daily", "Weekly", "Monthly"
BANK_CURRENCY, COMPANY_CURRENCY = "Bank Currency", "Company Currency"
DEFAULT_DAYS = 90
IN, OUT = "in", "out"


def execute(filters=None):
	return BankPositionForecast(filters).run()


def build_periods(from_date, to_date, periodicity: str) -> list[frappe._dict]:
	"""Consecutive periods covering [from_date, to_date]: single days, 7-day weeks starting on From Date, or
	calendar months (the first and last one clipped to the range)."""
	periods = []
	start, to_date = getdate(from_date), getdate(to_date)
	while start <= to_date:
		if periodicity == DAILY:
			end = start
		elif periodicity == MONTHLY:
			end = getdate(get_last_day(start))
		else:
			end = getdate(add_days(start, 6))
		end = min(end, to_date)
		label = formatdate(start) if start == end else f"{formatdate(start)} - {formatdate(end)}"
		periods.append(frappe._dict(start=start, end=end, label=label))
		start = getdate(add_days(end, 1))
	return periods


class BankPositionForecast:
	def __init__(self, filters=None):
		filters = frappe._dict(filters or {})
		self.company = validate_company(filters.company)
		self.per_bank = cint(filters.per_bank)
		self.today = getdate(filters.today) if self.per_bank and filters.today else report_today()
		self.from_date = getdate(filters.from_date or self.today)
		self.to_date = getdate(filters.to_date or add_days(self.from_date, DEFAULT_DAYS))
		if cint(filters.horizon_weeks) > 0:
			# fixed horizon (dashboard chart "Expected Cash Flow"): overrides To Date
			self.to_date = getdate(add_days(self.from_date, 7 * cint(filters.horizon_weeks) - 1))
		validate_date_range(self.from_date, self.to_date)
		self.periodicity = filters.periodicity or WEEKLY
		if self.periodicity not in (DAILY, WEEKLY, MONTHLY):
			labels = ", ".join(_(p, context=CTX) for p in (DAILY, WEEKLY, MONTHLY))
			frappe.throw(_("Periodicity must be one of: {0}.").format(labels))
		self.include_created = cint(filters.include_created)
		self.include_overdue = 1 if filters.get("include_overdue") is None else cint(filters.include_overdue)
		self.show_details = cint(filters.show_details)
		self.selected_banks = as_list(filters.bank_account)
		self.banks = get_bank_accounts(self.company, self.selected_banks)
		self.bank_names = {b.name for b in self.banks}
		# per bank: every amount stays in its bank account currency
		self.bank_view = self.per_bank or filters.currency_view == BANK_CURRENCY
		self.converter = AmountConverter(self.company, self.today)
		self.currency = self.get_view_currency()
		self.precision = get_precision()
		self.summary_values = frappe._dict()

	def get_view_currency(self) -> str | None:
		if self.per_bank:
			return None
		if not self.bank_view:
			return self.converter.company_currency
		currencies = sorted({b.currency for b in self.banks if b.currency})
		if len(currencies) > 1:
			frappe.throw(
				_(
					"The bank accounts are in different currencies ({0}). Select one currency's bank accounts or the Company Currency view."
				).format(", ".join(currencies))
			)
		return currencies[0] if currencies else self.converter.company_currency

	# ------------------------------------------------------------------ run

	def run(self):
		self.periods = build_periods(self.from_date, self.to_date, self.periodicity)
		self.overdue_index = self.get_overdue_index()
		flows = self.collect_flows()
		overdue, brought_forward, by_period = self.assign_flows(flows)
		actual = self.get_actual_by_period()
		summary_rows, data = self.build_rows(overdue, brought_forward, by_period, actual)
		return self.get_columns(), data, None, self.get_chart(summary_rows), self.get_summary(summary_rows)

	def get_overdue_index(self) -> int | None:
		"""Position of the Overdue row: before the first period that is not entirely in the past.

		None when the whole range is in the past (overdue flows are then expected after To Date)."""
		if not self.include_overdue:
			return None
		for index, period in enumerate(self.periods):
			if period.end >= self.today:
				return index
		return None

	# ------------------------------------------------------------------ flows

	def accepts_bank(self, bank_account: str | None) -> bool:
		"""Flows of the selected banks; every flow (with or without a bank) when no bank is selected."""
		if self.selected_banks:
			return bank_account in self.bank_names
		return True

	def get_expected_cheques(self, direction: str) -> list[frappe._dict]:
		"""Cheques of `direction` whose bank posting is still to come and may fall in the report.

		Not cleared ones are bounded by their effective due date (no lower bound when overdue ones count;
		else from today, which also covers the Brought Forward flows). Cleared-not-bank-posted ones (Manual
		mode, a short list) are dated by the bank's date, so they are filtered by `forecast_date` later."""
		base = {
			"company": self.company,
			"direction": direction,
			"accounting_status": ["!=", BANK_POSTED],
		}
		if self.selected_banks:
			base["bank_account"] = ["in", sorted(self.bank_names)]
		open_statuses = [s for s in expected_statuses(direction, self.include_created) if s != "Cleared"]
		if self.include_overdue:
			due_filter = ["<=", self.to_date]
		else:
			due_filter = ["between", [self.today, self.to_date]]
		return get_cheques(
			{**base, "operational_status": ["in", open_statuses], "effective_due_date": due_filter}
		) + get_cheques({**base, "operational_status": "Cleared"})

	def collect_flows(self) -> list[frappe._dict]:
		"""Every expected flow (cheques, then the hook sources), unfiltered by date."""
		return self.get_cheque_flows() + self.get_hook_flows()

	def get_cheque_flows(self) -> list[frappe._dict]:
		flows = []
		for direction in (OUTGOING, INCOMING):
			for row in self.get_expected_cheques(direction):
				if not self.accepts_bank(row.bank_account):
					continue
				if self.bank_view and not self.per_bank and row.currency != self.currency:
					continue
				flow = self._cheque_flow(row, direction)
				flow.amount = flow.amount_in_account_currency if self.bank_view else flow.amount_company
				flows.append(flow)
		return flows

	def _cheque_flow(self, row, direction: str) -> frappe._dict:
		"""A cheque as a flow, with its amount in the cheque (= bank account) currency and, outside the bank
		currency views (which never convert), in the company currency."""
		return frappe._dict(
			date=forecast_date(row),
			direction=IN if direction == INCOMING else OUT,
			amount=None,
			amount_company=None if self.bank_view else self.converter.company_amount(row),
			amount_in_account_currency=flt(row.amount, self.precision),
			currency=row.currency,
			bank_account=row.bank_account,
			reference_doctype=DOCTYPE,
			reference_name=row.name,
			party=row.party_name or row.party,
			description=row.cheque_no,
		)

	def get_hook_filters(self) -> frappe._dict:
		return frappe._dict(
			company=self.company,
			bank_accounts=sorted(self.bank_names),
			from_date=self.from_date,
			to_date=self.to_date,
			currency_view=BANK_CURRENCY if self.bank_view else COMPANY_CURRENCY,
		)

	def get_hook_flows(self) -> list[frappe._dict]:
		flows = []
		for flow in forecast_sources.get_additional_flows(self.get_hook_filters()):
			flow = frappe._dict(flow)
			if flow.direction not in (IN, OUT) or not flow.date or not self.accepts_bank(flow.bank_account):
				continue
			amount = flow.amount_in_account_currency if self.bank_view else flow.amount
			flow.amount = flt(amount, self.precision)
			flow.date = getdate(flow.date)
			flows.append(flow)
		return flows

	def assign_flows(self, flows: list[frappe._dict]) -> tuple[list, list, list[list]]:
		"""Split flows into the overdue ones, the brought forward ones (from today to the day before a future
		From Date) and one list per period; flows after To Date are dropped."""
		starts = [p.start for p in self.periods]
		overdue, brought_forward, by_period = [], [], [[] for _p in self.periods]
		for flow in flows:
			if flow.date < self.today:
				if self.overdue_index is not None:
					overdue.append(flow)
			elif flow.date < self.from_date:
				brought_forward.append(flow)
			elif flow.date <= self.to_date:
				by_period[bisect_right(starts, flow.date) - 1].append(flow)
		return overdue, brought_forward, by_period

	# ------------------------------------------------------------------ actual (GL)

	def get_gl_accounts(self) -> list[str]:
		return sorted({b.account for b in self.banks if b.account})

	def get_actual_by_period(self) -> list[frappe._dict]:
		starts = [p.start for p in self.periods]
		actual = [frappe._dict(debit=0.0, credit=0.0) for _p in self.periods]
		for row in get_gl_movements(
			self.company, self.get_gl_accounts(), self.from_date, self.to_date, self.bank_view
		):
			bucket = actual[bisect_right(starts, getdate(row.posting_date)) - 1]
			bucket.debit += flt(row.debit)
			bucket.credit += flt(row.credit)
		return actual

	def get_opening(self) -> float:
		return flt(
			get_gl_balance(
				self.company, self.get_gl_accounts(), add_days(self.from_date, -1), self.bank_view
			),
			self.precision,
		)

	# ------------------------------------------------------------------ rows

	def build_rows(self, overdue, brought_forward, by_period, actual) -> tuple[list, list]:
		summary_rows, data = [], []
		balance = self.get_opening()
		for index, period in enumerate(self.periods):
			if index == self.overdue_index and overdue:
				row = self.make_row(
					"overdue",
					_("Overdue", context=CTX),
					None,
					add_days(self.today, -1),
					balance,
					0,
					0,
					overdue,
					1,
				)
				balance = row.closing
				summary_rows.append(row)
				data += [row, *self.detail_rows(row, overdue)]
			if index == 0 and brought_forward:
				# only when From Date is after today: the Overdue row (if any) is then before this one
				row = self.make_row(
					"brought_forward",
					_("Brought Forward", context=CTX),
					self.today,
					add_days(self.from_date, -1),
					balance,
					0,
					0,
					brought_forward,
				)
				balance = row.closing
				summary_rows.append(row)
				data += [row, *self.detail_rows(row, brought_forward)]
			flows = by_period[index]
			row = self.make_row(
				f"p{index}",
				period.label,
				period.start,
				period.end,
				balance,
				actual[index].debit,
				actual[index].credit,
				flows,
			)
			balance = row.closing
			summary_rows.append(row)
			data += [row, *self.detail_rows(row, flows)]
		return summary_rows, data

	def make_row(
		self, row_id, label, start, end, opening, actual_in, actual_out, flows, is_overdue=0
	) -> frappe._dict:
		expected_in = sum(f.amount for f in flows if f.direction == IN)
		expected_out = sum(f.amount for f in flows if f.direction == OUT)
		net = flt(actual_in) - flt(actual_out) + expected_in - expected_out
		return frappe._dict(
			row_id=row_id,
			parent_row_id=None,
			indent=0,
			period=label,
			period_start=start,
			period_end=end,
			is_overdue=is_overdue,
			opening=flt(opening, self.precision),
			actual_in=flt(actual_in, self.precision),
			actual_out=flt(actual_out, self.precision),
			expected_in=flt(expected_in, self.precision),
			expected_out=flt(expected_out, self.precision),
			net=flt(net, self.precision),
			closing=flt(flt(opening) + net, self.precision),
			currency=self.currency,
		)

	def detail_rows(self, parent, flows) -> list[frappe._dict]:
		if not self.show_details:
			return []
		rows = []
		for index, flow in enumerate(sorted(flows, key=lambda f: (f.date, f.reference_name or ""))):
			rows.append(
				frappe._dict(
					row_id=f"{parent.row_id}-{index}",
					parent_row_id=parent.row_id,
					indent=1,
					period=formatdate(flow.date),
					due_date=flow.date,
					expected_in=flow.amount if flow.direction == IN else 0,
					expected_out=flow.amount if flow.direction == OUT else 0,
					reference_doctype=flow.reference_doctype,
					reference_name=flow.reference_name,
					party=flow.party,
					bank_account=flow.bank_account or _("Unassigned Bank"),
					description=flow.description,
					currency=self.currency,
				)
			)
		return rows

	# ------------------------------------------------------------------ output

	def get_columns(self) -> list[dict]:
		def currency(fieldname, label, width=130):
			return {
				"fieldname": fieldname,
				"label": label,
				"fieldtype": "Currency",
				"options": "currency",
				"width": width,
			}

		columns = [
			{"fieldname": "period", "label": _("Period", context=CTX), "fieldtype": "Data", "width": 210},
			currency("opening", _("Opening Balance", context=CTX)),
			currency("actual_in", _("Actual In")),
			currency("actual_out", _("Actual Out")),
			currency("expected_in", _("Expected In")),
			currency("expected_out", _("Expected Out")),
			currency("net", _("Net Flow")),
			currency("closing", _("Projected Closing Balance"), 160),
		]
		if self.show_details:
			columns += [
				{
					"fieldname": "reference_doctype",
					"label": _("Reference Type"),
					"fieldtype": "Link",
					"options": "DocType",
					"width": 110,
				},
				{
					"fieldname": "reference_name",
					"label": _("Reference Name", context="Treasury"),
					"fieldtype": "Dynamic Link",
					"options": "reference_doctype",
					"width": 170,
				},
				{"fieldname": "description", "label": _("Cheque No"), "fieldtype": "Data", "width": 110},
				{"fieldname": "party", "label": _("Party Name"), "fieldtype": "Data", "width": 170},
				{
					"fieldname": "bank_account",
					"label": _("Bank Account", context="Treasury"),
					"fieldtype": "Data",
					"width": 170,
				},
			]
		columns.append(
			{
				"fieldname": "currency",
				"label": _("Currency"),
				"fieldtype": "Link",
				"options": "Currency",
				"hidden": 1,
			}
		)
		return columns

	def get_chart(self, summary_rows) -> dict:
		return {
			"data": {
				"labels": [r.period for r in summary_rows],
				"datasets": [
					{
						"name": _("Projected Closing Balance"),
						"values": [r.closing for r in summary_rows],
						"chartType": "line",
					},
					{
						"name": _("Expected In"),
						"values": [r.expected_in for r in summary_rows],
						"chartType": "bar",
					},
					{
						"name": _("Expected Out"),
						"values": [r.expected_out for r in summary_rows],
						"chartType": "bar",
					},
				],
			},
			"type": "axis-mixed",
			"colors": ["#2490EF", "#29CD42", "#FF5858"],
			"fieldtype": "Currency",
			"options": self.currency,
		}

	def get_summary(self, summary_rows) -> list[dict]:
		lowest = min(summary_rows, key=lambda r: r.closing)
		values = self.summary_values
		values.current_balance = flt(
			get_gl_balance(self.company, self.get_gl_accounts(), self.today, self.bank_view), self.precision
		)
		values.total_expected_in = flt(sum(r.expected_in for r in summary_rows), self.precision)
		values.total_expected_out = flt(sum(r.expected_out for r in summary_rows), self.precision)
		values.lowest_balance = lowest.closing
		values.lowest_date = self.today if lowest.is_overdue else lowest.period_end
		values.closing = summary_rows[-1].closing

		def card(value, label, indicator):
			return {
				"value": value,
				"label": label,
				"datatype": "Currency",
				"currency": self.currency,
				"indicator": indicator,
			}

		hook_filters = self.get_hook_filters()
		hook_filters.update(currency=self.currency, today=self.today)
		extra = [
			card(flt(item.get("value"), self.precision), item.get("label"), item.get("indicator") or "Blue")
			for item in forecast_sources.get_additional_summary(hook_filters)
		]
		return [
			card(values.current_balance, _("Current Actual Balance"), "Blue"),
			card(values.total_expected_in, _("Total Expected In"), "Green"),
			card(values.total_expected_out, _("Total Expected Out"), "Red"),
			card(
				values.lowest_balance,
				_("Lowest Projected Balance ({0})").format(formatdate(values.lowest_date)),
				"Red" if values.lowest_balance < 0 else "Orange",
			),
			card(
				values.closing,
				_("Projected Closing on {0}").format(formatdate(self.to_date)),
				"Red" if values.closing < 0 else "Green",
			),
			*extra,
		]
