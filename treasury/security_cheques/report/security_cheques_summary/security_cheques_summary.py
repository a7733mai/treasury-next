# Copyright (c) 2026, Ahmed Abdelmageed and contributors
# For license information, please see license.txt

"""Security Cheques Summary (spec 7f §3.6): security held from and given to others per party, purpose or
status, with the amounts of the live cheques aged by their expiry date.

Amounts are in the company currency (report_utils.AmountConverter at the As On date) and only for Fixed
cheques; an Open / Blank cheque is counted, never summed. The ageing buckets hold the live cheques (held /
given) by days from As On to the expiry date: Expired (< 0), the ranges, beyond the last range and No Expiry.
Cheques in a final status (returned, encashed, lost, cancelled) are listed only when selected, in Closed
Amount. Held from others and given to others are never added together.
"""

import frappe
from frappe import _
from frappe.utils import cint, date_diff, flt, getdate

from treasury.pdc.report_utils import column, get_precision, report_today, validate_company, validate_party
from treasury.security_cheques.lifecycle import (
	FINAL_STATUSES,
	INCOMING,
	LIVE_STATUSES,
	OUTGOING,
)
from treasury.security_cheques.report_utils import (
	CTX,
	DOCTYPE,
	AmountConverter,
	company_currency_column,
	get_security_cheques,
	is_open,
	link_party_type,
	validate_statuses,
)

GROUP_PARTY, GROUP_PURPOSE, GROUP_STATUS = "Party", "Purpose", "Status"
DEFAULT_RANGES = "30, 60, 90"
MAX_RANGES = 5
FIELDS = [
	"name",
	"direction",
	"party_type",
	"party",
	"party_name",
	"purpose",
	"amount_type",
	"amount",
	"currency",
	"expiry_date",
	"status",
]


def execute(filters=None):
	filters = frappe._dict(filters or {})
	company = validate_company(filters.company, DOCTYPE)
	if filters.party:
		validate_party(filters.party_type, filters.party)
	group_by = filters.group_by or GROUP_PARTY
	if group_by not in (GROUP_PARTY, GROUP_PURPOSE, GROUP_STATUS):
		frappe.throw(_("Invalid Group By: {0}").format(group_by))
	ranges = parse_ranges(filters.ageing_ranges)
	as_on = getdate(filters.as_on or report_today())
	statuses = validate_statuses(filters.status) or list(LIVE_STATUSES)

	converter = AmountConverter(company, as_on)
	cheques = get_security_cheques(get_filters(filters, company, statuses), fields=FIELDS)
	buckets = bucket_columns(ranges)
	data = []
	for direction, label in ((INCOMING, _("Held from Others")), (OUTGOING, _("Given to Others"))):
		direction_cheques = [c for c in cheques if c.direction == direction]
		if not direction_cheques:
			continue
		groups: dict[tuple, list] = {}
		for cheque in direction_cheques:
			groups.setdefault(group_key(cheque, group_by), []).append(cheque)
		rows = [
			group_row(key, group_by, items, converter, as_on, ranges, buckets)
			for key, items in sorted(groups.items(), key=lambda item: sort_key(item[0]))
		]
		for row in rows:
			row.direction = direction
		data.append(header_row(direction, label, rows, buckets, converter.company_currency))
		data += rows
	return (
		get_columns(buckets),
		data,
		None,
		get_chart(data, buckets),
		get_summary(cheques, data, converter, as_on),
	)


def parse_ranges(value) -> list[int]:
	"""The ageing ranges ("30, 60, 90"): 1 to 5 increasing whole numbers of days."""
	text = str(value or DEFAULT_RANGES)
	parts = [p.strip() for p in text.split(",") if p.strip()]
	error = _("Ageing ranges must be increasing whole numbers of days, e.g. 30, 60, 90.")
	if not 1 <= len(parts) <= MAX_RANGES or not all(p.isdigit() for p in parts):
		frappe.throw(error)
	ranges = [int(p) for p in parts]
	if ranges[0] <= 0 or any(b <= a for a, b in zip(ranges, ranges[1:], strict=False)):
		frappe.throw(error)
	return ranges


def bucket_columns(ranges: list[int]) -> list[tuple[str, str]]:
	"""(fieldname, label) of the ageing buckets, in order: Expired, 0-r1, r1+1-r2 ..., last+, No Expiry."""
	columns = [("expired", _("Expired", context="Security Cheque"))]
	low = 0
	for index, high in enumerate(ranges, 1):
		columns.append((f"range_{index}", f"{low}-{high}"))
		low = high + 1
	columns.append(("range_last", f"{low}+"))
	columns.append(("no_expiry", _("No Expiry")))
	return columns


def bucket_of(cheque, as_on, ranges: list[int]) -> str:
	if not cheque.expiry_date:
		return "no_expiry"
	days = date_diff(getdate(cheque.expiry_date), as_on)
	if days < 0:
		return "expired"
	for index, high in enumerate(ranges, 1):
		if days <= high:
			return f"range_{index}"
	return "range_last"


def get_filters(filters: frappe._dict, company: str, statuses: list[str]) -> list:
	conditions = [["company", "=", company], ["status", "in", statuses]]
	for fieldname in ("direction", "party_type", "party", "purpose"):
		if filters.get(fieldname):
			conditions.append([fieldname, "=", filters.get(fieldname)])
	return conditions


def group_key(cheque, group_by: str) -> tuple:
	if group_by == GROUP_PURPOSE:
		return (cheque.purpose or "",)
	if group_by == GROUP_STATUS:
		return (cheque.status or "",)
	return (link_party_type(cheque.party_type), cheque.party or "", cheque.party_name or "")


def sort_key(key: tuple) -> tuple:
	"""Groups by party name (then party), purpose or status."""
	return tuple(str(part or "") for part in reversed(key))


def group_row(key, group_by, cheques, converter, as_on, ranges, buckets) -> frappe._dict:
	precision = get_precision()
	row = frappe._dict(indent=1, company_currency=converter.company_currency, group_by=group_by)
	# group_label: the readable first column (design 7f P18); purpose and status are translated by the script
	if group_by == GROUP_PURPOSE:
		row.purpose = row.group_label = key[0]
	elif group_by == GROUP_STATUS:
		row.status = row.group_label = key[0]
	else:
		row.party_type, row.party, row.party_name = key[0], key[1] or None, key[2]
		row.group_label = key[2] or key[1] or ""
	row.fixed_count = len([c for c in cheques if not is_open(c)])
	row.open_count = len([c for c in cheques if is_open(c)])
	for fieldname, _label in buckets:
		row[fieldname] = 0.0
	row.amount = row.closed_amount = 0.0
	for cheque in cheques:
		if is_open(cheque):
			continue
		amount = converter.company_amount(cheque)
		if cheque.status in FINAL_STATUSES:
			row.closed_amount = flt(row.closed_amount + amount, precision)
			continue
		row.amount = flt(row.amount + amount, precision)
		if cheque.status in LIVE_STATUSES:
			bucket = bucket_of(cheque, as_on, ranges)
			row[bucket] = flt(row[bucket] + amount, precision)
	return row


def header_row(direction: str, label: str, rows: list, buckets, company_currency: str) -> frappe._dict:
	precision = get_precision()
	header = frappe._dict(
		is_group=1,
		indent=0,
		direction=direction,
		exposure=label,
		group_label=label,
		company_currency=company_currency,
		fixed_count=sum(cint(r.fixed_count) for r in rows),
		open_count=sum(cint(r.open_count) for r in rows),
	)
	for fieldname in ("amount", "closed_amount", *(f for f, _label in buckets)):
		header[fieldname] = flt(sum(flt(r.get(fieldname)) for r in rows), precision)
	return header


def get_chart(data: list, buckets) -> dict:
	headers = {row.direction: row for row in data if row.get("is_group")}
	datasets = []
	for direction, label in ((INCOMING, _("Held from Others")), (OUTGOING, _("Given to Others"))):
		header = headers.get(direction) or {}
		datasets.append({"name": label, "values": [flt(header.get(f)) for f, _label in buckets]})
	return {
		"data": {"labels": [label for _f, label in buckets], "datasets": datasets},
		"type": "bar",
		"colors": ["#2490EF", "#F68B1F"],
		"barOptions": {"spaceRatio": 0.4},
		"fieldtype": "Currency",
	}


def get_summary(cheques: list, data: list, converter: AmountConverter, as_on) -> list[dict]:
	headers = {row.direction: row for row in data if row.get("is_group")}
	live = [c for c in cheques if c.status in LIVE_STATUSES and c.expiry_date]
	days = [date_diff(getdate(c.expiry_date), as_on) for c in live]
	currency = converter.company_currency
	expired = len([d for d in days if d < 0])
	expiring = len([d for d in days if 0 <= d <= 30])
	return [
		{
			"value": flt(headers[INCOMING].amount) if INCOMING in headers else 0,
			"label": _("Held from Others"),
			"datatype": "Currency",
			"currency": currency,
			"indicator": "Blue",
		},
		{
			"value": flt(headers[OUTGOING].amount) if OUTGOING in headers else 0,
			"label": _("Given to Others"),
			"datatype": "Currency",
			"currency": currency,
			"indicator": "Orange",
		},
		{
			"value": expired,
			"label": _("Expired Not Returned"),
			"datatype": "Int",
			"indicator": "Red" if expired else "Green",
		},
		{
			"value": expiring,
			"label": _("Expiring in 30 Days"),
			"datatype": "Int",
			"indicator": "Orange" if expiring else "Green",
		},
	]


def get_columns(buckets) -> list[dict]:
	"""`group_label` first (the tree toggle and a readable label: Held from / Given to Others, the party, the
	purpose or the status); the keys stay as hidden columns for export (design 7f P18)."""
	return [
		column("group_label", _("Group", context="Security Cheques Summary"), width=240),
		column("fixed_count", _("Fixed", context="Security Cheques Summary"), "Int", 70),
		column("open_count", _("Open / Blank", context="Security Cheques Summary"), "Int", 90),
		column("amount", _("Amount (Company Currency)"), "Currency", 140, options="company_currency"),
		*[
			column(f, label, "Currency", 120 if f == "expired" else 110, options="company_currency")
			for f, label in buckets
		],
		column("closed_amount", _("Closed Amount"), "Currency", 120, options="company_currency"),
		column("exposure", _("Security", context="Cheque Leaf"), width=140, hidden=1),
		column("direction", _("Direction"), width=90, hidden=1),
		column("party_type", _("Party Type"), width=90, hidden=1),
		column("party", _("Party", context=CTX), "Dynamic Link", 150, options="party_type", hidden=1),
		column("party_name", _("Party Name"), width=160, hidden=1),
		column("purpose", _("Purpose", context="Security Cheque"), width=130, hidden=1),
		column("status", _("Status", context="Treasury"), width=100, hidden=1),
		company_currency_column(),
	]
