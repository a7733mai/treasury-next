# Copyright (c) 2026, Ahmed Abdelmageed and contributors
# For license information, please see license.txt

"""Security Cheque Movements (spec 7f §3.6): every status and custody movement of the security cheques in a
period, from their status and custody logs.

A movement is in the period by its event date, or, for an event without one (e.g. Expiry Extended, Created),
by the date it was recorded. A custody log row is shown on the status row it belongs to (Stored, Custody
Transferred, Returned, Encashed, Lost: the next such row of the cheque); a custody row without one is a row of
its own, "Custody Moved". Only the security cheques the user may read are listed.
"""

import frappe
from frappe import _
from frappe.utils import add_days, get_datetime, getdate

from treasury.pdc.report_utils import (
	as_list,
	column,
	validate_company,
	validate_date_range,
	validate_party,
)
from treasury.security_cheques.report_utils import CTX, DOCTYPE, get_security_cheques, link_party_type

STATUS_LOG = "Security Cheque Status Log"
CUSTODY_LOG = "Security Cheque Custody Log"
CUSTODY_MOVED = "Custody Moved"  # a report-only event: a custody row without its status row
CUSTODY_EVENTS = ("Stored", "Custody Transferred", "Returned", "Encashed", "Lost")
EVENTS = (
	"Created",
	"Received",
	"Stored",
	"Custody Transferred",
	"Issued",
	"Returned",
	"Lost",
	"Cancelled",
	"Encashed",
	"Expiry Extended",
	CUSTODY_MOVED,
)
MOVEMENT_STATUS, MOVEMENT_CUSTODY = "Status", "Custody"
CHUNK = 1000
CHEQUE_FIELDS = [
	"name",
	"direction",
	"party_type",
	"party",
	"party_name",
	"purpose",
	"cheque_no",
	"amount_type",
	"amount",
	"currency",
]


def execute(filters=None):
	filters = frappe._dict(filters or {})
	company = validate_company(filters.company, DOCTYPE)
	if not (filters.from_date and filters.to_date):
		frappe.throw(_("From Date and To Date are required."))
	validate_date_range(filters.from_date, filters.to_date)
	if filters.party:
		validate_party(filters.party_type, filters.party)
	events = as_list(filters.event)
	if invalid := [e for e in events if e not in EVENTS]:
		frappe.throw(_("Invalid Event: {0}").format(", ".join(invalid)))
	if filters.movement and filters.movement not in (MOVEMENT_STATUS, MOVEMENT_CUSTODY):
		frappe.throw(_("Invalid Movement: {0}").format(filters.movement))

	cheques = {
		c.name: c
		for c in get_security_cheques(get_filters(filters, company), fields=CHEQUE_FIELDS, order_by="name asc")
	}
	names = list(cheques)
	status_rows, custody_rows = [], []
	for start in range(0, len(names), CHUNK):
		chunk = names[start : start + CHUNK]
		status_rows += get_log_rows(STATUS_LOG, chunk, filters.from_date, filters.to_date)
		custody_rows += get_log_rows(CUSTODY_LOG, chunk, filters.from_date, filters.to_date)

	rows = [make_row(movement, cheques[movement.parent]) for movement in merge(status_rows, custody_rows)]
	rows = [row for row in rows if matches(row, filters, events)]
	rows.sort(key=lambda r: (getdate(r.movement_date), get_datetime(r.datetime)))
	return get_columns(), rows, None, None, get_summary(rows)


def get_filters(filters: frappe._dict, company: str) -> list:
	conditions = [["company", "=", company]]
	for fieldname in ("direction", "party_type", "party"):
		if filters.get(fieldname):
			conditions.append([fieldname, "=", filters.get(fieldname)])
	return conditions


def get_log_rows(doctype: str, parents: list[str], from_date, to_date) -> list[frappe._dict]:
	"""Log rows of `parents` in the period: by event date, or by the recording date without one."""
	if not parents:
		return []
	log = frappe.qb.DocType(doctype)
	from_date, to_date = getdate(from_date), getdate(to_date)
	fields = [log.name, log.parent, log.idx, log.datetime, log.event_date, log.user, log.remarks]
	if doctype == STATUS_LOG:
		fields += [log.event, log.from_status, log.to_status, log.pdc_cheque]
	else:
		fields += [log.from_location, log.to_location, log.from_custodian, log.to_custodian]
	in_period = ((log.event_date >= from_date) & (log.event_date <= to_date)) | (
		log.event_date.isnull()
		& (log.datetime >= get_datetime(from_date))
		& (log.datetime < get_datetime(add_days(to_date, 1)))
	)
	rows = (
		frappe.qb.from_(log)
		.select(*fields)
		.where((log.parenttype == DOCTYPE) & log.parent.isin(parents) & in_period)
		.run(as_dict=True)
	)
	for row in rows:
		row.kind = "status" if doctype == STATUS_LOG else "custody"
	return rows


def merge(status_rows: list, custody_rows: list) -> list[frappe._dict]:
	"""The status rows, each custody row on the next status row of its cheque that moves the custody; the
	custody rows without one as "Custody Moved" rows."""
	by_parent: dict[str, list] = {}
	for row in (*status_rows, *custody_rows):
		by_parent.setdefault(row.parent, []).append(row)
	result = []
	for rows in by_parent.values():
		# a custody row is written just before its status row (same action)
		rows.sort(key=lambda r: (get_datetime(r.datetime), 0 if r.kind == "custody" else 1, r.idx))
		pending = []
		for row in rows:
			if row.kind == "custody":
				pending.append(row)
				continue
			if pending and row.event in CUSTODY_EVENTS:
				custody = pending.pop(0)
				for fieldname in ("from_location", "to_location", "from_custodian", "to_custodian"):
					row[fieldname] = custody[fieldname]
			result.append(row)
		for custody in pending:
			custody.event = CUSTODY_MOVED
			result.append(custody)
	return result


def make_row(movement: frappe._dict, cheque: frappe._dict) -> frappe._dict:
	return frappe._dict(
		movement_date=movement.event_date or getdate(movement.datetime),
		datetime=movement.datetime,
		security_cheque=cheque.name,
		direction=cheque.direction,
		party_type=link_party_type(cheque.party_type),
		party=cheque.party,
		party_name=cheque.party_name,
		purpose=cheque.purpose,
		cheque_no=cheque.cheque_no,
		amount_type=cheque.amount_type,
		amount=cheque.amount,
		currency=cheque.currency,
		event=movement.event,
		from_status=movement.get("from_status"),
		to_status=movement.get("to_status"),
		from_location=movement.get("from_location"),
		to_location=movement.get("to_location"),
		from_custodian=movement.get("from_custodian"),
		to_custodian=movement.get("to_custodian"),
		pdc_cheque=movement.get("pdc_cheque"),
		user=movement.user,
		remarks=movement.remarks,
	)


def is_custody(row) -> bool:
	return any(row.get(f) for f in ("from_location", "to_location", "from_custodian", "to_custodian"))


def matches(row: frappe._dict, filters: frappe._dict, events: list[str]) -> bool:
	if events and row.event not in events:
		return False
	if filters.movement == MOVEMENT_CUSTODY and not is_custody(row):
		return False
	if filters.movement == MOVEMENT_STATUS and row.event == CUSTODY_MOVED:
		return False
	if filters.custody_location and filters.custody_location not in (row.from_location, row.to_location):
		return False
	if filters.custodian and filters.custodian not in (row.from_custodian, row.to_custodian):
		return False
	if filters.user and row.user != filters.user:
		return False
	return True


def get_summary(rows: list) -> list[dict]:
	counts: dict[str, int] = {}
	for row in rows:
		counts[row.event] = counts.get(row.event, 0) + 1
	return [
		{"value": counts[event], "label": _(event, context=DOCTYPE), "datatype": "Int"}
		for event in EVENTS
		if event in counts
	]


def get_columns() -> list[dict]:
	return [
		column("movement_date", _("Date", context="Treasury"), "Date", 100),
		column("datetime", _("Date and Time"), "Datetime", 150),
		column("security_cheque", _("Security Cheque"), "Link", 160, options=DOCTYPE),
		column("direction", _("Direction"), width=90),
		column("party_type", _("Party Type"), width=90, hidden=1),
		column("party", _("Party", context=CTX), "Dynamic Link", 150, options="party_type"),
		column("party_name", _("Party Name"), width=160),
		column("purpose", _("Purpose", context="Security Cheque"), width=130),
		column("cheque_no", _("Cheque No")),
		column("amount", _("Amount", context="Treasury"), "Currency", 120, options="currency"),
		column("currency", _("Currency"), "Link", 70, options="Currency"),
		column("event", _("Event", context="Security Cheque"), width=140),
		column("from_status", _("From Status"), width=100),
		column("to_status", _("To Status"), width=100),
		column("from_location", _("From Location"), "Link", 130, options="Custody Location"),
		column("to_location", _("To Location"), "Link", 130, options="Custody Location"),
		column("from_custodian", _("From Custodian"), "Link", 130, options="User"),
		column("to_custodian", _("To Custodian"), "Link", 130, options="User"),
		column("pdc_cheque", _("PDC Cheque"), "Link", 150, options="PDC Cheque"),
		column("user", _("User", context=CTX), "Link", 130, options="User"),
		column("remarks", _("Remarks", context="Treasury"), width=200),
	]
