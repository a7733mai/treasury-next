"""Accounting dimensions on the Treasury documents that carry them to their vouchers.

ERPNext adds a Link field per Accounting Dimension to every doctype listed in the `accounting_dimension_doctypes`
hook (merged across apps): Treasury lists PDC Cheque, Cheque Batch and Bank Transfer there (hooks.py), whose
JSON has the
`accounting_dimensions_section` / `dimension_col_break` fields ERPNext inserts them after. Dimensions created
after the app are added by ERPNext itself; the ones that existed before are added by `create_dimension_fields`
(install). Their values are copied to every Payment Entry / Journal Entry built from the cheque.
"""

import frappe
from erpnext.accounts.doctype.accounting_dimension.accounting_dimension import (
	create_accounting_dimensions_for_doctype,
	get_accounting_dimensions,
	get_dimensions,
)

TREASURY_DIMENSION_DOCTYPES = ("PDC Cheque", "Cheque Batch", "Bank Transfer")


def get_dimension_fieldnames() -> list[str]:
	"""Fieldnames of the enabled Accounting Dimensions."""
	return get_accounting_dimensions()


def get_dimension_values(doc) -> dict:
	"""{fieldname: value} of the enabled dimensions that exist on `doc`."""
	meta = frappe.get_meta(doc.doctype)
	return {f: doc.get(f) for f in get_dimension_fieldnames() if meta.has_field(f)}


def copy_dimensions(source, target):
	"""Copy the dimension values of `source` to `target` (fields that exist on both)."""
	source_meta, target_meta = frappe.get_meta(source.doctype), frappe.get_meta(target.doctype)
	for fieldname in get_dimension_fieldnames():
		if source_meta.has_field(fieldname) and target_meta.has_field(fieldname):
			target.set(fieldname, source.get(fieldname))


def default_dimensions(company: str) -> dict:
	"""{fieldname: default value} of the dimensions for `company` (Accounting Dimension Detail)."""
	return get_dimensions()[1].get(company, {}) if company else {}


def create_dimension_fields():
	"""Add the fields of the existing Accounting Dimensions to the Treasury doctypes (idempotent)."""
	for doctype in TREASURY_DIMENSION_DOCTYPES:
		create_accounting_dimensions_for_doctype(doctype)


def dimension_custom_fields() -> list[tuple[str, str]]:
	"""(doctype, fieldname) of the Custom Fields of the Treasury doctypes that are Accounting Dimension fields."""
	fieldnames = frappe.get_all("Accounting Dimension", pluck="fieldname")
	if not fieldnames:
		return []
	return [
		(row.dt, row.fieldname)
		for row in frappe.get_all(
			"Custom Field",
			filters={"dt": ["in", TREASURY_DIMENSION_DOCTYPES], "fieldname": ["in", fieldnames]},
			fields=["dt", "fieldname"],
		)
	]
