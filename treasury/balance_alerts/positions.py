"""Thresholds and levels of the per-bank projected positions (spec 7e §3.2).

A day is Critical when its projected closing is below the minimum balance, Warning when below the warning
balance, else OK. A position (one GL account, see treasury.pdc.bank_positions) takes the strictest thresholds
of its Bank Accounts (the highest minimum and the highest warning); without any threshold row its level is
"No Threshold" and it has no breaches. Amounts are in the bank account currency.
"""

import frappe
from frappe.utils import cint, flt, getdate

from treasury.pdc.bank_positions import get_bank_positions
from treasury.pdc.report.bank_position_forecast.bank_position_forecast import OUT
from treasury.utils.settings import stored_single_value

WARNING, CRITICAL, OK, NO_THRESHOLD = "Warning", "Critical", "OK", "No Threshold"
LEVELS = (CRITICAL, WARNING, OK, NO_THRESHOLD)  # worst first
THRESHOLD_DOCTYPE = "Treasury Balance Threshold"
SETTINGS = "Treasury Company Settings"
DEFAULT_HORIZON = 7
MAX_HORIZON = 7
# Treasury Settings → Bank Position Alerts: the JSON defaults (used when a value was never stored)
ALERT_SETTING_DEFAULTS = {
	"send_balance_digest": 1,
	"send_balance_alerts": 1,
	"balance_alert_notification": 1,
	"balance_alert_email": 1,
	"balance_digest_hour": 7,
	"digest_zero_days": 1,
}


def get_thresholds(company: str) -> dict[str, frappe._dict]:
	"""{bank_account: {warning_balance, minimum_balance, currency}} of the company's Balance Thresholds."""
	return {
		row.bank_account: row
		for row in frappe.get_all(
			THRESHOLD_DOCTYPE,
			filters={"parenttype": SETTINGS, "parent": company, "parentfield": "balance_thresholds"},
			fields=["bank_account", "warning_balance", "minimum_balance", "currency"],
			order_by="idx asc",
		)
	}


def alert_settings() -> frappe._dict:
	"""The Bank Position Alerts values of Treasury Settings; a value never stored takes the field default."""
	values = frappe._dict()
	for fieldname, default in ALERT_SETTING_DEFAULTS.items():
		value = stored_single_value("Treasury Settings", fieldname)
		values[fieldname] = default if value in (None, "") else cint(value)
	return values


def horizon() -> int:
	"""Treasury Settings `balance_alert_horizon` (DEFAULT_HORIZON when never stored), within 1..MAX_HORIZON."""
	value = stored_single_value("Treasury Settings", "balance_alert_horizon")
	days = DEFAULT_HORIZON if value in (None, "") else cint(value)
	return min(max(days, 1), MAX_HORIZON)


def epsilon(precision: int) -> float:
	"""Half a unit of the last decimal: two amounts closer than this are equal (rounding)."""
	return 0.5 / (10**precision)


def day_level(closing: float, minimum: float, warning: float) -> str:
	if closing < minimum:
		return CRITICAL
	if closing < warning:
		return WARNING
	return OK


def evaluate(company: str, bank_accounts=None, today=None, horizon_days: int | None = None) -> list:
	"""`get_bank_positions` with the thresholds applied: each position gets `minimum`, `warning` (None without
	thresholds), `level` (worst day), `lowest` ({date, closing}: the lowest day, earliest on a tie),
	`first_breach` (date of the first day that is not OK), `breaches` ([{date, level, closing, threshold}])
	and a `level` on every day."""
	positions = get_bank_positions(company, horizon_days or horizon(), bank_accounts, today)
	thresholds = get_thresholds(company)
	for position in positions:
		apply_thresholds(position, [thresholds[b] for b in position.bank_accounts if b in thresholds])
	return positions


def apply_thresholds(position, rows: list):
	position.minimum = max(flt(r.minimum_balance) for r in rows) if rows else None
	position.warning = max(flt(r.warning_balance) for r in rows) if rows else None
	position.breaches = []
	position.first_breach = None
	lowest = None
	for day in position.days:
		if lowest is None or day.closing < lowest.closing:
			lowest = day
		if not rows:
			day.level = NO_THRESHOLD
			continue
		day.level = day_level(day.closing, position.minimum, position.warning)
		if day.level != OK:
			position.breaches.append(
				frappe._dict(
					date=day.date,
					level=day.level,
					closing=day.closing,
					threshold=position.minimum if day.level == CRITICAL else position.warning,
				)
			)
	position.first_breach = position.breaches[0].date if position.breaches else None
	position.lowest = frappe._dict(date=lowest.date, closing=lowest.closing) if lowest else None
	if not rows:
		position.level = NO_THRESHOLD
	elif any(b.level == CRITICAL for b in position.breaches):
		position.level = CRITICAL
	elif position.breaches:
		position.level = WARNING
	else:
		position.level = OK


def level_order(level: str) -> int:
	return LEVELS.index(level) if level in LEVELS else len(LEVELS)


def get_day(position, date) -> frappe._dict | None:
	date = getdate(date)
	return next((d for d in position.days if d.date == date), None)


def top_movements(position, date, n: int = 5) -> list[frappe._dict]:
	"""The largest expected outflows dated up to `date` (overdue ones included), amount descending then date."""
	date = getdate(date)
	flows = [
		f for day in position.days for f in day.flows if f.direction == OUT and getdate(f.date) <= date
	]
	flows.sort(key=lambda f: (-flt(f.amount), getdate(f.date), f.get("reference_name") or ""))
	return [
		frappe._dict(
			date=getdate(f.date),
			amount=flt(f.amount),
			reference_doctype=f.get("reference_doctype"),
			reference_name=f.get("reference_name"),
			party=f.get("party"),
			description=f.get("description"),
		)
		for f in flows[:n]
	]
