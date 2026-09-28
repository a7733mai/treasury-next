import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

TREASURY_ROLES = ("Treasury Manager", "Treasury User", "Cheque Approver")
TREASURY_MODULE = "Treasury Setup"

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


def setup():
	create_module_defs()
	create_roles()
	create_custom_fields(get_custom_fields(), update=True)


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
