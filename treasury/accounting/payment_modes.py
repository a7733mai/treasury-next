"""Modes of Payment and Payment Entry series of the Treasury vouchers (Treasury Company Settings, "Modes of
Payment" table `payment_modes`, child DocType Treasury Payment Mode Map).

Each row maps (company bank account, method) to a Mode of Payment and, optionally, a Payment Entry naming
series. The posting service gives every Payment Entry it builds for that bank and method the mode and the
series (`resolve`), so e.g. all the cheques of one bank are numbered in their own series for paper filing.

`generate` creates the missing Modes of Payment (type "Bank": ERPNext has no "Cheque" type) with the bank's GL
account as the company's default account, and fills the missing map rows; existing modes and map rows are
never changed. `add_series_to_payment_entry` registers the map's series as Payment Entry naming series through
Frappe's own Document Naming Settings (`update_series`: validation, duplicate check, Property Setters).
"""

import json

import frappe
from frappe import _

SETTINGS = "Treasury Company Settings"
MAP_DOCTYPE = "Treasury Payment Mode Map"
METHODS = (
	"Cheque Out",
	"Cheque In",
	"Bank Transfer",
	"Fawran",
	"SWIFT",
	"Internal Transfer",
	"Auto Debit",
	"Bank Charge",
)
CHEQUE_METHOD = {"Outgoing": "Cheque Out", "Incoming": "Cheque In"}
DEFAULT_GENERATE_METHODS = ("Cheque Out", "Cheque In", "Bank Transfer")
DEFAULT_NAME_TEMPLATE = "{method} - {bank}"
METHOD_PLACEHOLDER = "{method}"
# Global default: the series this app added to Payment Entry (left in place on uninstall, see install.py).
SERIES_ADDED_KEY = "treasury_pe_series_added"
# Numbered variants tried when a generated name is taken by another bank account.
MAX_NAME_VARIANTS = 100


def resolve(company: str, bank_account: str | None, method: str) -> frappe._dict:
	"""The Mode of Payment and Payment Entry series mapped to (bank_account, method); empty values without a row."""
	result = frappe._dict(mode_of_payment=None, naming_series=None)
	if not (company and bank_account and method):
		return result
	rows = frappe.get_all(
		MAP_DOCTYPE,
		filters={"parenttype": SETTINGS, "parent": company, "bank_account": bank_account, "method": method},
		fields=["mode_of_payment", "pe_naming_series"],
		order_by="idx asc",
		limit=1,
	)
	if rows:
		result.mode_of_payment = rows[0].mode_of_payment or None
		result.naming_series = (rows[0].pe_naming_series or "").strip() or None
	return result


def mode_name(method: str, bank_account: str, template: str | None = None) -> str:
	"""The generated name of the Mode of Payment of (method, bank_account) from the settings' name template.

	Placeholders: {method} (the method in the language of the acting user), {bank} (the Bank of the Bank
	Account, e.g. "Al Rayan") and {account} (the Bank Account's account name).
	"""
	values = _bank_values(bank_account)
	name = template or DEFAULT_NAME_TEMPLATE
	for placeholder, value in (
		(METHOD_PLACEHOLDER, _(method, context=MAP_DOCTYPE)),
		("{bank}", values.bank),
		("{account}", values.account),
	):
		name = name.replace(placeholder, value)
	return " ".join(name.split())


def validate_name_template(template: str | None):
	if METHOD_PLACEHOLDER not in (template or ""):
		frappe.throw(_("The Mode Name Template must contain {method}."), title=_("Invalid Template"))


def generate(settings_doc, methods=None, bank_accounts=None) -> dict:
	"""Create the missing Modes of Payment of (bank account, method) and append the missing map rows to
	`settings_doc` (the caller saves it). Runs in `service_context` after the caller's permission check.

	Returns {"created": [modes], "linked": [modes given a row for the company], "skipped": [{"mode", "reason"}]}.
	"""
	methods = list(methods or DEFAULT_GENERATE_METHODS)
	for method in methods:
		if method not in METHODS:
			frappe.throw(_("Unknown method {0}.").format(frappe.bold(method)))
	if bank_accounts is None:
		bank_accounts = [row.bank_account for row in settings_doc.pdc_accounts]
	bank_gl = {}
	for idx, bank_account in enumerate(dict.fromkeys(bank_accounts), 1):
		bank_gl[bank_account] = settings_doc.validate_bank_account(
			frappe._dict(idx=idx, bank_account=bank_account)
		)

	result = {"created": [], "linked": [], "skipped": []}
	mapped = {(row.bank_account, row.method) for row in settings_doc.get("payment_modes")}
	for bank_account, gl_account in bank_gl.items():
		for method in methods:
			if (bank_account, method) in mapped:
				continue  # existing map rows are never overwritten
			name = _available_name(settings_doc, method, bank_account)
			reason = _ensure_mode(name, settings_doc.company, gl_account, result)
			if reason:
				result["skipped"].append({"mode": name, "reason": reason})
				continue
			settings_doc.append(
				"payment_modes", {"bank_account": bank_account, "method": method, "mode_of_payment": name}
			)
			mapped.add((bank_account, method))
	return result


def _available_name(settings_doc, method: str, bank_account: str) -> str:
	"""The template name, or (when another bank account's map row already uses it) the name followed by the
	account name, then a number: two accounts at the same bank get distinct modes."""
	base = mode_name(method, bank_account, settings_doc.get("mode_name_template"))
	account = _bank_values(bank_account).account
	candidates = [base, f"{base} - {account}"]
	candidates += [f"{base} - {account} {n}" for n in range(2, MAX_NAME_VARIANTS)]
	for name in dict.fromkeys(candidates):
		if not _used_by_other_bank(settings_doc, name, bank_account):
			return name
	frappe.throw(_("No free Mode of Payment name found for {0}.").format(frappe.bold(base)))


def _used_by_other_bank(settings_doc, name: str, bank_account: str) -> bool:
	"""`name` is mapped to another bank account: in this (unsaved) settings or in any company's settings."""
	if any(
		row.mode_of_payment == name and row.bank_account != bank_account
		for row in settings_doc.get("payment_modes")
	):
		return True
	return bool(
		frappe.db.exists(
			MAP_DOCTYPE,
			{"parenttype": SETTINGS, "mode_of_payment": name, "bank_account": ["!=", bank_account]},
		)
	)


def _ensure_mode(name: str, company: str, gl_account: str, result: dict) -> str | None:
	"""Create the Mode of Payment or give it a row for `company`; the reason to skip it otherwise."""
	if not frappe.db.exists("Mode of Payment", name):
		mode = frappe.get_doc(
			{
				"doctype": "Mode of Payment",
				"mode_of_payment": name,
				"type": "Bank",
				"enabled": 1,
				"accounts": [{"company": company, "default_account": gl_account}],
			}
		)
		# SECURITY: after the caller checked the user's role on the settings
		mode.flags.ignore_permissions = True
		mode.insert()
		result["created"].append(mode.name)
		return None

	mode = frappe.get_doc("Mode of Payment", name)
	if not mode.enabled:
		return _("Mode of Payment {0} is disabled.").format(name)
	row = next((d for d in mode.accounts if d.company == company), None)
	if row and row.default_account and row.default_account != gl_account:
		return _("uses account {0}").format(row.default_account)
	if not row or not row.default_account:
		if row:
			row.default_account = gl_account
		else:
			mode.append("accounts", {"company": company, "default_account": gl_account})
		# SECURITY: after the caller checked the user's role on the settings
		mode.flags.ignore_permissions = True
		mode.save()
		result["linked"].append(mode.name)
	return None


def _bank_values(bank_account: str) -> frappe._dict:
	values = frappe.db.get_value("Bank Account", bank_account, ["bank", "account_name"], as_dict=True) or {}
	account = values.get("account_name") or bank_account
	return frappe._dict(bank=values.get("bank") or account, account=account)


# ---------------------------------------------------------------------------
# Payment Entry naming series
# ---------------------------------------------------------------------------


def payment_entry_series() -> list[str]:
	"""The naming series options of Payment Entry (Property Setters included)."""
	return [o for o in frappe.get_meta("Payment Entry").get_naming_series_options() if o]


def map_series(settings_doc) -> list[str]:
	"""Distinct Payment Entry series of the map rows, in row order."""
	series = ((row.pe_naming_series or "").strip() for row in settings_doc.get("payment_modes"))
	return list(dict.fromkeys(s for s in series if s))


def unregistered_series(settings_doc) -> list[str]:
	"""Series of the map that are not Payment Entry series yet (posting on them would fail)."""
	registered = set(payment_entry_series())
	return [s for s in map_series(settings_doc) if s not in registered]


def add_series_to_payment_entry(settings_doc) -> list[str]:
	"""Append the unregistered series of the map to the Payment Entry series (the current first option stays
	first, so the default series does not change). Returns the series added. The caller checks the role."""
	new = unregistered_series(settings_doc)
	if not new:
		return []
	current = frappe.get_meta("Payment Entry").get_naming_series_options()
	dns = frappe.get_single("Document Naming Settings")
	dns.transaction_type = "Payment Entry"
	dns.user_must_always_select = 1 if current and current[0] == "" else 0
	dns.naming_series_options = "\n".join([o for o in current if o] + new)
	dns.update_series()

	added = get_added_series()
	frappe.db.set_global(SERIES_ADDED_KEY, json.dumps(list(dict.fromkeys(added + new))))
	return new


def get_added_series() -> list[str]:
	"""The series this app added to Payment Entry (global default `treasury_pe_series_added`)."""
	value = frappe.db.get_global(SERIES_ADDED_KEY)
	try:
		series = json.loads(value) if value else []
	except ValueError:
		return []
	return [s for s in series if isinstance(s, str)] if isinstance(series, list) else []
