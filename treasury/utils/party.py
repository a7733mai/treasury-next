"""Open party: a cheque may be drawn to / from any ERPNext Party Type (Customer, Supplier, Employee,
Shareholder ...), and a payee without a Supplier record yet is created from its name alone (`create_payee`).

The party account of a Payment Entry comes from ERPNext's own resolution (`get_party_account`: the party's
account row, its group for a Customer / Supplier, else the company default for the Party Type's account type).
"""

import frappe
from frappe import _, scrub
from frappe.utils.nestedset import get_root_of

from treasury.utils.context import elevated

DEFAULT_PAYEE_GROUP = "Treasury Payees"
ROOT_SUPPLIER_GROUP = "All Supplier Groups"
PAYEE_ROLES = ("Treasury Manager", "Treasury User", "System Manager")
PAYEE_NAME_LENGTH = 140


def validate_party_type(party_type: str | None):
	if not party_type or not frappe.db.exists("Party Type", party_type):
		frappe.throw(_("{0} is not a Party Type.").format(frappe.bold(party_type or "-")))


def get_party_name(party_type: str, party: str) -> str:
	"""The display name of a party, the way Payment Entry sets its `party_name`: `title` for a Shareholder,
	`<party type>_name` when the doctype has it (customer_name, supplier_name, employee_name ...), else the
	record name."""
	fieldname = "title" if party_type == "Shareholder" else f"{scrub(party_type)}_name"
	name = None
	if frappe.db.has_column(party_type, fieldname):
		name = frappe.db.get_value(party_type, party, fieldname)
	return name or party


@frappe.whitelist(methods=["POST"])
def create_payee(supplier_name: str) -> dict:
	"""A Supplier named `supplier_name` in the Payee Supplier Group of Treasury Settings, or the existing
	(enabled) Supplier of that name. Returns {"party_type", "party", "party_name", "created"}."""
	if not (
		frappe.has_permission("PDC Cheque", "create") or frappe.has_permission("Cheque Batch", "create")
	) or not set(PAYEE_ROLES) & set(frappe.get_roles()):
		frappe.throw(
			_("You need permission to create PDC Cheques or Cheque Batches to add a payee."),
			frappe.PermissionError,
			title=_("Not Permitted"),
		)
	name = " ".join((supplier_name or "").split())
	if not name:
		frappe.throw(_("A payee name is required."))
	if len(name) > PAYEE_NAME_LENGTH:
		frappe.throw(_("The payee name is too long."))

	group = frappe.db.get_single_value("Treasury Settings", "payee_supplier_group")
	# lock the Payee Supplier Group: two concurrent calls with the same name create one supplier
	group_is_leaf = group and frappe.db.get_value("Supplier Group", group, "is_group", for_update=True) == 0

	supplier = _find_payee(name, group if group_is_leaf else None)
	if supplier:
		if supplier.disabled:
			frappe.throw(_("Supplier {0} is disabled.").format(frappe.bold(supplier.supplier_name)))
		return _payee(supplier.name, supplier.supplier_name, created=False)

	if not group_is_leaf:
		frappe.throw(_("Set a valid Payee Supplier Group in Treasury Settings."), title=_("Missing Settings"))
	# a supplier of that name the user cannot read is neither selected nor duplicated
	if frappe.db.exists("Supplier", {"supplier_name": name}):
		frappe.throw(
			_(
				"A Supplier named {0} already exists but you cannot access it. Ask a user with access to Suppliers to select it."
			).format(frappe.bold(name))
		)

	# the user's create permission on the cheque doctypes and role were checked above; Treasury users need
	# not hold the Supplier create permission
	with elevated() as user:
		supplier = frappe.get_doc({"doctype": "Supplier", "supplier_name": name, "supplier_group": group})
		supplier.insert(ignore_permissions=True)
		frappe.db.set_value(
			"Supplier", supplier.name, {"owner": user, "modified_by": user}, update_modified=False
		)
	return _payee(supplier.name, supplier.supplier_name, created=True)


def _find_payee(name: str, group: str | None) -> frappe._dict | None:
	"""The Supplier named `name` that the user may pick: one they can read (User Permissions apply), else one
	of the Payee Supplier Group (the payees Treasury created). The default collation compares
	case-insensitively: "ali rent" finds "Ali Rent"."""
	fields = ["name", "supplier_name", "disabled"]
	if frappe.has_permission("Supplier", "read"):
		readable = frappe.get_list("Supplier", filters={"supplier_name": name}, fields=fields, limit=1)
		if readable:
			return readable[0]
	if group:
		payees = frappe.get_all(
			"Supplier", filters={"supplier_name": name, "supplier_group": group}, fields=fields, limit=1
		)
		if payees:
			return payees[0]
	return None


def _payee(name: str, supplier_name: str, created: bool) -> dict:
	return {"party_type": "Supplier", "party": name, "party_name": supplier_name, "created": created}


def ensure_payee_supplier_group() -> str | None:
	"""The default Payee Supplier Group, a leaf under the root Supplier Group.

	A site that never ran the setup wizard has no Supplier Group at all; a leaf cannot be a tree root (every
	leaf needs a group parent), so the root is created first, the way ERPNext's install fixtures do. Returns
	None (and leaves the tree alone) when the existing root is not a group."""
	if frappe.db.exists("Supplier Group", DEFAULT_PAYEE_GROUP):
		return DEFAULT_PAYEE_GROUP
	root = get_root_of("Supplier Group") or _ensure_root_supplier_group()
	if not frappe.db.get_value("Supplier Group", root, "is_group"):
		return None
	frappe.get_doc(
		{
			"doctype": "Supplier Group",
			"supplier_group_name": DEFAULT_PAYEE_GROUP,
			"parent_supplier_group": root,
			"is_group": 0,
		}
	).insert(ignore_permissions=True)
	return DEFAULT_PAYEE_GROUP


def _ensure_root_supplier_group() -> str:
	"""ERPNext's root Supplier Group (`install_fixtures`: a group without a parent)."""
	if not frappe.db.exists("Supplier Group", ROOT_SUPPLIER_GROUP):
		frappe.get_doc(
			{
				"doctype": "Supplier Group",
				"supplier_group_name": ROOT_SUPPLIER_GROUP,
				"parent_supplier_group": "",
				"is_group": 1,
			}
		).insert(ignore_permissions=True)
	return ROOT_SUPPLIER_GROUP
