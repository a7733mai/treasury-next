"""Who receives the bank position digest and alerts (spec 7e §3.3).

Treasury Settings → Recipients lists roles and / or users (an empty table: nobody). Each recipient only gets
the companies and bank accounts they may see: read permission on PDC Cheque (the ref_doctype of the forecast
reports) and their User Permissions on Company and Bank Account.
"""

import frappe

from treasury.pdc import scheduler as pdc_scheduler
from treasury.pdc.report_utils import DOCTYPE, is_permitted_value

RECIPIENT_DOCTYPE = "Treasury Alert Recipient"
SETTINGS = "Treasury Settings"
ROLE, USER = "Role", "User"
DEFAULT_ROLE = "Treasury Manager"
# Global default: the default recipient row was added once (a table the user empties is never refilled).
DEFAULT_RECIPIENTS_KEY = "treasury_balance_alert_recipients_created"


def _rows() -> list[frappe._dict]:
	return frappe.get_all(
		RECIPIENT_DOCTYPE,
		filters={"parenttype": SETTINGS, "parent": SETTINGS, "parentfield": "balance_alert_recipients"},
		fields=["recipient_type", "role", "user"],
		order_by="idx asc",
	)


def get_recipients() -> list[str]:
	"""The enabled system users of the Role rows plus the enabled User rows, deduplicated and sorted."""
	rows = _rows()
	users = set(pdc_scheduler.users_with_roles([r.role for r in rows if r.recipient_type == ROLE and r.role]))
	named = [r.user for r in rows if r.recipient_type == USER and r.user]
	if named:
		users.update(frappe.get_all("User", filters={"name": ["in", named], "enabled": 1}, pluck="name"))
	return sorted(users)


def visible_banks(user: str, company: str, bank_accounts: list[str]) -> list[str]:
	"""The bank accounts among `bank_accounts` of `company` that `user` may see in the forecast reports."""
	if not frappe.has_permission(DOCTYPE, "read", user=user):
		return []
	if not is_permitted_value("Company", company, DOCTYPE, user):
		return []
	return [b for b in bank_accounts if is_permitted_value("Bank Account", b, DOCTYPE, user)]


def ensure_default_recipients() -> bool:
	"""Install / migrate: add the Role row "Treasury Manager" once, when the table is empty. True if added."""
	if frappe.db.get_global(DEFAULT_RECIPIENTS_KEY):
		return False
	added = False
	if not _rows() and frappe.db.exists("Role", DEFAULT_ROLE):
		# written directly: saving the settings would run their whole validation during a migrate
		frappe.get_doc(
			{
				"doctype": RECIPIENT_DOCTYPE,
				"name": frappe.generate_hash(length=10),
				"parenttype": SETTINGS,
				"parentfield": "balance_alert_recipients",
				"parent": SETTINGS,
				"idx": 1,
				"recipient_type": ROLE,
				"role": DEFAULT_ROLE,
			}
		).db_insert()
		added = True
	frappe.db.set_global(DEFAULT_RECIPIENTS_KEY, "1")
	return added
