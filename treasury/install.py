import json

import frappe
from frappe.core.doctype.custom_docperm.custom_docperm import update_custom_docperm
from frappe.core.doctype.doctype.doctype import validate_permissions_for_doctype
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
from frappe.permissions import add_permission, reset_perms
from frappe.utils import cint

TREASURY_ROLES = ("Treasury Manager", "Treasury User", "Cheque Approver")
TREASURY_MODULE = "Treasury Setup"

# Read access granted to the Treasury roles. The posting service saves its vouchers with
# ignore_permissions, but ERPNext still checks the *session user's* permission on the way:
# - erpnext.accounts.party.get_party_account → has_permission("Account", select/read), unconditionally
# - PaymentEntry.set_missing_ref_details → get_reference_details → has_permission(<reference>, "read")
# - PaymentEntry.validate_allocated_amount_with_latest_data → get_outstanding_reference_documents
#   → has_permission(<party type>, "read")
# The other doctypes are linked from the PDC Cheque / PDC Bank Clearing forms (incl. the resulting vouchers).
# Never write / create / submit: only the posting service creates vouchers.
PERMISSION_ROLES = ("Treasury Manager", "Treasury User")
READ_PERMISSIONS = {
	"Account": ("select", "read"),
	"Customer": ("select", "read"),
	"Supplier": ("select", "read"),
	"Bank Account": ("select", "read"),
	"Bank": ("select", "read"),
	"Company": ("select", "read"),
	"Currency": ("select", "read"),
	"Mode of Payment": ("select", "read"),
	"Sales Invoice": ("read",),
	"Purchase Invoice": ("read",),
	"Sales Order": ("read",),
	"Purchase Order": ("read",),
	"Journal Entry": ("read",),
	"Payment Entry": ("read",),
}
# Global default recording what grant_permissions changed, so that uninstall removes exactly that.
GRANTED_PERMISSIONS_KEY = "treasury_granted_permissions"
DOCPERM_COLUMNS = (
	"permlevel",
	"if_owner",
	"select",
	"read",
	"write",
	"create",
	"delete",
	"submit",
	"cancel",
	"amend",
	"report",
	"export",
	"share",
	"print",
	"email",
	"mask",
)

# Custom fields are inserted inside the "More Info" area of each voucher, right after a
# stable standard field (verified against ERPNext 16 JSON).
VOUCHER_INSERT_AFTER = {
	"Payment Entry": "amended_from",
	"Journal Entry": "stock_entry",
}
TREASURY_FIELDNAMES = (
	"treasury_section",
	"treasury_source_doctype",
	"treasury_source_name",
	"treasury_event",
	"treasury_cycle",
)


def after_install():
	setup()


def after_migrate():
	setup()


def before_uninstall():
	delete_custom_fields()
	revoke_permissions()


def setup():
	create_module_defs()
	create_roles()
	create_custom_fields(get_custom_fields(), update=True)
	grant_permissions()


def create_module_defs():
	# `bench migrate` does not create Module Defs for modules added to modules.txt after install
	# (only `install_app` does), and our custom fields link to the "Treasury Setup" module.
	for module in frappe.get_module_list("treasury"):
		if not frappe.db.exists("Module Def", module):
			frappe.get_doc({"doctype": "Module Def", "module_name": module, "app_name": "treasury"}).insert(
				ignore_permissions=True
			)


def create_roles():
	for role in TREASURY_ROLES:
		if not frappe.db.exists("Role", role):
			frappe.get_doc({"doctype": "Role", "role_name": role, "desk_access": 1}).insert(
				ignore_permissions=True
			)


def grant_permissions():
	"""Grant READ_PERMISSIONS to the Treasury roles through Custom DocPerm, as the Role Permission Manager does.

	Each grant is made once and recorded: a permission an administrator removes later is not restored by the
	next migrate, and a permission the role already had is left alone (and never revoked by us).
	"""
	state = _get_granted()
	done = {(g["doctype"], g["role"], g["ptype"]) for g in state["grants"]}
	for doctype, ptypes in READ_PERMISSIONS.items():
		if not frappe.db.exists("DocType", doctype):
			continue
		for role in PERMISSION_ROLES:
			for ptype in ptypes:
				if (doctype, role, ptype) in done:
					continue
				if not frappe.db.exists("Custom DocPerm", {"parent": doctype}):
					# add_permission copies the standard DocPerms into Custom DocPerm first
					state["copied"].append(doctype)
				row = frappe.db.get_value("Custom DocPerm", _docperm_filters(doctype, role), "name")
				if row and cint(frappe.db.get_value("Custom DocPerm", row, ptype)):
					continue
				if row:
					update_custom_docperm(row, {ptype: 1})
					validate_permissions_for_doctype(doctype)
				else:
					add_permission(doctype, role, 0, ptype)
				state["grants"].append({"doctype": doctype, "role": role, "ptype": ptype, "created_row": not row})
	_save_granted(state)


def revoke_permissions():
	"""Undo grant_permissions: remove the rows / flags it added. Where it caused the copy of the standard
	permissions and nothing else was customised since, the doctype is reset to its standard permissions."""
	state = _get_granted()
	for grant in state["grants"]:
		row = frappe.db.get_value("Custom DocPerm", _docperm_filters(grant["doctype"], grant["role"]), "name")
		if not row:
			continue
		if grant["created_row"]:
			frappe.delete_doc("Custom DocPerm", row, ignore_permissions=True, force=True)
		else:
			update_custom_docperm(row, {grant["ptype"]: 0})
	for doctype in set(state["copied"]):
		if _docperms(doctype, "Custom DocPerm") == _docperms(doctype, "DocPerm"):
			reset_perms(doctype)
		frappe.clear_cache(doctype=doctype)
	frappe.db.set_global(GRANTED_PERMISSIONS_KEY, None)


def _docperm_filters(doctype: str, role: str) -> dict:
	return {"parent": doctype, "role": role, "permlevel": 0, "if_owner": 0}


def _docperms(doctype: str, perm_doctype: str) -> list[tuple]:
	rows = frappe.get_all(perm_doctype, filters={"parent": doctype}, fields=["*"])
	return sorted((d.role, *(cint(d.get(c)) for c in DOCPERM_COLUMNS)) for d in rows)


def _get_granted() -> dict:
	value = frappe.db.get_global(GRANTED_PERMISSIONS_KEY)
	state = json.loads(value) if value else {}
	return {"grants": state.get("grants") or [], "copied": state.get("copied") or []}


def _save_granted(state: dict):
	frappe.db.set_global(GRANTED_PERMISSIONS_KEY, json.dumps(state, sort_keys=True))


def get_voucher_fields(insert_after: str) -> list[dict]:
	return [
		{
			"fieldname": "treasury_section",
			"fieldtype": "Section Break",
			"label": "Treasury",
			"collapsible": 1,
			"insert_after": insert_after,
			"module": TREASURY_MODULE,
		},
		{
			"fieldname": "treasury_source_doctype",
			"fieldtype": "Link",
			"label": "Treasury Source Type",
			"options": "DocType",
			"read_only": 1,
			"no_copy": 1,
			"insert_after": "treasury_section",
			"module": TREASURY_MODULE,
		},
		{
			"fieldname": "treasury_source_name",
			"fieldtype": "Dynamic Link",
			"label": "Treasury Source",
			"options": "treasury_source_doctype",
			"read_only": 1,
			"no_copy": 1,
			"search_index": 1,
			"insert_after": "treasury_source_doctype",
			"module": TREASURY_MODULE,
		},
		{
			"fieldname": "treasury_event",
			"fieldtype": "Data",
			"label": "Treasury Event",
			"read_only": 1,
			"no_copy": 1,
			"insert_after": "treasury_source_name",
			"module": TREASURY_MODULE,
		},
		{
			# Distinguishes repeated occurrences of the same event on one source
			# (e.g. a bounced cheque that is re-presented posts party_posting again in cycle 2).
			"fieldname": "treasury_cycle",
			"fieldtype": "Int",
			"label": "Treasury Cycle",
			"default": "1",
			"read_only": 1,
			"no_copy": 1,
			"insert_after": "treasury_event",
			"module": TREASURY_MODULE,
		},
	]


def get_custom_fields() -> dict[str, list[dict]]:
	return {doctype: get_voucher_fields(after) for doctype, after in VOUCHER_INSERT_AFTER.items()}


def delete_custom_fields():
	for doctype in VOUCHER_INSERT_AFTER:
		for fieldname in TREASURY_FIELDNAMES:
			name = frappe.db.get_value("Custom Field", {"dt": doctype, "fieldname": fieldname})
			if name:
				frappe.delete_doc("Custom Field", name, ignore_permissions=True, force=True)
		frappe.clear_cache(doctype=doctype)
