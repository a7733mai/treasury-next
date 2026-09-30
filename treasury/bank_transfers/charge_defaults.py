"""The default Bank Charge Categories, created once per site on install / migrate (spec 7d §3.6).

A category the user deletes is never created again (global default `DEFAULTS_CREATED_KEY`). A category gets an
account row for every company whose PDC Accounts mapping has exactly one distinct Bank Charges Account.
"""

import frappe

DEFAULTS_CREATED_KEY = "treasury_bank_charge_categories_created"
DEFAULT_CATEGORIES = [
	{
		"category_name": "Account Maintenance",
		"keywords": ["account maintenance", "maintenance fee", "account fee", "رسوم إدارة"],
	},
	{"category_name": "Cheque Book", "keywords": ["cheque book", "chequebook", "check book", "دفتر شيكات"]},
	{"category_name": "SMS Alerts", "keywords": ["sms", "alert fee", "رسائل"]},
	{"category_name": "Returned Cheque", "keywords": ["returned cheque", "return chq", "rtn chq", "شيك مرتد"]},
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
	accounts = company_charge_accounts()
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
	result = {}
	for settings, found in accounts.items():
		if len(found) != 1:
			continue
		company = frappe.db.get_value("Treasury Company Settings", settings, "company") or settings
		result[company] = next(iter(found))
	return result
