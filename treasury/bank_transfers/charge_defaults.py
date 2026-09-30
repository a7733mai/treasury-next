"""The default Bank Charge Categories, created once per site on install / migrate (spec 7d §3.6).

A category the user deletes is never created again (global default `DEFAULTS_CREATED_KEY`). A category gets an
account row for every company whose PDC Accounts mapping has exactly one distinct Bank Charges Account.
"""

import frappe

from treasury.treasury_setup.doctype.treasury_company_settings.treasury_company_settings import (
	validate_expense_account,
)

DEFAULTS_CREATED_KEY = "treasury_bank_charge_categories_created"
DEFAULT_CATEGORIES = [
	{
		"category_name": "Account Maintenance",
		"keywords": ["account maintenance", "maintenance fee", "account fee", "رسوم إدارة"],
	},
	{"category_name": "Cheque Book", "keywords": ["cheque book", "chequebook", "check book", "دفتر شيكات"]},
	{"category_name": "SMS Alerts", "keywords": ["sms", "alert fee", "رسائل"]},
	{
		"category_name": "Returned Cheque",
		"keywords": ["returned cheque", "return chq", "rtn chq", "شيك مرتد"],
	},
	{
		"category_name": "Transfer Fees",
		"keywords": ["transfer fee", "trf fee", "swift charge", "fawran fee", "رسوم تحويل"],
		"auto_select_proposals": 0,
	},
	{"category_name": "Other", "keywords": []},
]


def ensure_default_categories() -> list[str]:
	"""Create the missing default categories once; returns the names created."""
	if frappe.db.get_global(DEFAULTS_CREATED_KEY):
		return []
	accounts = valid_charge_accounts()
	created = []
	for values in DEFAULT_CATEGORIES:
		name = values["category_name"]
		if frappe.db.exists("Bank Charge Category", name):
			continue
		doc = frappe.get_doc(
			{
				"doctype": "Bank Charge Category",
				"category_name": name,
				"keywords": "\n".join(values["keywords"]) or None,
				"auto_select_proposals": values.get("auto_select_proposals", 1),
				"accounts": [
					{"company": company, "expense_account": account} for company, account in accounts.items()
				],
			}
		)
		doc.insert(ignore_permissions=True)
		created.append(doc.name)
	frappe.db.set_global(DEFAULTS_CREATED_KEY, "1")
	return created


def company_charge_accounts() -> dict[str, str]:
	"""{company: its Bank Charges Account} for the companies whose PDC Accounts rows have exactly one distinct
	non-empty Bank Charges Account."""
	rows = frappe.get_all(
		"PDC Account Mapping",
		filters={"parenttype": "Treasury Company Settings", "bank_charges_account": ["is", "set"]},
		fields=["parent", "bank_charges_account"],
	)
	accounts: dict[str, set] = {}
	for row in rows:
		accounts.setdefault(row.parent, set()).add(row.bank_charges_account)
	# a Treasury Company Settings is named after its company (autoname field:company)
	return {company: next(iter(found)) for company, found in accounts.items() if len(found) == 1}


def valid_charge_accounts() -> dict[str, str]:
	"""`company_charge_accounts` without the accounts a category row would reject (disabled, moved to
	another company, no longer an Expense ledger in the company currency): install / migrate must not fail
	on one company's settings, whose categories then simply get no account row."""
	result = {}
	for company, account in company_charge_accounts().items():
		if frappe.db.get_value("Account", account, "disabled"):
			continue
		try:
			validate_expense_account(company, account, "Bank Charges Account")
		except frappe.ValidationError:
			frappe.clear_last_message()
			continue
		result[company] = account
	return result
