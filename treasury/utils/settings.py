"""Raw access to Single DocType values.

`frappe.db.get_single_value` casts a value that was never stored to the field type's empty value (0 for an
Int), and `bench migrate` does not store the default of a Single field added after install. These helpers
tell "never stored" apart from an explicit value.
"""

import frappe
from frappe.model import no_value_fields


def stored_single_value(doctype: str, fieldname: str) -> str | None:
	"""The raw value stored for a Single field; None when the field has no row in `tabSingles` (or a NULL)."""
	singles = frappe.qb.DocType("Singles")
	rows = (
		frappe.qb.from_(singles)
		.select(singles.value)
		.where((singles.doctype == doctype) & (singles.field == fieldname))
		.limit(1)
		.run()
	)
	return rows[0][0] if rows else None


def set_missing_single_defaults(doctype: str) -> list[str]:
	"""Store the JSON default of every field of the Single `doctype` that has no stored value yet.

	Values already stored (an explicit 0 included) are never touched. Returns the fields that were set.
	"""
	updated = []
	for df in frappe.get_meta(doctype).fields:
		if df.default in (None, "") or df.fieldtype in no_value_fields:
			continue
		if stored_single_value(doctype, df.fieldname) is None:
			frappe.db.set_single_value(doctype, df.fieldname, df.default, update_modified=False)
			updated.append(df.fieldname)
	return updated
