"""The Supplier a Bank is booked to on the bank-charge Payment Entries (spec 7d §3.2).

Treasury Settings → Bank Suppliers maps each Bank to a Supplier. `ensure_bank_supplier` returns the mapped one,
or creates (or reuses, by name) a Supplier in the Payee Supplier Group and maps it. The Payment Entry books no
party row (the whole amount is a deduction), so the supplier's balance is never affected.
"""

import frappe
from frappe import _

from treasury.utils.context import elevated, real_user

SETTINGS = "Treasury Settings"
ROW_DOCTYPE = "Treasury Bank Supplier"


def get_bank_supplier(bank: str) -> str | None:
	"""The Supplier mapped to `bank` in Treasury Settings, if any."""
	if not bank:
		return None
	rows = frappe.get_all(
		ROW_DOCTYPE,
		filters={"parenttype": SETTINGS, "parent": SETTINGS, "bank": bank},
		pluck="supplier",
		limit=1,
	)
	return rows[0] if rows else None


def ensure_bank_supplier(bank: str) -> str:
	"""The enabled Supplier of `bank`, created (or found by the Bank's name) and mapped on first use. The
	caller has checked the user's Bank Charge permission; this runs elevated."""
	with elevated():
		return _ensure(bank)


def _ensure(bank: str) -> str:
	group = frappe.db.get_single_value(SETTINGS, "payee_supplier_group")
	# lock the Payee Supplier Group: two concurrent first charges of one bank map one supplier
	is_leaf = bool(group) and frappe.db.get_value("Supplier Group", group, "is_group", for_update=True) == 0

	supplier = get_bank_supplier(bank)
	if supplier and frappe.db.exists("Supplier", supplier):
		if frappe.db.get_value("Supplier", supplier, "disabled"):
			frappe.throw(
				_("Supplier {0} of Bank {1} is disabled (Treasury Settings).").format(
					frappe.bold(supplier), frappe.bold(bank)
				)
			)
		return supplier

	name = frappe.db.get_value("Bank", bank, "bank_name") or bank
	existing = frappe.get_all(
		"Supplier",
		filters={"supplier_name": name, "disabled": 0},
		pluck="name",
		order_by="creation asc",
		limit=1,
	)
	if existing:
		supplier = existing[0]
	else:
		if not is_leaf:
			frappe.throw(
				_("Set a valid Payee Supplier Group in Treasury Settings."), title=_("Missing Settings")
			)
		doc = frappe.get_doc(
			{
				"doctype": "Supplier",
				"supplier_name": name,
				"supplier_group": group,
				"supplier_type": "Company",
			}
		)
		doc.insert(ignore_permissions=True)
		user = real_user()
		frappe.db.set_value("Supplier", doc.name, {"owner": user, "modified_by": user}, update_modified=False)
		supplier = doc.name

	_map(bank, supplier)
	return supplier


def _map(bank: str, supplier: str):
	"""Write the mapping row of `bank` only. Saving the whole Treasury Settings would validate every other
	row and field, so an unrelated problem there (another bank's disabled supplier, an empty mandatory field)
	would block this bank's charges."""
	filters = {"parenttype": SETTINGS, "parent": SETTINGS, "parentfield": "bank_suppliers"}
	frappe.db.delete(ROW_DOCTYPE, {**filters, "bank": bank})
	last = frappe.get_all(ROW_DOCTYPE, filters=filters, pluck="idx", order_by="idx desc", limit=1)
	frappe.get_doc(
		{
			"doctype": ROW_DOCTYPE,
			**filters,
			"idx": (last[0] if last else 0) + 1,
			"bank": bank,
			"supplier": supplier,
		}
	).db_insert()
	frappe.clear_document_cache(SETTINGS, SETTINGS)
