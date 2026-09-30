"""Setup Checklist of Treasury Company Settings (spec 7h §3.4), read only.

Eight items, in setup order, each Required or Recommended, with its status (OK / Missing / Warning), what is
missing, a short explanation and where to fix it (`action`: a route, or a settings field to scroll to). The
form shows them in its "Setup Checklist" section; the Treasury Dashboard shows `get_status` as a banner.

A constant number of queries whatever the number of banks: the company banks, the default Cheque Layouts,
the Bank Charge Categories and their accounts, the Treasury Bank Suppliers (the Payment Entry meta is cached).
"""

import frappe
from frappe import _

from treasury.accounting import payment_modes
from treasury.bank_transfers.lifecycle import TRANSFER_METHOD
from treasury.pdc.report_utils import DOCTYPE as PDC_CHEQUE
from treasury.pdc.report_utils import is_permitted_value
from treasury.treasury_setup.doctype.treasury_company_settings.treasury_company_settings import (
	TRANSFER_DEFAULT_BANK_FIELDS,
)

SETTINGS = "Treasury Company Settings"
REQUIRED, RECOMMENDED = "Required", "Recommended"
OK, MISSING, WARNING = "OK", "Missing", "Warning"
ITEMS = (
	"pdc_accounts",
	"payment_modes",
	"default_banks",
	"transfer_fee_rules",
	"balance_thresholds",
	"cheque_layouts",
	"charge_categories",
	"bank_suppliers",
)
DEFAULT_BANK_FIELDS = ("default_outgoing_cheque_bank_account", "default_normal_bank_account")
CHEQUE_METHODS = ("Cheque Out", "Cheque In")


def company_banks(company: str) -> list[frappe._dict]:
	"""The company's enabled company bank accounts with a GL account ({name, bank, account}, name order) that
	the user may see (User Permissions on Bank Account for PDC Cheque, as `get_threshold_banks`)."""
	return [
		b
		for b in frappe.get_all(
			"Bank Account",
			filters={"company": company, "is_company_account": 1, "disabled": 0, "account": ["is", "set"]},
			fields=["name", "bank", "account"],
			order_by="name asc",
		)
		if is_permitted_value("Bank Account", b.name, PDC_CHEQUE)
	]


def item(key: str, label: str, level: str, missing: list, detail: str = "", action=None, status=None):
	return frappe._dict(
		key=key,
		label=label,
		level=level,
		status=status or (MISSING if missing else OK),
		missing=list(missing),
		detail=detail,
		action=frappe._dict(action or {}),
	)


def get_checklist(settings_doc) -> list[frappe._dict]:
	"""The eight items (ITEMS order) of the settings document `settings_doc` (saved or not)."""
	company = settings_doc.company
	banks = company_banks(company) if company else []
	pdc_banks = [row.bank_account for row in settings_doc.get("pdc_accounts") if row.bank_account]
	return [
		_pdc_accounts(settings_doc, banks, pdc_banks),
		_payment_modes(settings_doc, pdc_banks),
		_default_banks(settings_doc, pdc_banks),
		_transfer_fee_rules(settings_doc),
		_balance_thresholds(settings_doc, banks),
		_cheque_layouts(settings_doc, pdc_banks),
		_charge_categories(company),
		_bank_suppliers(banks),
	]


def get_status(company: str) -> frappe._dict:
	"""{exists, complete, missing, required_missing, route (, route_options)} of the company's checklist:
	`missing` = the number of items Missing, `complete` = no Required item Missing."""
	if not frappe.db.exists(SETTINGS, company):
		return frappe._dict(
			exists=0,
			complete=0,
			missing=len(ITEMS),
			required_missing=1,
			route=["Form", SETTINGS, "new"],
			route_options={"company": company},
		)
	doc = frappe.get_doc(SETTINGS, company)
	doc.check_permission("read")
	items = get_checklist(doc)
	required_missing = sum(1 for i in items if i.status == MISSING and i.level == REQUIRED)
	return frappe._dict(
		exists=1,
		complete=0 if required_missing else 1,
		missing=sum(1 for i in items if i.status == MISSING),
		required_missing=required_missing,
		route=["Form", SETTINGS, company],
	)


# ---------------------------------------------------------------------------
# Items
# ---------------------------------------------------------------------------


def _pdc_accounts(doc, banks, pdc_banks):
	label = _("PDC Accounts", context=SETTINGS)
	if not banks:
		return item(
			"pdc_accounts",
			label,
			REQUIRED,
			[],
			_("No company bank account with a GL account of type Bank."),
			{
				"route": ["List", "Bank Account"],
				"route_options": {"company": doc.company, "is_company_account": 1},
			},
			status=MISSING,
		)
	missing = [b.name for b in banks if b.name not in pdc_banks]
	return item(
		"pdc_accounts",
		label,
		REQUIRED,
		missing,
		_("Every company bank account needs its PDC accounts (payable, receivable, under collection)."),
		{"field": "pdc_accounts"},
	)


def _payment_modes(doc, pdc_banks):
	mapped = {(row.bank_account, row.method) for row in doc.get("payment_modes")}
	wanted = [(bank, method) for bank in pdc_banks for method in CHEQUE_METHODS]
	for method, fieldname in TRANSFER_DEFAULT_BANK_FIELDS.items():
		if doc.get(fieldname):
			wanted.append((doc.get(fieldname), TRANSFER_METHOD[method]))
	missing = [
		f"{bank}: {_(method, context=payment_modes.MAP_DOCTYPE)}"
		for bank, method in dict.fromkeys(wanted)
		if (bank, method) not in mapped
	]
	action = {"field": "payment_modes"}
	label = _("Modes of Payment", context=SETTINGS)
	if not missing:
		unregistered = payment_modes.unregistered_series(doc)
		if unregistered:
			return item(
				"payment_modes",
				label,
				RECOMMENDED,
				[],
				_("Series not registered for Payment Entry yet: {0}").format(", ".join(unregistered)),
				action,
				status=WARNING,
			)
	return item(
		"payment_modes",
		label,
		RECOMMENDED,
		missing,
		_("A Mode of Payment per bank and method gives every voucher its mode and Payment Entry series."),
		action,
	)


def _default_banks(doc, pdc_banks):
	empty = [f for f in DEFAULT_BANK_FIELDS if not doc.get(f)]
	result = item(
		"default_banks",
		_("Default Banks", context=SETTINGS),
		RECOMMENDED,
		[_(doc.meta.get_label(f)) for f in empty],
		_("The banks proposed for new cheques and transfers."),
		{"field": empty[0] if empty else DEFAULT_BANK_FIELDS[0]},
	)
	# quick fill (form only): with exactly one bank in PDC Accounts, the user may fill the empty defaults with it
	result.quick_fill = pdc_banks[0] if empty and len(pdc_banks) == 1 else None
	result.empty_fields = empty
	return result


def _transfer_fee_rules(doc):
	label = _("Transfer Fee Rules", context=SETTINGS)
	action = {"field": "transfer_fee_rules"}
	defaults = [(m, doc.get(f)) for m, f in TRANSFER_DEFAULT_BANK_FIELDS.items() if doc.get(f)]
	if not defaults:
		return item(
			"transfer_fee_rules", label, RECOMMENDED, [], _("No default transfer bank to check."), action
		)
	rules = {(row.bank_account, row.method) for row in doc.get("transfer_fee_rules")}
	missing = [
		f"{bank}: {_(method, context='Bank Transfer')}"
		for method, bank in defaults
		if (bank, method) not in rules
	]
	return item(
		"transfer_fee_rules",
		label,
		RECOMMENDED,
		missing,
		_("Without a fee rule, a transfer from the default bank is booked without its fee."),
		action,
	)


def _balance_thresholds(doc, banks):
	rows = {row.bank_account for row in doc.get("balance_thresholds")}
	return item(
		"balance_thresholds",
		_("Balance Thresholds", context=SETTINGS),
		RECOMMENDED,
		[b.name for b in banks if b.name not in rows],
		_("A bank without thresholds raises no low balance alert."),
		{"field": "balance_thresholds"},
	)


def _cheque_layouts(doc, pdc_banks):
	with_format = {
		row.bank_account for row in doc.get("pdc_accounts") if row.bank_account and row.cheque_print_format
	}
	with_layout = set(
		frappe.get_all(
			"Cheque Layout",
			filters={"bank_account": ["in", pdc_banks], "is_default": 1, "disabled": 0},
			pluck="bank_account",
		)
		if pdc_banks
		else []
	)
	missing = [b for b in pdc_banks if b not in with_format and b not in with_layout]
	action = {"route": ["Form", "Cheque Layout", "new"]}
	if missing:
		action["route_options"] = {"bank_account": missing[0]}
	return item(
		"cheque_layouts",
		_("Cheque Layouts", context=SETTINGS),
		RECOMMENDED,
		missing,
		_("A default Cheque Layout per bank prints the cheque leaves."),
		action,
	)


def _charge_categories(company):
	label = _("Bank Charge Categories", context=SETTINGS)
	action = {"route": ["List", "Bank Charge Category"]}
	categories = frappe.get_all(
		"Bank Charge Category", filters={"disabled": 0}, pluck="name", order_by="name asc"
	)
	if not categories:
		return item(
			"charge_categories",
			label,
			RECOMMENDED,
			[],
			_("No enabled Bank Charge Category."),
			action,
			status=MISSING,
		)
	with_account = set(
		frappe.get_all(
			"Bank Charge Category Account",
			filters={"parenttype": "Bank Charge Category", "parent": ["in", categories], "company": company},
			pluck="parent",
		)
	)
	return item(
		"charge_categories",
		label,
		RECOMMENDED,
		[c for c in categories if c not in with_account],
		_("Every enabled category needs an expense account for the company."),
		action,
	)


def _bank_suppliers(banks):
	mapped = set(
		frappe.get_all(
			"Treasury Bank Supplier",
			filters={"parenttype": "Treasury Settings", "parent": "Treasury Settings"},
			pluck="bank",
		)
	)
	distinct = list(dict.fromkeys(b.bank for b in banks if b.bank))
	return item(
		"bank_suppliers",
		_("Bank Suppliers", context=SETTINGS),
		RECOMMENDED,
		[b for b in distinct if b not in mapped],
		_(
			"Created automatically on the first bank charge; map an existing Supplier here to avoid a duplicate."
		),
		{"route": ["Form", "Treasury Settings"], "field": "bank_suppliers"},
	)
