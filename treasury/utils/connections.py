"""Connections dashboards of the Treasury doctypes (`<doctype>_dashboard.get_data`, spec 7c §5.3).

`vouchers=True` adds the "Accounting" group: the Payment Entries / Journal Entries posted for the document,
found through their read-only source fields (`treasury_source_doctype` / `treasury_source_name`).
References to invoices / orders / Journal Entries are not dashboard items (they are Dynamic Links): they are
reached through the References table of the document.
"""

from frappe import _

SOURCE_NAME_FIELD = "treasury_source_name"
SOURCE_DOCTYPE_FIELD = "treasury_source_doctype"


def dashboard(
	doctype: str,
	fieldname: str,
	groups: list[tuple[str, list[str]]],
	internal_links: dict | None = None,
	non_standard_fieldnames: dict | None = None,
	vouchers: bool = False,
) -> dict:
	data = {
		"fieldname": fieldname,
		"non_standard_fieldnames": dict(non_standard_fieldnames or {}),
		"internal_links": dict(internal_links or {}),
		"dynamic_links": {},
		"transactions": [{"label": _(label), "items": list(items)} for label, items in groups],
	}
	if vouchers:
		for voucher_doctype in ("Payment Entry", "Journal Entry"):
			data["non_standard_fieldnames"][voucher_doctype] = SOURCE_NAME_FIELD
		data["dynamic_links"][SOURCE_NAME_FIELD] = [doctype, SOURCE_DOCTYPE_FIELD]
		data["transactions"].append(
			{"label": _("Accounting", context="Treasury"), "items": ["Payment Entry", "Journal Entry"]}
		)
	return data
