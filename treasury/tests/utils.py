"""Shared, isolated fixtures for Treasury tests.

We deliberately do NOT import `erpnext.tests.utils`: importing it runs ERPNext's `BootStrapTestData()`
(hundreds of records, system-settings changes and commits). Instead every helper below is idempotent
and creates only what Treasury needs, inside the IntegrationTestCase transaction.
"""

import frappe
from frappe.utils import add_days, add_years, flt, getdate, today
from frappe.utils.nestedset import get_root_of

from erpnext.accounts.utils import get_balance_on

COMPANY = "_Test Treasury Co"
ABBR = "_TTC"
OTHER_COMPANY = "_Test Treasury Co 2"
OTHER_ABBR = "_TTC2"
BANK = "_Test Treasury Bank"
SUPPLIER = "_Test Treasury Supplier"
USD_SUPPLIER = "_Test Treasury Supplier USD"
CUSTOMER = "_Test Treasury Customer"
ITEM = "_Test Treasury Service"
BUYING_PRICE_LIST = "_Test Treasury Buying"
SELLING_PRICE_LIST = "_Test Treasury Selling"
CUSTOMER_GROUP = "_Test Treasury Customer Group"
SUPPLIER_GROUP = "_Test Treasury Supplier Group"
TERRITORY = "_Test Treasury Territory"
ITEM_GROUP = "_Test Treasury Item Group"
UOM = "_Test Treasury Unit"
USD_RATE = 50.0
RATE_DATE = "2000-01-01"
# PDC scenarios use fixed dates around this day (see `day`), inside a fiscal year that ensure_company creates,
# so their results never depend on the day the suite runs. Mid-year: offsets of ±180 days stay in one year.
BASE_DATE = "2025-07-01"

# GL layout per currency: bank GL account + PDC accounts (all account_type "Bank").
BANK_SETUP = {
	"EGP": {"bank_account_name": "Treasury EGP Current"},
	"USD": {"bank_account_name": "Treasury USD Current"},
}


def acc(name: str, abbr: str = ABBR) -> str:
	return f"{name} - {abbr}"


# ---------------------------------------------------------------------------
# Masters
# ---------------------------------------------------------------------------


def _ensure(doctype: str, name: str, values: dict) -> str:
	if not frappe.db.exists(doctype, name):
		frappe.get_doc({"doctype": doctype, **values}).insert(ignore_permissions=True)
	return name


def _ensure_tree_leaf(doctype: str, name_field: str, parent_field: str, root_name: str, leaf: str) -> str:
	"""Non-group node `leaf` under the existing root of a tree DocType (root created only if missing).

	The root name is looked up, not assumed: on a bare site it may be translated or differently named.
	"""
	root = get_root_of(doctype)
	if not root:
		root = _ensure(doctype, root_name, {name_field: root_name, "is_group": 1})
	return _ensure(doctype, leaf, {name_field: leaf, parent_field: root, "is_group": 0})


def ensure_base_masters():
	"""Minimal setup-wizard records, in case the test site never ran the wizard."""
	_ensure("Party Type", "Customer", {"party_type": "Customer", "account_type": "Receivable"})
	_ensure("Party Type", "Supplier", {"party_type": "Supplier", "account_type": "Payable"})
	_ensure("Warehouse Type", "Transit", {"name": "Transit"})
	_ensure("UOM", UOM, {"uom_name": UOM, "must_be_whole_number": 1})
	_ensure_tree_leaf("Item Group", "item_group_name", "parent_item_group", "All Item Groups", ITEM_GROUP)
	_ensure_tree_leaf(
		"Customer Group", "customer_group_name", "parent_customer_group", "All Customer Groups", CUSTOMER_GROUP
	)
	_ensure_tree_leaf("Territory", "territory_name", "parent_territory", "All Territories", TERRITORY)
	_ensure_tree_leaf(
		"Supplier Group", "supplier_group_name", "parent_supplier_group", "All Supplier Groups", SUPPLIER_GROUP
	)
	ensure_currencies()
	_ensure(
		"Price List",
		BUYING_PRICE_LIST,
		{"price_list_name": BUYING_PRICE_LIST, "currency": "EGP", "buying": 1, "selling": 0, "enabled": 1},
	)
	_ensure(
		"Price List",
		SELLING_PRICE_LIST,
		{"price_list_name": SELLING_PRICE_LIST, "currency": "EGP", "buying": 0, "selling": 1, "enabled": 1},
	)


def ensure_currencies():
	"""Enable EGP/USD and provide a USD<->EGP rate so nothing calls the external rate API.

	The rows are dated far back and stale rates are allowed, so any posting date resolves to them.
	"""
	for currency in ("EGP", "USD"):
		if not frappe.db.get_value("Currency", currency, "enabled"):
			frappe.db.set_value("Currency", currency, "enabled", 1)

	if not frappe.db.get_single_value("Accounts Settings", "allow_stale"):
		frappe.db.set_single_value("Accounts Settings", "allow_stale", 1)
		frappe.clear_document_cache("Accounts Settings", "Accounts Settings")

	for from_currency, to_currency, rate in (("USD", "EGP", USD_RATE), ("EGP", "USD", 1 / USD_RATE)):
		filters = {"from_currency": from_currency, "to_currency": to_currency, "date": RATE_DATE}
		if not frappe.db.exists("Currency Exchange", filters):
			frappe.get_doc(
				{
					"doctype": "Currency Exchange",
					**filters,
					"exchange_rate": rate,
					"for_buying": 1,
					"for_selling": 1,
				}
			).insert(ignore_permissions=True)


def ensure_company(company: str = COMPANY, abbr: str = ABBR) -> str:
	if not frappe.db.exists("Company", company):
		frappe.get_doc(
			{
				"doctype": "Company",
				"company_name": company,
				"abbr": abbr,
				"default_currency": "EGP",
				"country": "Egypt",
				"chart_of_accounts": "Standard",
			}
		).insert(ignore_permissions=True)
	# Previous/current/next year so tests may post back- or forward-dated cheques across a year end.
	for years in (-1, 0, 1):
		ensure_fiscal_year(company, add_years(today(), years))
	ensure_fiscal_year(company, BASE_DATE)
	return company


def ensure_fiscal_year(company: str, date=None):
	date = getdate(date or today())
	fy = frappe.qb.DocType("Fiscal Year")
	candidates = (
		frappe.qb.from_(fy)
		.select(fy.name)
		.where((fy.disabled == 0) & (fy.year_start_date <= date) & (fy.year_end_date >= date))
	).run(pluck=True)
	for name in candidates:
		companies = frappe.get_all("Fiscal Year Company", filters={"parent": name}, pluck="company")
		if not companies or company in companies:
			return name

	year = date.year
	doc = frappe.get_doc(
		{
			"doctype": "Fiscal Year",
			"year": f"{year} {company}",
			"year_start_date": f"{year}-01-01",
			"year_end_date": f"{year}-12-31",
			"companies": [{"company": company}],
		}
	).insert(ignore_permissions=True)  # FiscalYear.on_update clears ERPNext's fiscal year cache itself
	return doc.name


def ensure_account(
	account_name: str,
	parent: str,
	account_type: str | None = None,
	currency: str | None = None,
	company: str = COMPANY,
	abbr: str = ABBR,
) -> str:
	name = acc(account_name, abbr)
	if not frappe.db.exists("Account", name):
		frappe.get_doc(
			{
				"doctype": "Account",
				"account_name": account_name,
				"parent_account": acc(parent, abbr),
				"company": company,
				"is_group": 0,
				"account_type": account_type,
				"account_currency": currency or "EGP",
			}
		).insert(ignore_permissions=True)
	return name


def ensure_bank_account(bank_account_name: str, gl_account: str, company: str = COMPANY) -> str:
	_ensure("Bank", BANK, {"bank_name": BANK})
	name = f"{bank_account_name} - {BANK}"
	if not frappe.db.exists("Bank Account", name):
		frappe.get_doc(
			{
				"doctype": "Bank Account",
				"account_name": bank_account_name,
				"bank": BANK,
				"account": gl_account,
				"company": company,
				"is_company_account": 1,
			}
		).insert(ignore_permissions=True)
	return name


def ensure_bank_setup(currency: str) -> frappe._dict:
	"""Bank GL + Bank Account + PDC accounts for one currency."""
	cur = None if currency == "EGP" else currency
	bank_gl = ensure_account(f"Treasury Bank {currency}", "Bank Accounts", "Bank", cur)
	return frappe._dict(
		currency=currency,
		bank_gl=bank_gl,
		bank_account=ensure_bank_account(BANK_SETUP[currency]["bank_account_name"], bank_gl),
		pdc_payable=ensure_account(f"PDC Payable {currency}", "Current Liabilities", "Bank", cur),
		pdc_receivable=ensure_account(f"PDC Receivable {currency}", "Current Assets", "Bank", cur),
		under_collection=ensure_account(f"Cheques Under Collection {currency}", "Current Assets", "Bank", cur),
		bank_charges=ensure_account("Treasury Bank Charges", "Indirect Expenses"),
	)


def ensure_parties():
	_ensure(
		"Supplier",
		SUPPLIER,
		{"supplier_name": SUPPLIER, "supplier_group": SUPPLIER_GROUP, "supplier_type": "Company"},
	)
	# Foreign supplier whose party account is in USD (same currency as the USD bank).
	usd_payable = ensure_account("Treasury Creditors USD", "Accounts Payable", "Payable", "USD")
	_ensure(
		"Supplier",
		USD_SUPPLIER,
		{
			"supplier_name": USD_SUPPLIER,
			"supplier_group": SUPPLIER_GROUP,
			"supplier_type": "Company",
			"default_currency": "USD",
			"accounts": [{"company": COMPANY, "account": usd_payable}],
		},
	)
	_ensure(
		"Customer",
		CUSTOMER,
		{
			"customer_name": CUSTOMER,
			"customer_group": CUSTOMER_GROUP,
			"territory": TERRITORY,
			"customer_type": "Company",
		},
	)
	_ensure(
		"Item",
		ITEM,
		{
			"item_code": ITEM,
			"item_name": ITEM,
			"item_group": ITEM_GROUP,
			"stock_uom": UOM,
			"is_stock_item": 0,
			"include_item_in_manufacturing": 0,
		},
	)


def mapping_row(bank: frappe._dict) -> dict:
	return {
		"bank_account": bank.bank_account,
		"pdc_payable_account": bank.pdc_payable,
		"pdc_receivable_account": bank.pdc_receivable,
		"under_collection_account": bank.under_collection,
		"bank_charges_account": bank.bank_charges,
	}


def ensure_company_settings(banks: list[frappe._dict]) -> str:
	if frappe.db.exists("Treasury Company Settings", COMPANY):
		return COMPANY
	doc = frappe.get_doc(
		{
			"doctype": "Treasury Company Settings",
			"company": COMPANY,
			"pdc_accounts": [mapping_row(b) for b in banks],
		}
	)
	doc.insert(ignore_permissions=True)
	return doc.name


def setup_treasury_test_data() -> frappe._dict:
	"""Idempotently create everything a Treasury test needs and return the names.

	Roles and custom fields come from the app install (`after_install`/`after_migrate`); they are not
	re-created here because custom-field DDL would implicitly commit the test transaction.
	"""
	ensure_base_masters()
	ensure_company()
	ensure_parties()
	egp = ensure_bank_setup("EGP")
	usd = ensure_bank_setup("USD")
	settings = ensure_company_settings([egp, usd])
	return frappe._dict(
		company=COMPANY,
		supplier=SUPPLIER,
		usd_supplier=USD_SUPPLIER,
		usd_payable=acc("Treasury Creditors USD"),
		customer=CUSTOMER,
		item=ITEM,
		egp=egp,
		usd=usd,
		settings=settings,
		payable=frappe.get_cached_value("Company", COMPANY, "default_payable_account")
		or acc("Creditors"),
		receivable=frappe.get_cached_value("Company", COMPANY, "default_receivable_account")
		or acc("Debtors"),
	)


class TreasuryTestMixin:
	"""Mixin for IntegrationTestCase classes: exposes `cls.data` with the fixtures."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.data = setup_treasury_test_data()

	def use_company_settings(self, **values):
		"""Change Treasury Company Settings for the current test only (restored by a cleanup).

		IntegrationTestCase rolls back only at the end of the class, so every change must be undone.
		"""
		previous = frappe.db.get_value("Treasury Company Settings", COMPANY, list(values), as_dict=True)
		frappe.db.set_value("Treasury Company Settings", COMPANY, values)
		self.addCleanup(frappe.db.set_value, "Treasury Company Settings", COMPANY, dict(previous))


# ---------------------------------------------------------------------------
# PDC helpers
# ---------------------------------------------------------------------------


# The shipped defaults of Treasury Company Settings; PDC test classes start every test from them.
DEFAULT_PDC_SETTINGS = {
	"outgoing_party_posting": "On Handover",
	"outgoing_bank_posting": "On Actual Clearance",
	"incoming_party_posting": "On Receipt",
	"incoming_under_collection": "On Deposit",
	"incoming_bank_posting": "On Actual Clearance",
	"on_bounce": "Reverse by Journal Entry",
	"bounce_charges_account": None,
}


def day(offset: int):
	"""BASE_DATE + `offset` days, as a date (a fixed date in the past: never "in the future")."""
	return getdate(add_days(BASE_DATE, offset))


def bank_account_name(currency: str = "EGP") -> str:
	return f"{BANK_SETUP[currency]['bank_account_name']} - {BANK}"


def unique_cheque_no() -> str:
	return frappe.generate_hash(length=10).upper()


def make_pdc_cheque(
	direction: str = "Outgoing",
	amount: float = 1000,
	party: str | None = None,
	currency: str = "EGP",
	issue_date=None,
	due_date=None,
	invoice=None,
	cheque_no: str | None = None,
	cheque_book: str | None = None,
	**values,
):
	"""Insert a PDC Cheque (status Created). `invoice` (or an order) adds a reference for the full amount."""
	outgoing = direction == "Outgoing"
	issue_date = getdate(issue_date or day(0))
	references = values.pop("references", None) or []
	if invoice:
		references.append(
			{"reference_doctype": invoice.doctype, "reference_name": invoice.name, "allocated_amount": amount}
		)
	if cheque_no is None and not cheque_book:
		cheque_no = unique_cheque_no()
	doc = frappe.get_doc(
		{
			"doctype": "PDC Cheque",
			"direction": direction,
			"company": COMPANY,
			"party_type": "Supplier" if outgoing else "Customer",
			"party": party or (SUPPLIER if outgoing else CUSTOMER),
			"bank_account": bank_account_name(currency),
			"cheque_book": cheque_book,
			"cheque_no": cheque_no,
			"amount": amount,
			"issue_date": issue_date,
			"original_due_date": getdate(due_date or add_days(issue_date, 30)),
			"references": references,
			**values,
		}
	)
	doc.insert()
	return doc


def make_cheque_book(start_no: int, end_no: int, currency: str = "EGP", **values):
	return frappe.get_doc(
		{
			"doctype": "Cheque Book",
			"company": COMPANY,
			"bank_account": bank_account_name(currency),
			"start_no": start_no,
			"end_no": end_no,
			**values,
		}
	).insert()


def ensure_user(email: str, roles: list[str]) -> str:
	"""A System User holding exactly `roles` (created inside the test transaction)."""
	if not frappe.db.exists("User", email):
		frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": email.split("@")[0],
				"send_welcome_email": 0,
				"roles": [{"role": role} for role in roles],
			}
		).insert(ignore_permissions=True)
	return email


def add_user_permission(user: str, allow: str, for_value: str, applicable_for: str | None = None):
	"""A User Permission restricting `user` to `for_value` of `allow` (all doctypes unless `applicable_for`)."""
	filters = {"user": user, "allow": allow, "for_value": for_value}
	if not frappe.db.exists("User Permission", filters):
		frappe.get_doc(
			{
				"doctype": "User Permission",
				**filters,
				"apply_to_all_doctypes": 0 if applicable_for else 1,
				"applicable_for": applicable_for,
			}
		).insert(ignore_permissions=True)


# ---------------------------------------------------------------------------
# Invoice factories
# ---------------------------------------------------------------------------


def make_purchase_invoice(amount: float, posting_date=None, supplier: str = SUPPLIER, submit: bool = True):
	posting_date = getdate(posting_date or today())
	pi = frappe.get_doc(
		{
			"doctype": "Purchase Invoice",
			"company": COMPANY,
			"supplier": supplier,
			"posting_date": posting_date,
			"set_posting_time": 1,
			"due_date": add_days(posting_date, 30),
			"currency": "EGP",
			"conversion_rate": 1,
			"buying_price_list": BUYING_PRICE_LIST,
			"plc_conversion_rate": 1,
			"credit_to": frappe.get_cached_value("Company", COMPANY, "default_payable_account"),
			"update_stock": 0,
			"ignore_pricing_rule": 1,
			"items": [
				{
					"item_code": ITEM,
					"qty": 1,
					"rate": amount,
					"expense_account": acc("Cost of Goods Sold"),
					"cost_center": acc("Main"),
				}
			],
		}
	)
	pi.insert(ignore_permissions=True)
	if submit:
		pi.submit()
	return pi


def make_sales_invoice(amount: float, posting_date=None, customer: str = CUSTOMER, submit: bool = True):
	posting_date = getdate(posting_date or today())
	si = frappe.get_doc(
		{
			"doctype": "Sales Invoice",
			"company": COMPANY,
			"customer": customer,
			"posting_date": posting_date,
			"set_posting_time": 1,
			"due_date": add_days(posting_date, 30),
			"currency": "EGP",
			"conversion_rate": 1,
			"selling_price_list": SELLING_PRICE_LIST,
			"plc_conversion_rate": 1,
			"debit_to": frappe.get_cached_value("Company", COMPANY, "default_receivable_account"),
			"update_stock": 0,
			"ignore_pricing_rule": 1,
			"items": [
				{
					"item_code": ITEM,
					"qty": 1,
					"rate": amount,
					"income_account": acc("Sales"),
					"cost_center": acc("Main"),
				}
			],
		}
	)
	si.insert(ignore_permissions=True)
	if submit:
		si.submit()
	return si


def make_sales_order(amount: float, transaction_date=None, customer: str = CUSTOMER, submit: bool = True):
	"""Sales Order of one non-stock item (no warehouse needed), in the company currency."""
	transaction_date = getdate(transaction_date or today())
	so = frappe.get_doc(
		{
			"doctype": "Sales Order",
			"company": COMPANY,
			"customer": customer,
			"order_type": "Sales",
			"transaction_date": transaction_date,
			"delivery_date": add_days(transaction_date, 30),
			"currency": "EGP",
			"conversion_rate": 1,
			"selling_price_list": SELLING_PRICE_LIST,
			"plc_conversion_rate": 1,
			"ignore_pricing_rule": 1,
			"items": [
				{
					"item_code": ITEM,
					"qty": 1,
					"rate": amount,
					"delivery_date": add_days(transaction_date, 30),
					"cost_center": acc("Main"),
				}
			],
		}
	)
	so.insert(ignore_permissions=True)
	if submit:
		so.submit()
	return so


# ---------------------------------------------------------------------------
# Extra bank
# ---------------------------------------------------------------------------


def ensure_second_egp_bank() -> frappe._dict:
	"""A second EGP company bank account with its own PDC accounts and a PDC Account Mapping row."""
	bank_gl = ensure_account("Treasury Bank EGP 2", "Bank Accounts", "Bank")
	bank = frappe._dict(
		currency="EGP",
		bank_gl=bank_gl,
		bank_account=ensure_bank_account("Treasury EGP Second", bank_gl),
		pdc_payable=ensure_account("PDC Payable EGP 2", "Current Liabilities", "Bank"),
		pdc_receivable=ensure_account("PDC Receivable EGP 2", "Current Assets", "Bank"),
		under_collection=ensure_account("Cheques Under Collection EGP 2", "Current Assets", "Bank"),
		bank_charges=ensure_account("Treasury Bank Charges", "Indirect Expenses"),
	)
	settings = frappe.get_doc("Treasury Company Settings", COMPANY)
	if not any(row.bank_account == bank.bank_account for row in settings.pdc_accounts):
		settings.append("pdc_accounts", mapping_row(bank))
		settings.save(ignore_permissions=True)
	return bank


# ---------------------------------------------------------------------------
# Assertions / queries
# ---------------------------------------------------------------------------


def get_gl_entries(voucher_type: str, voucher_no: str) -> list[frappe._dict]:
	return frappe.get_all(
		"GL Entry",
		filters={"voucher_type": voucher_type, "voucher_no": voucher_no, "is_cancelled": 0},
		fields=["account", "debit", "credit", "party_type", "party", "posting_date"],
	)


def assert_gl_entries(testcase, voucher_type: str, voucher_no: str, expected: list[tuple]):
	"""Order-independent comparison of non-cancelled GL rows.

	`expected` rows are (account, debit, credit) or (account, debit, credit, party) or
	(account, debit, credit, party, posting_date). Debit/credit are in the company currency.
	"""
	width = max((len(row) for row in expected), default=3)

	def key(account, debit, credit, party=None, posting_date=None):
		parts = [account, flt(debit, 2), flt(credit, 2)]
		if width >= 4:
			parts.append(party or None)
		if width >= 5:
			parts.append(getdate(posting_date) if posting_date else None)
		return tuple(parts)

	actual = sorted(
		(
			key(e.account, e.debit, e.credit, e.party, e.posting_date)
			for e in get_gl_entries(voucher_type, voucher_no)
		),
		key=str,
	)
	wanted = sorted((key(*row) for row in expected), key=str)
	testcase.assertEqual(wanted, actual, f"GL mismatch for {voucher_type} {voucher_no}")


def get_account_balance(account: str, date=None) -> float:
	return flt(get_balance_on(account=account, date=date or today(), ignore_account_permission=True))


def assert_account_balance(testcase, account: str, date, expected: float):
	testcase.assertEqual(flt(expected, 2), flt(get_account_balance(account, date), 2), account)


def get_party_balance(party_type: str, party: str, date=None, company: str = COMPANY) -> float:
	return flt(
		get_balance_on(
			party_type=party_type,
			party=party,
			company=company,
			date=date or today(),
			ignore_account_permission=True,
		)
	)


def assert_party_balance(
	testcase, party_type: str, party: str, date, expected: float, company: str = COMPANY
):
	testcase.assertEqual(flt(expected, 2), flt(get_party_balance(party_type, party, date, company), 2), party)


def get_voucher_count(source_doctype: str, source_name: str, docstatus: int | None = None) -> int:
	"""Number of Payment Entries + Journal Entries created for a Treasury source."""
	filters = {"treasury_source_doctype": source_doctype, "treasury_source_name": source_name}
	if docstatus is not None:
		filters["docstatus"] = docstatus
	return sum(frappe.db.count(doctype, filters) for doctype in ("Payment Entry", "Journal Entry"))


def get_party_outstanding(voucher_type: str, voucher_no: str) -> float:
	"""Outstanding amount of an invoice as maintained by the Payment Ledger."""
	return flt(frappe.db.get_value(voucher_type, voucher_no, "outstanding_amount"))


def get_ledger_open_amount(voucher_type: str, voucher_no: str) -> float:
	"""Open amount booked against a voucher in the Payment Ledger (account currency).

	Works for vouchers without an `outstanding_amount` field, e.g. the unallocated part of a Payment Entry.
	"""
	return flt(
		sum(
			frappe.get_all(
				"Payment Ledger Entry",
				filters={"against_voucher_type": voucher_type, "against_voucher_no": voucher_no, "delinked": 0},
				pluck="amount_in_account_currency",
			)
		),
		2,
	)
