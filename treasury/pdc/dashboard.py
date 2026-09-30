"""Custom Number Card sources of the Treasury workspace (read-only).

The Document Type cards (counts) filter PDC Cheque directly. Sums need the company currency (a company may
hold cheques in several currencies), so they come from `get_cheque_total`, which reads the cheques with
`frappe.get_list` (permissions and User Permissions apply) and converts them like the reports do.
"""

import erpnext
import frappe
from frappe import _
from frappe.utils import add_days, flt, getdate

from treasury.pdc.report_utils import (
	BANK_POSTED,
	INCOMING,
	OUTGOING,
	AmountConverter,
	get_cheques,
	report_today,
	validate_company,
)

# Cheques still to be paid / collected, as the number cards count them (also in the card JSON filters).
# Deliberately not the Bank Position Forecast's set: a card shows the cheques still in circulation, so an
# outgoing cheque written but not handed over yet (Created) is outstanding, and a cheque the bank already
# cleared (Manual bank posting pending) is not, although the forecast still expects its bank posting.
CARD_STATUSES = {
	OUTGOING: ("Created", "Delivered"),
	INCOMING: ("Received", "Deposited"),
}
DUE_7_DAYS, OUTSTANDING = "due_7_days", "outstanding"
# "Due in 7 days" = due today or within the next 7 days (0 to 7 days from today, both included): the same
# convention as the "0-7 Days" bucket of PDC Maturity.
DUE_WINDOW_DAYS = 7


@frappe.whitelist()
def get_cheque_total(filters: str | dict | None = None) -> dict:
	"""Company currency total of open cheques.

	filters: company (default: the user's default company), direction (Outgoing / Incoming) and scope:
	`due_7_days` (effective due date from today to today + 7) or `outstanding` (all open cheques).
	Without a company and without a default company, the total is 0 (nothing to show, not an error).
	"""
	filters = frappe._dict(frappe.parse_json(filters) or {})
	if filters.direction not in CARD_STATUSES:
		frappe.throw(_("Direction must be one of: {0}.").format(", ".join((_(OUTGOING), _(INCOMING)))))
	if filters.scope not in (DUE_7_DAYS, OUTSTANDING):
		frappe.throw(_("Invalid scope: {0}").format(filters.scope))
	company = filters.company or erpnext.get_default_company()
	if not company:
		return {"value": 0, "fieldtype": "Currency"}
	company = validate_company(company)

	totals = open_cheque_totals(company)
	card = totals[filters.direction][filters.scope]
	return {
		"value": card.amount,
		"fieldtype": "Currency",
		"options": totals.company_currency,
		"route": ["query-report", "PDC Register"],
		"route_options": card.route_options,
	}


def open_cheque_totals(
	company: str, bank_accounts: list[str] | None = None, today=None, converter: AmountConverter | None = None
) -> frappe._dict:
	"""The number-card figures of both directions from one `get_cheques` query (the caller has validated the
	company): {"Outgoing": {"due_7_days": {count, amount, route_options}, "outstanding": {...}}, "Incoming":
	{...}, "company_currency"}. Amounts in the company currency (`converter`, default an AmountConverter at
	`today`); `bank_accounts` narrows to those banks (the route options then carry the bank only when there is
	exactly one)."""
	today = getdate(today or report_today())
	window = [today, getdate(add_days(today, DUE_WINDOW_DAYS))]
	conditions = {
		"company": company,
		"operational_status": ["in", [s for statuses in CARD_STATUSES.values() for s in statuses]],
		"accounting_status": ["!=", BANK_POSTED],
	}
	if bank_accounts:
		conditions["bank_account"] = ["in", list(bank_accounts)]
	converter = converter or AmountConverter(company, today)
	result = frappe._dict(company_currency=converter.company_currency)
	for direction, statuses in CARD_STATUSES.items():
		route_options = {
			"company": company,
			"direction": direction,
			"operational_status": list(statuses),
			"date_based_on": "Effective Due Date",
		}
		if bank_accounts and len(bank_accounts) == 1:
			route_options["bank_account"] = bank_accounts[0]
		result[direction] = frappe._dict(
			{
				DUE_7_DAYS: frappe._dict(
					count=0,
					amount=0.0,
					route_options={**route_options, "from_date": str(window[0]), "to_date": str(window[1])},
				),
				OUTSTANDING: frappe._dict(count=0, amount=0.0, route_options=route_options),
			}
		)
	for cheque in get_cheques(conditions):
		if cheque.operational_status not in CARD_STATUSES.get(cheque.direction, ()):
			continue
		amount = converter.company_amount(cheque)
		cards = result[cheque.direction]
		scopes = [OUTSTANDING]
		if window[0] <= getdate(cheque.effective_due_date) <= window[1]:
			scopes.append(DUE_7_DAYS)
		for scope in scopes:
			cards[scope].count += 1
			cards[scope].amount += amount
	precision = converter.precision
	for direction in CARD_STATUSES:
		for card in result[direction].values():
			card.amount = flt(card.amount, precision)
	return result
