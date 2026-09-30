# Copyright (c) 2026, Ahmed Abdelmageed and contributors
# For license information, please see license.txt

"""Bank Charges Analysis (spec 7d §3.7): every bank charge booked by Treasury in a period — Bank Charges and
their refunds, transfer fees, cheque clearance charges, bounce charges recovered, LG / LC commissions and
charges, facility fees (treasury.bank_transfers.charge_sources) — per month, bank, bank account, category or
source, or voucher by voucher (Detail), in the company currency.

The charge of a voucher is the net (debit - credit) of its non-cancelled GL rows on Expense accounts other than
the company's Exchange Gain / Loss and Round Off accounts: reversals, refunds and recoveries come out negative,
and a voucher with a net of 0 is left out. A fixed number of queries whatever the number of vouchers: the GL
rows (one query per voucher doctype), the source documents (one `frappe.get_list` per source doctype, so the
user's permissions and User Permissions apply: a voucher whose source the user may not read is left out) and
their Bank Accounts.
"""

import frappe
from frappe import _
from frappe.query_builder.functions import Sum
from frappe.utils import flt, fmt_money, get_first_day, getdate

from treasury.bank_transfers.charge_sources import SOURCES, event_sources
from treasury.pdc.report_utils import CTX, column, report_today, validate_company, validate_date_range
from treasury.utils.voucher_summary import event_label

DOCTYPE = "Bank Charge"
# translation context of the report's own labels: "Transfer Fees" is also a category name (design 7d §7)
REPORT = "Bank Charges Analysis"
TREASURY = "Treasury"
BANK_CHARGES = "Bank Charges"
VOUCHER_DOCTYPES = ("Payment Entry", "Journal Entry")
MONTH, BANK, BANK_ACCOUNT, CATEGORY, SOURCE = "Month", "Bank", "Bank Account", "Category", "Source"
GROUPS = (MONTH, BANK, BANK_ACCOUNT, CATEGORY, SOURCE)
SUMMARY, DETAIL = "Summary", "Detail"
CHUNK = 500
# chart colour of each source (SOURCES order), stable whatever the sources present (design 7d §5.4)
SOURCE_COLORS = {
	"Bank Charges": "#f8814f",
	"Transfer Fees": "#318ad8",
	"Cheque Clearance Charges": "#9c4dd8",
	"Bounce Charges Recovered": "#48bb74",
	"LG Commissions": "#1fc1c3",
	"LC Commissions and Charges": "#e5539b",
	"Facility Fees": "#ecad4b",
}
NET_COLOR = SOURCE_COLORS["Bank Charges"]


def execute(filters=None):
	filters = frappe._dict(filters or {})
	company = validate_company(filters.company, DOCTYPE)
	today = report_today()
	from_date = getdate(filters.from_date or get_first_day(today))
	to_date = getdate(filters.to_date or today)
	validate_date_range(from_date, to_date)
	group_by = filters.group_by or MONTH
	if group_by not in GROUPS:
		frappe.throw(
			_("Group By must be one of: {0}.").format(", ".join(_(g, context=CTX) for g in GROUPS))
		)
	view = filters.view or SUMMARY
	if view not in (SUMMARY, DETAIL):
		frappe.throw(_("View must be Summary or Detail."))
	if filters.source and filters.source not in SOURCES:
		frappe.throw(_("Unknown source {0}.").format(frappe.bold(filters.source)))

	currency = frappe.get_cached_value("Company", company, "default_currency")
	rows = get_rows(company, from_date, to_date, filters)
	summary = report_summary(rows, currency)
	if view == DETAIL:
		data = detail_rows(rows, currency)
		return detail_columns(), data, None, None, summary
	data = group_rows(rows, group_by, currency)
	chart = get_chart(rows, data, group_by, currency)
	total = total_row(data, currency)
	return summary_columns(), [*data, total] if data else [], None, chart, summary


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------


def allowed_sources(filters) -> dict[str, tuple]:
	"""The SOURCES the filters keep (a category keeps Bank Charges only) whose doctype the user may read."""
	result = {}
	for label, (doctype, events) in SOURCES.items():
		if filters.source and filters.source != label:
			continue
		if filters.category and label != BANK_CHARGES:
			continue
		if frappe.has_permission(doctype, "read"):
			result[label] = (doctype, events)
	return result


def get_rows(company: str, from_date, to_date, filters) -> list[frappe._dict]:
	"""One row per voucher (net charge, source, document, bank account, category) the user may see."""
	sources = allowed_sources(filters)
	if not sources:
		return []
	labels = {event: label for event, label in event_sources().items() if label in sources}
	doctypes = {doctype for doctype, _events in sources.values()}
	vouchers = gl_amounts(company, from_date, to_date, list(labels), list(doctypes))
	documents = source_documents(vouchers, company, filters)
	banks = bank_of(documents)
	precision = frappe.get_precision("GL Entry", "debit") or 2

	rows = []
	for voucher in vouchers:
		document = documents.get((voucher.treasury_source_doctype, voucher.treasury_source_name))
		amount = flt(voucher.amount, precision)
		if not document or not amount:
			continue
		source = labels.get(voucher.treasury_event)
		if voucher.treasury_source_doctype != sources[source][0]:
			continue
		bank = banks.get(document.bank_account)
		if filters.bank and bank != filters.bank:
			continue
		category = document.get("category") if source == BANK_CHARGES else None
		if filters.category and category != filters.category:
			continue
		rows.append(
			frappe._dict(
				posting_date=getdate(voucher.posting_date),
				source=source,
				category=category,
				bank=bank,
				bank_account=document.bank_account,
				voucher_type=voucher.voucher_type,
				voucher_no=voucher.voucher_no,
				document_type=voucher.treasury_source_doctype,
				document=voucher.treasury_source_name,
				event=voucher.treasury_event,
				amount=amount,
			)
		)
	rows.sort(key=lambda r: (r.posting_date, r.voucher_no))
	return rows


def gl_amounts(company: str, from_date, to_date, events: list[str], doctypes: list[str]) -> list[frappe._dict]:
	"""Net Expense amount per Treasury voucher of `events` posted in the period (one query per doctype)."""
	if not events:
		return []
	excluded = [
		a
		for a in frappe.get_cached_value(
			"Company", company, ["exchange_gain_loss_account", "round_off_account"]
		)
		or []
		if a
	]
	gl = frappe.qb.DocType("GL Entry")
	account = frappe.qb.DocType("Account")
	result = []
	for voucher_type in VOUCHER_DOCTYPES:
		voucher = frappe.qb.DocType(voucher_type)
		query = (
			frappe.qb.from_(gl)
			.inner_join(account)
			.on(account.name == gl.account)
			.inner_join(voucher)
			.on(voucher.name == gl.voucher_no)
			.select(
				gl.voucher_no,
				gl.posting_date,
				voucher.treasury_source_doctype,
				voucher.treasury_source_name,
				voucher.treasury_event,
				Sum(gl.debit - gl.credit).as_("amount"),
			)
			.where(
				(gl.voucher_type == voucher_type)
				& (gl.company == company)
				& (gl.is_cancelled == 0)
				& (gl.posting_date >= from_date)
				& (gl.posting_date <= to_date)
				& (account.root_type == "Expense")
				& voucher.treasury_event.isin(events)
				& voucher.treasury_source_doctype.isin(doctypes)
			)
			.groupby(
				gl.voucher_no,
				gl.posting_date,
				voucher.treasury_source_doctype,
				voucher.treasury_source_name,
				voucher.treasury_event,
			)
		)
		if excluded:
			query = query.where(gl.account.notin(excluded))
		for row in query.run(as_dict=True):
			row.voucher_type = voucher_type
			result.append(row)
	return result


def source_documents(vouchers: list, company: str, filters) -> dict[tuple[str, str], frappe._dict]:
	"""{(doctype, name): document} of the vouchers' sources the user may read (one get_list per doctype and
	500 names), in the company and the Bank Account filter."""
	names: dict[str, set] = {}
	for voucher in vouchers:
		names.setdefault(voucher.treasury_source_doctype, set()).add(voucher.treasury_source_name)
	documents = {}
	for doctype, doc_names in names.items():
		fields = ["name", "bank_account"]
		if doctype == DOCTYPE:
			fields += ["category", "entry_type"]
		doc_names = sorted(doc_names)
		for start in range(0, len(doc_names), CHUNK):
			conditions = {"name": ["in", doc_names[start : start + CHUNK]], "company": company}
			if filters.bank_account:
				conditions["bank_account"] = filters.bank_account
			for doc in frappe.get_list(doctype, filters=conditions, fields=fields, limit_page_length=0):
				documents[(doctype, doc.name)] = doc
	return documents


def bank_of(documents: dict) -> dict[str, str]:
	"""{bank account: its Bank}."""
	accounts = sorted({d.bank_account for d in documents.values() if d.bank_account})
	if not accounts:
		return {}
	return {
		row.name: row.bank
		for row in frappe.get_all("Bank Account", filters={"name": ["in", accounts]}, fields=["name", "bank"])
	}


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------


def source_label(source: str) -> str:
	return _(source, context=REPORT)


def group_key(row, group_by: str) -> tuple[str, str, str]:
	"""(key, label, filter fieldname of the drill-down) of a row in the grouping `group_by`."""
	if group_by == MONTH:
		key = row.posting_date.strftime("%Y-%m")
		return key, key, "month"
	if group_by == BANK:
		return row.bank or "-", row.bank or "-", "bank"
	if group_by == BANK_ACCOUNT:
		return row.bank_account or "-", row.bank_account or "-", "bank_account"
	if group_by == CATEGORY and row.category:
		return row.category, _(row.category), "category"
	return row.source, source_label(row.source), "source"


def group_rows(rows: list, group_by: str, currency: str) -> list[frappe._dict]:
	precision = frappe.get_precision("GL Entry", "debit") or 2
	groups: dict[tuple, frappe._dict] = {}
	for row in rows:
		key, label, fieldname = group_key(row, group_by)
		group = groups.setdefault(
			(fieldname, key),
			frappe._dict(
				group=label,
				group_value=key,
				group_filter=fieldname,
				vouchers=0,
				charges=0.0,
				refunds=0.0,
				company_currency=currency,
			),
		)
		group.vouchers += 1
		if row.amount > 0:
			group.charges += row.amount
		else:
			group.refunds -= row.amount
	data = list(groups.values())
	total_net = sum(g.charges - g.refunds for g in data)
	for group in data:
		finish(group, precision, total_net)
	if group_by == MONTH:
		data.sort(key=lambda g: g.group_value)
	else:
		data.sort(key=lambda g: (-g.net, g.group))
	return data


def finish(group: frappe._dict, precision: int, total_net: float):
	group.charges = flt(group.charges, precision)
	group.refunds = flt(group.refunds, precision)
	group.net = flt(group.charges - group.refunds, precision)
	group.share = flt(group.net / total_net * 100, 2) if flt(total_net, precision) else 0


def total_row(data: list, currency: str) -> frappe._dict:
	total = frappe._dict(
		group=_("Total", context=CTX),
		vouchers=sum(g.vouchers for g in data),
		charges=sum(g.charges for g in data),
		refunds=sum(g.refunds for g in data),
		is_total=1,
		bold=1,
		company_currency=currency,
	)
	precision = frappe.get_precision("GL Entry", "debit") or 2
	finish(total, precision, total.charges - total.refunds)
	return total


def detail_rows(rows: list, currency: str) -> list[frappe._dict]:
	return [
		frappe._dict(
			posting_date=row.posting_date,
			source=source_label(row.source),
			source_value=row.source,
			category=row.category,
			bank=row.bank,
			bank_account=row.bank_account,
			voucher_type=row.voucher_type,
			voucher_no=row.voucher_no,
			document_type=row.document_type,
			document=row.document,
			event=event_label(row.event),
			amount=row.amount,
			company_currency=currency,
		)
		for row in rows
	]


def report_summary(rows: list, currency: str) -> list[dict]:
	precision = frappe.get_precision("GL Entry", "debit") or 2
	charges = flt(sum(r.amount for r in rows if r.amount > 0), precision)
	refunds = flt(-sum(r.amount for r in rows if r.amount < 0), precision)
	net = flt(charges - refunds, precision)

	def money(label, value, indicator="Blue"):
		return {"label": label, "value": value, "datatype": "Currency", "currency": currency, "indicator": indicator}

	summary = [
		money(_("Charges", context=REPORT), charges, "Orange"),
		money(_("Refunds and Recoveries", context=REPORT), refunds, "Green"),
		money(_("Net Charges", context=REPORT), net, "Red" if net > 0 else "Green"),
		{"label": _("Vouchers", context=REPORT), "value": len(rows), "datatype": "Int", "indicator": "Blue"},
	]
	# design 7d P18: the largest net by category (a source label for the other sources) and by source
	tops = ((_("Top Category", context=REPORT), CATEGORY), (_("Top Source", context=REPORT), SOURCE))
	for label, group_by in tops:
		top = top_group(rows, group_by, currency, precision)
		if top:
			summary.append({"label": label, "value": top, "datatype": "Data", "indicator": "Blue"})
	return summary


def top_group(rows: list, group_by: str, currency: str, precision: int) -> str | None:
	"""`Group · net (share %)` of the group with the largest net charges, if any charges."""
	groups = group_rows(rows, group_by, currency)
	if not groups or groups[0].net <= 0:
		return None
	top = groups[0]
	return f"{top.group} · {fmt_money(top.net, precision)} ({flt(top.share, 1)}%)"


def get_chart(rows: list, data: list, group_by: str, currency: str) -> dict | None:
	if not data:
		return None
	labels = [g.group for g in data]
	if group_by == MONTH:
		months = [g.group_value for g in data]
		sources = [s for s in SOURCES if any(r.source == s for r in rows)]
		datasets = []
		precision = frappe.get_precision("GL Entry", "debit") or 2
		for source in sources:
			net = dict.fromkeys(months, 0.0)
			for row in rows:
				if row.source == source:
					net[row.posting_date.strftime("%Y-%m")] += row.amount
			datasets.append({"name": source_label(source), "values": [flt(net[m], precision) for m in months]})
		return {
			"data": {"labels": labels, "datasets": datasets},
			"type": "bar",
			"barOptions": {"stacked": 1},
			"colors": [SOURCE_COLORS.get(source, NET_COLOR) for source in sources],
			"fieldtype": "Currency",
			"options": currency,
		}
	return {
		"data": {
			"labels": labels,
			"datasets": [{"name": _("Net Charges", context=REPORT), "values": [g.net for g in data]}],
		},
		"type": "bar",
		"colors": [NET_COLOR],
		"fieldtype": "Currency",
		"options": currency,
	}


def money_column(fieldname: str, label: str, width: int = 150) -> dict:
	return column(fieldname, label, "Currency", width, options="company_currency")


def currency_column() -> dict:
	return column("company_currency", _("Company Currency", context=CTX), "Link", 80, options="Currency", hidden=1)


def summary_columns() -> list[dict]:
	return [
		column("group", _("Group", context=CTX), width=220),
		column("vouchers", _("Vouchers", context=REPORT), "Int", 90),
		money_column("charges", _("Charges", context=REPORT), 140),
		money_column("refunds", _("Refunds and Recoveries", context=REPORT), 160),
		money_column("net", _("Net", context=REPORT), 140),
		column("share", _("Share %", context=REPORT), "Percent", 150),
		column("group_value", _("Group", context=CTX), hidden=1),
		column("group_filter", _("Group By", context=CTX), hidden=1),
		currency_column(),
	]


def detail_columns() -> list[dict]:
	return [
		column("posting_date", _("Posting Date", context=TREASURY), "Date", 100),
		column("source", _("Source", context=REPORT), width=170),
		column("category", _("Category", context=REPORT), "Link", 150, options="Bank Charge Category"),
		column("bank", _("Bank", context=TREASURY), "Link", 130, options="Bank"),
		column("bank_account", _("Bank Account", context=TREASURY), "Link", 170, options="Bank Account"),
		column("voucher_type", _("Voucher Type"), "Link", 120, options="DocType", hidden=1),
		column("voucher_no", _("Voucher", context=CTX), "Dynamic Link", 170, options="voucher_type"),
		column("document_type", _("Document Type", context=REPORT), "Link", 130, options="DocType", hidden=1),
		column("document", _("Document", context=REPORT), "Dynamic Link", 160, options="document_type"),
		column("event", _("Event", context=REPORT), width=170),
		money_column("amount", _("Amount", context=REPORT), 130),
		column("source_value", _("Source", context=REPORT), hidden=1),
		currency_column(),
	]
