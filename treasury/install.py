import frappe
from frappe import _
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
from frappe.permissions import reset_perms
from frappe.utils import cint

from treasury.accounting import dimensions, payment_modes
from treasury.utils.party import ensure_payee_supplier_group
from treasury.utils.settings import set_missing_single_defaults

TREASURY_ROLES = ("Treasury Manager", "Treasury User", "Cheque Approver")
TREASURY_MODULE = "Treasury Setup"

# The app grants no permission on core doctypes: the posting service builds the vouchers in an elevated
# context (treasury.accounting.posting). Earlier development versions granted the Treasury roles read
# access to these doctypes through Custom DocPerm, which also copied (froze) their standard permissions;
# `remove_legacy_permissions` undoes that once per site.
LEGACY_PERMISSION_ROLES = ("Treasury Manager", "Treasury User")
LEGACY_PERMISSION_DOCTYPES = (
	"Account",
	"Customer",
	"Supplier",
	"Bank Account",
	"Bank",
	"Company",
	"Currency",
	"Mode of Payment",
	"Sales Invoice",
	"Purchase Invoice",
	"Sales Order",
	"Purchase Order",
	"Journal Entry",
	"Payment Entry",
)
# Global defaults: the state the legacy version kept, and the marker of the completed cleanup (so a
# permission an administrator grants the Treasury roles later is never removed by a migrate).
LEGACY_GRANTS_KEY = "treasury_granted_permissions"
LEGACY_CLEANUP_DONE_KEY = "treasury_legacy_permissions_removed"
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
	"import",
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
	"""Remove what the app added to core doctypes. The Treasury roles (and their assignment to users) are
	kept on purpose: they hold no permission outside the app's own doctypes once it is uninstalled, and a
	reinstall finds the users' assignments intact. An administrator can delete them in Role.

	Kept on purpose too, as user data: the Payment Entry series added from Treasury Company Settings (their
	Property Setters), the Modes of Payment and the "Treasury Payees" Supplier Group (and its suppliers)."""
	delete_custom_fields()
	delete_dimension_fields()
	remove_legacy_permissions()  # a site never migrated since the legacy grants
	report_kept_series()


def setup():
	create_module_defs()
	create_roles()
	create_custom_fields(get_custom_fields(), update=True)
	remove_legacy_permissions()
	ensure_payee_supplier_group()  # before the defaults: Payee Supplier Group links to it
	set_default_settings()
	dimensions.create_dimension_fields()


def set_default_settings() -> list[str]:
	"""Store the defaults of Treasury Settings fields a site has no value for yet (`bench migrate` does not
	store the default of a Single field added after install). Values already set, 0 included, are kept."""
	return set_missing_single_defaults("Treasury Settings")


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


def remove_legacy_permissions(force: bool = False) -> list[str]:
	"""Undo the Custom DocPerm grants of earlier development versions (see LEGACY_PERMISSION_DOCTYPES).

	For each of those doctypes: delete the Custom DocPerm rows of the Treasury Manager / Treasury User roles
	(and only those), then, if the remaining Custom DocPerm rows are exactly a copy of the standard DocPerm
	rows, reset the doctype to its standard permissions (Frappe then follows the shipped permissions again).
	A doctype an administrator customised otherwise keeps its customisation.

	Runs once per site (marker `LEGACY_CLEANUP_DONE_KEY`) unless `force`; idempotent either way.
	Returns the doctypes that were reset to their standard permissions.
	"""
	if not force and frappe.db.get_global(LEGACY_CLEANUP_DONE_KEY):
		return []
	reset = []
	for doctype in LEGACY_PERMISSION_DOCTYPES:
		if not frappe.db.exists("DocType", doctype):
			continue
		for row in frappe.get_all(
			"Custom DocPerm",
			filters={"parent": doctype, "role": ["in", LEGACY_PERMISSION_ROLES]},
			pluck="name",
		):
			frappe.delete_doc("Custom DocPerm", row, ignore_permissions=True, force=True)
		if frappe.db.exists("Custom DocPerm", {"parent": doctype}) and _docperms(
			doctype, "Custom DocPerm"
		) == _docperms(doctype, "DocPerm"):
			reset_perms(doctype)
			reset.append(doctype)
		frappe.clear_cache(doctype=doctype)
	frappe.db.set_global(LEGACY_GRANTS_KEY, None)
	frappe.db.set_global(LEGACY_CLEANUP_DONE_KEY, "1")
	return reset


def _docperms(doctype: str, perm_doctype: str) -> list[tuple]:
	rows = frappe.get_all(perm_doctype, filters={"parent": doctype}, fields=["*"])
	return sorted((d.role, *(cint(d.get(c)) for c in DOCPERM_COLUMNS)) for d in rows)


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


def custom_fields_to_delete() -> list[tuple[str, str]]:
	"""(doctype, fieldname) of every custom field `before_uninstall` removes: exactly those `setup` creates."""
	return [(doctype, fieldname) for doctype in VOUCHER_INSERT_AFTER for fieldname in TREASURY_FIELDNAMES]


def delete_custom_fields():
	for doctype, fieldname in custom_fields_to_delete():
		name = frappe.db.get_value("Custom Field", {"dt": doctype, "fieldname": fieldname})
		if name:
			frappe.delete_doc("Custom Field", name, ignore_permissions=True, force=True)
	for doctype in VOUCHER_INSERT_AFTER:
		frappe.clear_cache(doctype=doctype)


def delete_dimension_fields():
	"""The Accounting Dimension fields ERPNext added to the app's own doctypes (dropped with them anyway; the
	Custom Field records would otherwise stay behind)."""
	for doctype, fieldname in dimensions.dimension_custom_fields():
		name = frappe.db.get_value("Custom Field", {"dt": doctype, "fieldname": fieldname})
		if name:
			frappe.delete_doc("Custom Field", name, ignore_permissions=True, force=True)


def report_kept_series() -> list[str]:
	"""Tell the administrator which Payment Entry series the app added and left in place on uninstall."""
	series = payment_modes.get_added_series()
	if series:
		message = _(
			"The Payment Entry naming series {0} were added by Treasury and are kept on purpose (existing Payment Entries use them). Remove them in Document Naming Settings if they are not needed."
		).format(", ".join(series))
		print(message)
		if getattr(frappe.local, "request", None):
			frappe.msgprint(message)
	return series
