"""Comparison helpers for fields only a Treasury lifecycle service may change.

A lifecycle saves with `flags.treasury_lifecycle`; a controller compares the other saves with the document
before the save (PDCCheque / SecurityCheque.validate_protected_fields).
"""

import frappe
from frappe.model import no_value_fields
from frappe.utils import cint, flt, get_datetime, getdate


def normalize(value, fieldtype: str):
	if fieldtype == "Date":
		return getdate(value) if value else None
	if fieldtype == "Datetime":
		return get_datetime(value) if value else None
	if fieldtype in ("Int", "Check"):
		return cint(value)
	if fieldtype in ("Currency", "Float", "Percent"):
		return flt(value, 9)
	return value or None


def changed_fields(doc, before, fieldnames) -> list[str]:
	"""The fields among `fieldnames` whose (normalized) value differs between `before` and `doc`."""
	return [
		f
		for f in fieldnames
		if normalize(before.get(f), doc.meta.get_field(f).fieldtype)
		!= normalize(doc.get(f), doc.meta.get_field(f).fieldtype)
	]


def table_key(doc, table: str) -> list[tuple]:
	"""The rows of a child table as comparable tuples of their (normalized) values."""
	fields = [
		df
		for df in frappe.get_meta(doc.meta.get_field(table).options).fields
		if df.fieldtype not in no_value_fields
	]
	return [tuple(normalize(row.get(df.fieldname), df.fieldtype) for df in fields) for row in doc.get(table)]


def changed_tables(doc, before, tables, lifecycle_save: bool) -> list[str]:
	"""Append-only tables: existing rows never change, and only a lifecycle save appends rows."""
	changed = []
	for table in tables:
		old_rows, new_rows = table_key(before, table), table_key(doc, table)
		if new_rows[: len(old_rows)] != old_rows or (len(new_rows) != len(old_rows) and not lifecycle_save):
			changed.append(table)
	return changed
