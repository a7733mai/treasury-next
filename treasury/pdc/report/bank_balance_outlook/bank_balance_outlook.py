# Copyright (c) 2026, Ahmed Abdelmageed and contributors
# For license information, please see license.txt

"""Bank Balance Outlook (spec 7e §3.6): one row per bank position with its current balance, the lowest projected
balance within the horizon, its level against the Balance Thresholds and the shortfall below the minimum.

One evaluation (treasury.balance_alerts.positions.evaluate) with the user's own permissions: cheques and the
other flows are read with `frappe.get_list`, and User Permissions on Company and Bank Account apply. Amounts are
in the bank account currency; "Lowest (Company Currency)" converts the lowest balance at today's rate.
"""

import erpnext
import frappe
from frappe import _
from frappe.utils import cint, flt, fmt_money

from treasury.accounting import posting
from treasury.balance_alerts.positions import (
	LEVELS,
	evaluate,
	horizon,
	level_order,
	with_permitted_gl_siblings,
)
from treasury.pdc.report_utils import as_list, column, get_precision, report_today, validate_company

MAX_HORIZON_DAYS = 31
CTX = "Treasury Alert Log"
# the levels are shown with the report's context (design 7e §8)
LEVEL_CTX = "Bank Balance Outlook"


def execute(filters=None):
	return BankBalanceOutlook(filters).run()


class BankBalanceOutlook:
	def __init__(self, filters=None):
		filters = frappe._dict(filters or {})
		self.company = validate_company(filters.company)
		self.horizon = (
			cint(filters.horizon_days) if filters.get("horizon_days") not in (None, "") else horizon()
		)
		if not 1 <= self.horizon <= MAX_HORIZON_DAYS:
			frappe.throw(_("Horizon must be between 1 and 31 days."))
		# a bank sharing its GL account with another one is one position: selecting one of them selects both,
		# so the row keeps its pooled flows and strictest thresholds (the GL balance covers both anyway); a
		# sibling the user may not see is left out
		self.banks = with_permitted_gl_siblings(self.company, as_list(filters.bank_account))
		self.level = filters.level or None
		if self.level and self.level not in LEVELS:
			frappe.throw(_("Invalid level: {0}").format(self.level))
		self.today = report_today()
		self.company_currency = erpnext.get_company_currency(self.company)
		self.precision = get_precision()
		self._rates = {}

	def rate(self, currency: str | None) -> float:
		if not currency or currency == self.company_currency:
			return 1.0
		if currency not in self._rates:
			self._rates[currency] = flt(posting.get_cheque_exchange_rate(currency, self.company, self.today))
		return self._rates[currency]

	def run(self):
		positions = evaluate(self.company, self.banks or None, horizon_days=self.horizon)
		if self.level:
			positions = [p for p in positions if p.level == self.level]
		# worst first: Critical → Warning → OK → No Threshold, then the largest shortfall
		data = sorted(
			(self.row(p) for p in positions),
			key=lambda r: (level_order(r.level), -flt(r.shortfall), r.bank_account),
		)
		return self.get_columns(), data, None, self.get_chart(data), self.get_summary(data)

	def row(self, position) -> frappe._dict:
		lowest = position.lowest or frappe._dict(date=None, closing=position.current_balance)
		last = position.days[-1] if position.days else None
		minimum = position.minimum
		shortfall = max(flt(minimum) - flt(lowest.closing), 0) if minimum is not None else 0
		rate = self.rate(position.currency)
		return frappe._dict(
			bank_account=position.bank_account,
			bank=position.bank,
			currency=position.currency,
			current_balance=position.current_balance,
			lowest_balance=lowest.closing,
			lowest_date=lowest.date,
			closing_balance=last.closing if last else position.current_balance,
			warning_balance=position.warning,
			minimum_balance=minimum,
			level=position.level,
			level_label=_(position.level, context=LEVEL_CTX),
			first_breach=position.first_breach,
			shortfall=flt(shortfall, self.precision),
			lowest_company_currency=flt(flt(lowest.closing) * rate, self.precision),
			minimum_company_currency=flt(flt(minimum) * rate, self.precision)
			if minimum is not None
			else None,
			current_company_currency=flt(flt(position.current_balance) * rate, self.precision),
			company_currency=self.company_currency,
		)

	def get_columns(self) -> list[dict]:
		"""Which bank, how bad, how much today first (design 7e §6.2); Bank and Currency at the end."""

		def amount(fieldname, label, options="currency", width=140):
			return column(fieldname, label, "Currency", width, options=options)

		return [
			column(
				"bank_account", _("Bank Account", context="Treasury"), "Link", 200, options="Bank Account"
			),
			column("level_label", _("Level", context=CTX), "Data", 110),
			amount("current_balance", _("Current Balance")),
			amount("lowest_balance", _("Lowest Projected")),
			column("lowest_date", _("Lowest On"), "Date", 100),
			column("first_breach", _("First Breach"), "Date", 100),
			amount("shortfall", _("Shortfall"), width=120),
			amount("closing_balance", _("Closing at Horizon")),
			amount("warning_balance", _("Warning Balance"), width=120),
			amount("minimum_balance", _("Minimum Balance"), width=120),
			amount("lowest_company_currency", _("Lowest (Company Currency)"), "company_currency", 150),
			column("bank", _("Bank", context="Treasury"), "Link", 120, options="Bank"),
			column("currency", _("Currency"), "Link", 70, options="Currency"),
			column("level", _("Level", context=CTX), "Data", 80, hidden=1),
			column(
				"company_currency",
				_("Company Currency", context="Treasury Report"),
				"Link",
				80,
				options="Currency",
				hidden=1,
			),
		]

	def get_chart(self, data) -> dict:
		"""Bars: the lowest projected balance; line: the minimum balance (company currency). A bar below the
		line is a bank in breach, readable without colour (design 7e P19). No threshold: no point."""
		return {
			"data": {
				"labels": [r.bank_account for r in data],
				"datasets": [
					{
						"name": _("Lowest Projected Balance"),
						"chartType": "bar",
						"values": [r.lowest_company_currency for r in data],
					},
					{
						"name": _("Minimum Balance"),
						"chartType": "line",
						"values": [r.minimum_company_currency for r in data],
					},
				],
			},
			"type": "axis-mixed",
			"colors": ["#5E64FF", "#E24C4C"],
			"fieldtype": "Currency",
			"options": self.company_currency,
		}

	def get_summary(self, data) -> list[dict]:
		below_minimum = sum(1 for r in data if r.level == LEVELS[0])
		below_warning = sum(1 for r in data if r.level == LEVELS[1])
		total = flt(sum(r.current_company_currency for r in data), self.precision)
		cards = [
			{
				"value": below_minimum,
				"label": _("Banks Below Minimum"),
				"datatype": "Int",
				"indicator": "Red" if below_minimum else "Green",
			},
			{
				"value": below_warning,
				"label": _("Banks Below Warning"),
				"datatype": "Int",
				"indicator": "Orange" if below_warning else "Green",
			},
			{
				"value": total,
				"label": _("Current Balance"),
				"datatype": "Currency",
				"currency": self.company_currency,
				"indicator": "Blue",
			},
		]
		# design 7e P20: the worst bank at a glance (in its own currency); hidden without a shortfall
		worst = max(data, key=lambda r: flt(r.shortfall) * self.rate(r.currency), default=None)
		if worst and flt(worst.shortfall) > 0:
			cards.append(
				{
					"value": f"{worst.bank_account} · {fmt_money(worst.shortfall, currency=worst.currency)}",
					"label": _("Largest Shortfall"),
					"datatype": "Data",
					"indicator": "Red",
				}
			)
		return cards
