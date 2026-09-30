"""Shared, isolated fixtures for Treasury tests.

We deliberately do NOT import `erpnext.tests.utils`: importing it runs ERPNext's `BootStrapTestData()`
(hundreds of records, system-settings changes and commits). Instead every helper below is idempotent
and creates only what Treasury needs, inside the IntegrationTestCase transaction.
"""

import random

import frappe
from erpnext.accounts.utils import get_balance_on
from frappe.utils import add_days, add_years, flt, getdate, today
from frappe.utils.nestedset import get_root_of

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


# Link targets added by Phase 7a (open party, Accounting Details, Modes of Payment, the `Branch` test
# dimension). Frappe walks test-record dependencies recursively and applies a test module's
# IGNORE_TEST_RECORD_DEPENDENCIES only to that module's own doctype: a link not ignored there is walked
# with ERPNext's test modules (Cost Center -> Company -> ... -> Payment Gateway, a doctype of the payments
# app, which is not installed). The doctype test modules add these to their list.
# Department: accounting dimension fields present on the test site are walked too.
PHASE_7A_TEST_RECORD_IGNORES = [
	"Party Type",
	"Cost Center",
	"Project",
	"Mode of Payment",
	"Contact",
	"Branch",
	"Department",
]
# Link targets added by Phase 7f: the audit columns of Cheque Book Cancelled Leaf (`user` -> User, walked on
# to Email Account -> Payment Gateway; `source_doctype` -> DocType) and PDC Cheque `last_printed_by`.
PHASE_7F_TEST_RECORD_IGNORES = ["User", "DocType"]


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
	# open party (Phase 7a): the other Party Types of ERPNext's setup wizard
	_ensure("Party Type", "Employee", {"party_type": "Employee", "account_type": "Payable"})
	_ensure("Party Type", "Shareholder", {"party_type": "Shareholder", "account_type": "Payable"})
	_ensure("Warehouse Type", "Transit", {"name": "Transit"})
	_ensure("UOM", UOM, {"uom_name": UOM, "must_be_whole_number": 1})
	_ensure_tree_leaf("Item Group", "item_group_name", "parent_item_group", "All Item Groups", ITEM_GROUP)
	_ensure_tree_leaf(
		"Customer Group",
		"customer_group_name",
		"parent_customer_group",
		"All Customer Groups",
		CUSTOMER_GROUP,
	)
	_ensure_tree_leaf("Territory", "territory_name", "parent_territory", "All Territories", TERRITORY)
	_ensure_tree_leaf(
		"Supplier Group",
		"supplier_group_name",
		"parent_supplier_group",
		"All Supplier Groups",
		SUPPLIER_GROUP,
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
		under_collection=ensure_account(
			f"Cheques Under Collection {currency}", "Current Assets", "Bank", cur
		),
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


def ensure_supplier(name: str) -> str:
	"""An extra EGP supplier (default payable account), e.g. for scenarios that need a party of their own."""
	return _ensure(
		"Supplier",
		name,
		{"supplier_name": name, "supplier_group": SUPPLIER_GROUP, "supplier_type": "Company"},
	)


def ensure_customer(name: str) -> str:
	"""An extra EGP customer (default receivable account), e.g. for scenarios that need a party of their own."""
	return _ensure(
		"Customer",
		name,
		{
			"customer_name": name,
			"customer_group": CUSTOMER_GROUP,
			"territory": TERRITORY,
			"customer_type": "Company",
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
		payable=frappe.get_cached_value("Company", COMPANY, "default_payable_account") or acc("Creditors"),
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


# The shipped defaults of Treasury Company Settings; PDC test classes start every test from them. The 1.0 test
# classes keep asserting the Journal Entry settlement (under_collection_journal, bank_journal ...): they pin the
# `Journal Entry` Bank Settlement Voucher and are the coverage of that mode; the Payment Entry mode (the shipped
# default since Phase 7a) has its own test classes, which start from PE_SETTINGS.
DEFAULT_PDC_SETTINGS = {
	"outgoing_party_posting": "On Handover",
	"outgoing_bank_posting": "On Actual Clearance",
	"incoming_party_posting": "On Receipt",
	"incoming_under_collection": "On Deposit",
	"incoming_bank_posting": "On Actual Clearance",
	"on_bounce": "Reverse by Journal Entry",
	"bounce_charges_account": None,
	"bank_settlement_voucher": "Journal Entry",
}
PE_SETTINGS = {**DEFAULT_PDC_SETTINGS, "bank_settlement_voucher": "Payment Entry"}


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


def unique_leaf_start(size: int = 1_000, digits: int = 8) -> int:
	"""A random first leaf number for a book of `size` leaves whose range no Cheque Book of the site (any bank
	account, any status) overlaps and no PDC / Security Cheque number falls in: a test never depends on the
	books of other tests, of its own class (they stay until the class rolls back) or of an earlier run."""
	low, high = 10 ** (digits - 1), 10**digits - size
	for _attempt in range(100):
		start = random.randrange(low, high)
		end = start + size - 1
		if frappe.get_all("Cheque Book", filters=[["start_no", "<=", end], ["end_no", ">=", start]], limit=1):
			continue
		# numbers of the same width compare as text; a wider or narrower number is not a leaf of the range
		numbers = [["cheque_no", ">=", str(start).zfill(digits)], ["cheque_no", "<=", str(end).zfill(digits)]]
		if any(frappe.get_all(dt, filters=numbers, limit=1) for dt in ("PDC Cheque", "Security Cheque")):
			continue
		return start
	raise AssertionError(f"No free range of {size} cheque leaves")


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
# Security cheque helpers
# ---------------------------------------------------------------------------

EMPLOYEE_FIRST_NAME = "_Test Treasury Employee"
GENDER = "_Test Treasury Gender"


def ensure_custody_location(name: str = "_Test Treasury Safe", is_active: int = 1) -> str:
	return _ensure("Custody Location", name, {"location_name": name, "is_active": is_active})


def ensure_employee() -> str:
	"""An Employee of the test company (named by ERPNext's series), e.g. for an employee custody cheque."""
	existing = frappe.db.get_value("Employee", {"first_name": EMPLOYEE_FIRST_NAME, "company": COMPANY})
	if existing:
		return existing
	_ensure("Gender", GENDER, {"gender": GENDER})
	return (
		frappe.get_doc(
			{
				"doctype": "Employee",
				"first_name": EMPLOYEE_FIRST_NAME,
				"company": COMPANY,
				"gender": GENDER,
				"date_of_birth": "1990-01-01",
				"date_of_joining": "2020-01-01",
				"status": "Active",
			}
		)
		.insert(ignore_permissions=True)
		.name
	)


def make_security_cheque(
	direction: str = "Incoming",
	amount: float = 5000,
	amount_type: str = "Fixed",
	party_type: str | None = None,
	party: str | None = None,
	issue_date=None,
	expiry_date=None,
	cheque_no: str | None = None,
	cheque_book: str | None = None,
	currency: str = "EGP",
	**values,
):
	"""Insert a Security Cheque (status Created). Outgoing ones are drawn on the EGP (or `currency`) bank."""
	outgoing = direction == "Outgoing"
	issue_date = getdate(issue_date or day(-60))
	party_type = party_type or ("Supplier" if outgoing else "Customer")
	if party is None and party_type in ("Customer", "Supplier"):
		party = SUPPLIER if party_type == "Supplier" else CUSTOMER
	if cheque_no is None and not cheque_book:
		cheque_no = unique_cheque_no()
	doc = frappe.get_doc(
		{
			"doctype": "Security Cheque",
			"direction": direction,
			"company": COMPANY,
			"party_type": party_type,
			"party": party,
			"purpose": values.pop("purpose", "Contract Performance"),
			"cheque_no": cheque_no,
			"bank_account": bank_account_name(currency) if outgoing else None,
			"cheque_book": cheque_book,
			"drawer_bank": None if outgoing else "Drawer Bank Y",
			"amount_type": amount_type,
			"amount": amount if amount_type == "Fixed" else 0,
			"issue_date": issue_date,
			"expiry_date": getdate(expiry_date) if expiry_date else add_days(issue_date, 365),
			**values,
		}
	)
	doc.insert()
	return doc


# ---------------------------------------------------------------------------
# Bank facility helpers
# ---------------------------------------------------------------------------


def ensure_facility_accounts() -> frappe._dict:
	"""Liability (loans, accrued interest) accounts per currency and the expense accounts of the facilities."""
	return frappe._dict(
		loan_egp=ensure_account("Treasury Bank Loans EGP", "Current Liabilities"),
		loan_egp_2=ensure_account("Treasury Bank Loans EGP 2", "Current Liabilities"),
		loan_usd=ensure_account("Treasury Bank Loans USD", "Current Liabilities", currency="USD"),
		accrued_egp=ensure_account("Treasury Accrued Interest EGP", "Current Liabilities"),
		accrued_usd=ensure_account("Treasury Accrued Interest USD", "Current Liabilities", currency="USD"),
		interest=ensure_account("Treasury Interest Expense", "Indirect Expenses"),
		commission=ensure_account("Treasury Bank Commission", "Indirect Expenses"),
		fees=ensure_account("Treasury Facility Fees", "Indirect Expenses"),
		penalty=ensure_account("Treasury Facility Penalties", "Indirect Expenses"),
	)


def make_own_bank(currency: str = "EGP") -> frappe._dict:
	"""A fresh company bank account (own GL account) at BANK, so its GL balance belongs to one test only."""
	tag = frappe.generate_hash(length=6)
	cur = None if currency == "EGP" else currency
	bank_gl = ensure_account(f"Fac Bank {currency} {tag}", "Bank Accounts", "Bank", cur)
	return frappe._dict(
		currency=currency, bank_gl=bank_gl, bank_account=ensure_bank_account(f"Fac {currency} {tag}", bank_gl)
	)


def make_bank_facility(
	currency: str = "EGP",
	total_limit: float = 1_000_000,
	limits: list[dict] | None = None,
	bank_account: str | None = None,
	start_date="2025-01-01",
	expiry_date="2026-12-31",
	activate: bool = True,
	accrued: bool = True,
	**values,
):
	"""A Bank Facility (Active unless `activate` is False) with a Short Term Loan sub-limit by default."""
	accounts = ensure_facility_accounts()
	loan = accounts.loan_egp if currency == "EGP" else accounts.loan_usd
	if limits is None:
		limits = [{"limit_type": "Short Term Loan", "limit_amount": total_limit, "interest_rate": 12}]
	for row in limits:
		if row["limit_type"] != "Overdraft" and "liability_account" not in row:
			row["liability_account"] = loan
	doc = frappe.get_doc(
		{
			"doctype": "Bank Facility",
			"facility_name": f"Facility {frappe.generate_hash(length=6)}",
			"company": COMPANY,
			"bank": BANK,
			"currency": currency,
			"total_limit": total_limit,
			"start_date": getdate(start_date),
			"expiry_date": getdate(expiry_date),
			"default_bank_account": bank_account or bank_account_name(currency),
			"interest_expense_account": accounts.interest,
			"commission_expense_account": accounts.commission,
			"accrued_interest_account": (accounts.accrued_egp if currency == "EGP" else accounts.accrued_usd)
			if accrued
			else None,
			"limits": limits,
			**values,
		}
	).insert()
	if activate:
		doc.activate(activation_date=doc.start_date)
		doc.reload()
	return doc


def make_drawdown(
	facility,
	amount: float = 120_000,
	drawdown_date="2025-01-31",
	tenor_months: int = 12,
	frequency: str = "Monthly",
	method: str = "Equal Principal",
	submit: bool = True,
	limit_type: str = "Short Term Loan",
	**values,
):
	facility_name = facility if isinstance(facility, str) else facility.name
	doc = frappe.get_doc(
		{
			"doctype": "Facility Drawdown",
			"facility": facility_name,
			"limit_type": limit_type,
			"drawdown_date": getdate(drawdown_date),
			"amount": amount,
			"tenor_months": tenor_months,
			"repayment_frequency": frequency,
			"repayment_method": method,
			**values,
		}
	).insert()
	if submit:
		doc.submit()
	return doc


def make_repayment(drawdown, repayment_date, submit: bool = True, **values):
	drawdown_name = drawdown if isinstance(drawdown, str) else drawdown.name
	doc = frappe.get_doc(
		{
			"doctype": "Facility Repayment",
			"drawdown": drawdown_name,
			"repayment_date": getdate(repayment_date),
			**values,
		}
	).insert()
	if submit:
		doc.submit()
	return doc


# ---------------------------------------------------------------------------
# Phase 7g: lenders, Islamic products, installment cheques
# ---------------------------------------------------------------------------

LENDER = "_Test Treasury Lender"
# Treasury Company Settings `facility_voucher`: the 1.0 test classes pin the Journal Entries.
FACILITY_PE = {"facility_voucher": "Payment Entry"}
FACILITY_JE = {"facility_voucher": "Journal Entry"}


def ensure_lender(name: str = LENDER) -> str:
	"""A finance company (EGP Supplier with the default payable account)."""
	return ensure_supplier(name)


def ensure_islamic_accounts() -> frappe._dict:
	"""Liability accounts of the Islamic / trust receipt sub-limits, CWIP, and the late payment accounts."""
	return frappe._dict(
		murabaha=ensure_account("Treasury Murabaha Liability EGP", "Current Liabilities"),
		ijarah=ensure_account("Treasury Ijarah Liability EGP", "Current Liabilities"),
		istisna=ensure_account("Treasury Istisna Liability EGP", "Current Liabilities"),
		trust_receipt=ensure_account("Treasury Trust Receipts EGP", "Current Liabilities"),
		cwip=ensure_account("Treasury CWIP", "Current Assets"),
		charity=ensure_account("Treasury Late Payment Charity", "Indirect Expenses"),
		charity_payable=ensure_account("Treasury Charity Payable", "Current Liabilities"),
		profit=ensure_account("Treasury Profit Expense", "Indirect Expenses"),
	)


def make_finance_facility(limits: list[dict] | None = None, lender: str | None = None, **values):
	"""A Finance Company facility (lender `_Test Treasury Lender`, no bank) with a Murabaha sub-limit."""
	accounts = ensure_islamic_accounts()
	if limits is None:
		limits = [
			{
				"limit_type": "Murabaha",
				"limit_amount": values.get("total_limit", 1_000_000),
				"interest_rate": 12,
				"liability_account": accounts.murabaha,
			}
		]
	values.setdefault("interest_expense_account", accounts.profit)
	return make_bank_facility(
		limits=limits, lender_type="Finance Company", lender=lender or ensure_lender(), bank=None, **values
	)


def make_installment_batch(drawdown, submit: bool = True, handover: bool = True, **values):
	"""The Cheque Batch of a PDC to Lender drawdown's open installments (its own cheque numbers, no book)."""
	from treasury.bank_facilities import facility_cheques

	name = drawdown if isinstance(drawdown, str) else drawdown.name
	data = facility_cheques.make_installment_batch(name)
	data.pop("name", None)
	data.pop("__islocal", None)
	data["cheque_book"] = None
	if not handover:
		data["handover_date"] = None
	for row in data["cheques"]:
		row.pop("name", None)
		row.pop("parent", None)
		row["cheque_no"] = unique_cheque_no()
	data.update(values)
	batch = frappe.get_doc(data).insert()
	if submit:
		batch.submit()
		batch.reload()
	return batch


def liability_voucher_count(account: str, drawdown: str) -> int:
	"""Distinct non-cancelled vouchers with GL on `account` settling the drawdown's installments: those of its
	repayments and of its installment cheques (the drawdown's own vouchers are not counted)."""
	from treasury.bank_facilities.utilisation import DRAWDOWN, drawdown_sources

	sources = drawdown_sources([drawdown])
	sources.pop((DRAWDOWN, drawdown), None)
	if not sources:
		return 0
	gle = frappe.qb.DocType("GL Entry")
	vouchers = set()
	for doctype in ("Payment Entry", "Journal Entry"):
		voucher = frappe.qb.DocType(doctype)
		rows = (
			frappe.qb.from_(gle)
			.join(voucher)
			.on(voucher.name == gle.voucher_no)
			.select(gle.voucher_no, voucher.treasury_source_doctype, voucher.treasury_source_name)
			.distinct()
			.where(
				(gle.account == account)
				& (gle.voucher_type == doctype)
				& (gle.is_cancelled == 0)
				& (voucher.treasury_source_name.isin(sorted({s[1] for s in sources})))
			)
		).run(as_dict=True)
		for row in rows:
			if (row.treasury_source_doctype, row.treasury_source_name) in sources:
				vouchers.add((doctype, row.voucher_no))
	return len(vouchers)


def ensure_instrument_accounts() -> frappe._dict:
	"""Accounts of the Letters of Guarantee / Credit and Cheque Discounting tests (Phase 5b)."""
	return frappe._dict(
		lg_margin=ensure_account("Treasury LG Margins EGP", "Current Assets"),
		lc_margin=ensure_account("Treasury LC Margins EGP", "Current Assets"),
		lc_margin_usd=ensure_account("Treasury LC Margins USD", "Current Assets", currency="USD"),
		lg_liability=ensure_account("Treasury LG Claims Funded EGP", "Current Liabilities"),
		discounted=ensure_account("Treasury Discounted Cheques EGP", "Current Liabilities"),
		discounted_usd=ensure_account(
			"Treasury Discounted Cheques USD", "Current Liabilities", currency="USD"
		),
		claims_expense=ensure_account("Treasury LG Claims Expense", "Indirect Expenses"),
		discount_expense=ensure_account("Treasury Discount Expense", "Indirect Expenses"),
		lc_charges=ensure_account("Treasury LC Charges", "Indirect Expenses"),
	)


def make_letter_of_guarantee(facility, amount: float = 100_000, submit: bool = True, **values):
	accounts = ensure_instrument_accounts()
	doc = frappe.get_doc(
		{
			"doctype": "Letter of Guarantee",
			"facility": facility if isinstance(facility, str) else facility.name,
			"lg_type": "Performance",
			"lg_no": f"LG-{frappe.generate_hash(length=6)}",
			"beneficiary_type": "Customer",
			"beneficiary": CUSTOMER,
			"amount": amount,
			"issue_date": getdate("2025-03-01"),
			"expiry_date": getdate("2025-12-31"),
			"margin_account": accounts.lg_margin,
			**values,
		}
	).insert()
	if submit:
		doc.submit()
	return doc


def make_letter_of_credit(facility, amount: float = 100_000, submit: bool = True, **values):
	accounts = ensure_instrument_accounts()
	doc = frappe.get_doc(
		{
			"doctype": "Letter of Credit",
			"facility": facility if isinstance(facility, str) else facility.name,
			"lc_type": "Sight",
			"lc_no": f"LC-{frappe.generate_hash(length=6)}",
			"supplier": SUPPLIER,
			"amount": amount,
			"issue_date": getdate("2025-02-01"),
			"expiry_date": getdate("2025-08-31"),
			"margin_account": accounts.lc_margin,
			"charges_account": accounts.lc_charges,
			**values,
		}
	).insert()
	if submit:
		doc.submit()
	return doc


def make_cheque_discounting(
	facility, cheques: list, discount_date="2025-03-31", submit: bool = True, **values
):
	accounts = ensure_instrument_accounts()
	doc = frappe.get_doc(
		{
			"doctype": "Cheque Discounting",
			"facility": facility if isinstance(facility, str) else facility.name,
			"discount_date": getdate(discount_date),
			"discount_expense_account": accounts.discount_expense,
			"items": [{"pdc_cheque": c if isinstance(c, str) else c.name} for c in cheques],
			**values,
		}
	).insert()
	if submit:
		doc.submit()
	return doc


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


def make_usd_purchase_invoice(
	amount: float, posting_date=None, conversion_rate: float = USD_RATE, supplier: str = USD_SUPPLIER
):
	"""Purchase Invoice in USD of the USD supplier (party account in USD), at `conversion_rate`."""
	posting_date = getdate(posting_date or today())
	pi = frappe.get_doc(
		{
			"doctype": "Purchase Invoice",
			"company": COMPANY,
			"supplier": supplier,
			"posting_date": posting_date,
			"set_posting_time": 1,
			"due_date": add_days(posting_date, 30),
			"currency": "USD",
			"conversion_rate": conversion_rate,
			"buying_price_list": BUYING_PRICE_LIST,
			"plc_conversion_rate": 1,
			"credit_to": acc("Treasury Creditors USD"),
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
# Phase 7c: pay / receive from documents
# ---------------------------------------------------------------------------

LEAD_NAME = "_Test Treasury Lead"
EXPENSE_CLAIM_TYPE = "_Test Treasury Travel"
# The "Pay / Receive from Documents" settings every 7c test starts from (the shipped defaults, no default banks).
DOCUMENT_SETTINGS = {
	"default_outgoing_cheque_bank_account": None,
	"default_incoming_cheque_bank_account": None,
	"auto_select_cheque_book": 1,
	"default_installment_count": 12,
	"default_installment_frequency": "Monthly",
	"tender_bond_percent": 0,
	# shipped default 10; 0 here so that order-based security cheques start without an amount
	"performance_bond_percent": 0,
}


def settings_for_documents(**values) -> dict:
	"""DOCUMENT_SETTINGS with `values` (for `use_company_settings`)."""
	return {**DOCUMENT_SETTINGS, **values}


def make_purchase_order(amount: float, transaction_date=None, supplier: str = SUPPLIER, submit: bool = True):
	"""Purchase Order of one non-stock item, in the company currency."""
	transaction_date = getdate(transaction_date or today())
	po = frappe.get_doc(
		{
			"doctype": "Purchase Order",
			"company": COMPANY,
			"supplier": supplier,
			"transaction_date": transaction_date,
			"schedule_date": add_days(transaction_date, 30),
			"currency": "EGP",
			"conversion_rate": 1,
			"buying_price_list": BUYING_PRICE_LIST,
			"plc_conversion_rate": 1,
			"ignore_pricing_rule": 1,
			"items": [
				{
					"item_code": ITEM,
					"qty": 1,
					"rate": amount,
					"schedule_date": add_days(transaction_date, 30),
					"cost_center": acc("Main"),
					"expense_account": acc("Cost of Goods Sold"),
				}
			],
		}
	)
	po.insert(ignore_permissions=True)
	if submit:
		po.submit()
	return po


def ensure_lead() -> str:
	existing = frappe.db.get_value("Lead", {"first_name": LEAD_NAME})
	if existing:
		return existing
	return (
		frappe.get_doc({"doctype": "Lead", "first_name": LEAD_NAME, "company_name": f"{LEAD_NAME} Co"})
		.insert(ignore_permissions=True)
		.name
	)


def make_quotation(amount: float, quotation_to: str = "Customer", submit: bool = False, valid_days: int = 30):
	"""Quotation of one non-stock item to CUSTOMER or to the test Lead (draft unless `submit`)."""
	party = CUSTOMER if quotation_to == "Customer" else ensure_lead()
	date = getdate(today())
	quotation = frappe.get_doc(
		{
			"doctype": "Quotation",
			"company": COMPANY,
			"quotation_to": quotation_to,
			"party_name": party,
			"order_type": "Sales",
			"transaction_date": date,
			"valid_till": add_days(date, valid_days),
			"currency": "EGP",
			"conversion_rate": 1,
			"selling_price_list": SELLING_PRICE_LIST,
			"plc_conversion_rate": 1,
			"ignore_pricing_rule": 1,
			"items": [{"item_code": ITEM, "qty": 1, "rate": amount}],
		}
	)
	quotation.insert(ignore_permissions=True)
	if submit:
		quotation.submit()
	return quotation


def make_supplier_quotation(amount: float, supplier: str = SUPPLIER, submit: bool = False):
	date = getdate(today())
	quotation = frappe.get_doc(
		{
			"doctype": "Supplier Quotation",
			"company": COMPANY,
			"supplier": supplier,
			"transaction_date": date,
			"valid_till": add_days(date, 30),
			"currency": "EGP",
			"conversion_rate": 1,
			"buying_price_list": BUYING_PRICE_LIST,
			"plc_conversion_rate": 1,
			"ignore_pricing_rule": 1,
			"items": [{"item_code": ITEM, "qty": 1, "rate": amount, "uom": UOM, "conversion_factor": 1}],
		}
	)
	quotation.insert(ignore_permissions=True)
	if submit:
		quotation.submit()
	return quotation


def make_party_journal_entry(
	party_type: str,
	party: str,
	amount: float,
	debit_account: str,
	submit: bool = True,
	party_account: str | None = None,
	posting_date=None,
):
	"""A Journal Entry opening `amount` for a party against `debit_account` (the other row): a payable party is
	credited (Dr `debit_account` / Cr party), a receivable party debited (Dr party / Cr `debit_account`)."""
	receivable = frappe.get_cached_value("Party Type", party_type, "account_type") == "Receivable"
	party_account = party_account or frappe.get_cached_value(
		"Company", COMPANY, "default_receivable_account" if receivable else "default_payable_account"
	)
	party_side = "debit_in_account_currency" if receivable else "credit_in_account_currency"
	other_side = "credit_in_account_currency" if receivable else "debit_in_account_currency"
	je = frappe.get_doc(
		{
			"doctype": "Journal Entry",
			"company": COMPANY,
			"posting_date": getdate(posting_date or today()),
			"accounts": [
				{"account": debit_account, other_side: amount, "cost_center": acc("Main")},
				{
					"account": party_account,
					"party_type": party_type,
					"party": party,
					party_side: amount,
					"cost_center": acc("Main"),
				},
			],
		}
	)
	je.insert(ignore_permissions=True)
	if submit:
		je.submit()
	return je


def hrms_installed() -> bool:
	return "hrms" in frappe.get_installed_apps()


def make_expense_claim(amount: float, employee: str | None = None, payable_account: str | None = None):
	"""An approved, submitted Expense Claim of the test Employee (hrms only), booked on `payable_account`
	(default: the company's default payable account, the Employee's default party account)."""
	employee = employee or ensure_employee()
	if not frappe.db.exists("Expense Claim Type", EXPENSE_CLAIM_TYPE):
		frappe.get_doc(
			{
				"doctype": "Expense Claim Type",
				"expense_type": EXPENSE_CLAIM_TYPE,
				"accounts": [{"company": COMPANY, "default_account": acc("Cost of Goods Sold")}],
			}
		).insert(ignore_permissions=True)
	claim = frappe.get_doc(
		{
			"doctype": "Expense Claim",
			"company": COMPANY,
			"employee": employee,
			"posting_date": getdate(today()),
			"approval_status": "Approved",
			"currency": "EGP",
			"payable_account": payable_account
			or frappe.get_cached_value("Company", COMPANY, "default_payable_account"),
			"cost_center": acc("Main"),
			"expenses": [
				{
					"expense_date": getdate(today()),
					"expense_type": EXPENSE_CLAIM_TYPE,
					"amount": amount,
					"sanctioned_amount": amount,
					"cost_center": acc("Main"),
				}
			],
		}
	)
	# an approver may be mandatory in HR Settings; the claim is approved already
	claim.flags.ignore_mandatory = True
	claim.insert(ignore_permissions=True)
	claim.submit()
	return claim


def set_pdc_accounts(testcase, bank_accounts: list[str]):
	"""Keep only the PDC Accounts rows of `bank_accounts` for the current test (restored by a cleanup)."""
	previous = [dict(row) for row in _child_rows("PDC Account Mapping", "pdc_accounts")]
	kept = [row for row in previous if row["bank_account"] in bank_accounts]
	_write_child_rows("PDC Account Mapping", "pdc_accounts", kept)
	testcase.addCleanup(_write_child_rows, "PDC Account Mapping", "pdc_accounts", previous)


def isolate_cheque_books(testcase, bank_account: str):
	"""Close the other active cheque books of `bank_account` for the current test (restored by a cleanup), so
	`default_cheque_book` picks the one the test creates."""
	books = frappe.get_all(
		"Cheque Book", filters={"bank_account": bank_account, "status": "Active"}, pluck="name"
	)
	for name in books:
		frappe.db.set_value("Cheque Book", name, "status", "Closed", update_modified=False)
		testcase.addCleanup(
			frappe.db.set_value, "Cheque Book", name, "status", "Active", update_modified=False
		)


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
# Phase 7a: modes of payment, open party, accounting dimensions
# ---------------------------------------------------------------------------

MAP_DOCTYPE = "Treasury Payment Mode Map"
SHAREHOLDER_TITLE = "_Test Treasury Shareholder"
BRANCH = "_Test Treasury Branch"


def ensure_mode(name: str, company: str = COMPANY, account: str | None = None, enabled: int = 1) -> str:
	"""A Mode of Payment (type Bank) with `account` as the default account of `company` (no row without one)."""
	if not frappe.db.exists("Mode of Payment", name):
		frappe.get_doc(
			{
				"doctype": "Mode of Payment",
				"mode_of_payment": name,
				"type": "Bank",
				"enabled": enabled,
				"accounts": [{"company": company, "default_account": account}] if account else [],
			}
		).insert(ignore_permissions=True)
	return name


def _map_rows() -> list[dict]:
	return frappe.get_all(
		MAP_DOCTYPE,
		filters={"parenttype": "Treasury Company Settings", "parent": COMPANY},
		fields=["bank_account", "method", "mode_of_payment", "pe_naming_series"],
		order_by="idx asc",
	)


def _write_map_rows(rows: list[dict]):
	frappe.db.delete(MAP_DOCTYPE, {"parenttype": "Treasury Company Settings", "parent": COMPANY})
	for idx, row in enumerate(rows, 1):
		frappe.get_doc(
			{
				"doctype": MAP_DOCTYPE,
				"name": frappe.generate_hash(length=10),
				"parenttype": "Treasury Company Settings",
				"parentfield": "payment_modes",
				"parent": COMPANY,
				"idx": idx,
				**row,
			}
		).db_insert()


def set_mode_map(testcase, rows: list[dict]):
	"""Replace the Modes of Payment map of the test company for the current test (restored by a cleanup).

	Written straight to the table (no settings validation): the validation has tests of its own."""
	previous = [dict(row) for row in _map_rows()]
	_write_map_rows(rows)
	testcase.addCleanup(_write_map_rows, previous)


def ensure_shareholder() -> str:
	"""A Shareholder of the test company (named by ERPNext's series)."""
	existing = frappe.db.get_value("Shareholder", {"title": SHAREHOLDER_TITLE, "company": COMPANY})
	if existing:
		return existing
	return (
		frappe.get_doc({"doctype": "Shareholder", "title": SHAREHOLDER_TITLE, "company": COMPANY})
		.insert(ignore_permissions=True)
		.name
	)


def ensure_branch_dimension() -> str:
	"""The persistent Accounting Dimension on Branch of the test site; returns its fieldname.

	Creating an Accounting Dimension adds custom fields (DDL, which commits implicitly) to every doctype of the
	`accounting_dimension_doctypes` hook, so it is created once and never deleted, like the roles and custom
	fields of the app. Call it before any test data exists (first in `setUpClass`); the commit below only
	closes the DDL's own transaction. Branch values are created inside the tests (`ensure_branch`).
	"""
	name = frappe.db.get_value("Accounting Dimension", {"document_type": "Branch"}, "name")
	if not name:
		name = (
			frappe.get_doc({"doctype": "Accounting Dimension", "document_type": "Branch"})
			.insert(ignore_permissions=True)
			.name
		)
		frappe.db.commit()  # nosemgrep: persistent fixture (see docstring)
	fieldname = frappe.db.get_value("Accounting Dimension", name, "fieldname")
	from treasury.accounting.dimensions import TREASURY_DIMENSION_DOCTYPES, create_dimension_fields

	if not all(frappe.get_meta(doctype).has_field(fieldname) for doctype in TREASURY_DIMENSION_DOCTYPES):
		# a dimension created before the app's hook was loaded (or before a Treasury doctype existed)
		create_dimension_fields()
		frappe.db.commit()  # nosemgrep: persistent fixture (see docstring)
	return fieldname


def ensure_branch(name: str = BRANCH) -> str:
	return _ensure("Branch", name, {"branch": name})


def get_pe(name: str):
	return frappe.get_doc("Payment Entry", name)


# ---------------------------------------------------------------------------
# Phase 7b: bank transfers
# ---------------------------------------------------------------------------

OTHER_BANK = "_Test Treasury Other Bank"
BANK_SWIFT = "TTRBQAQA"
OTHER_BANK_SWIFT = "OTHBQAQA"
FEE_RULE_DOCTYPE = "Bank Transfer Fee Rule"
LIMIT_DOCTYPE = "Bank Transfer Limit"
TRANSFER_SETTINGS = {
	"default_transfer_method": "Normal",
	"default_charge_bearer": "SHA",
	"transfer_completion": "On Bank Confirmation",
	"refund_fees_on_return": 0,
	"transfer_limit_action": "Warn",
	"transfer_approval_threshold": 0,
}


def make_iban(bank_code: str, account: str | None = None, country: str = "QA") -> str:
	"""A valid IBAN (mod-97 check digits, which the Bank Account IBAN field validates) with `bank_code` in
	characters 5-8."""
	bban = f"{bank_code}{account or frappe.generate_hash(length=21).upper()}".upper()

	def encode(text: str) -> str:
		return "".join(str(int(char, 36)) for char in text)

	check = 98 - int(encode(f"{bban}{country}00")) % 97
	return f"{country}{check:02d}{bban}"


def ensure_other_bank(name: str = OTHER_BANK, swift: str = OTHER_BANK_SWIFT) -> str:
	"""A second Bank with its SWIFT code; the test Bank gets the SWIFT code BANK_SWIFT."""
	_ensure("Bank", BANK, {"bank_name": BANK})
	if frappe.db.get_value("Bank", BANK, "swift_number") != BANK_SWIFT:
		frappe.db.set_value("Bank", BANK, "swift_number", BANK_SWIFT)
	_ensure("Bank", name, {"bank_name": name, "swift_number": swift})
	return name


def ensure_other_company_bank() -> frappe._dict:
	"""An EGP company bank account of the test company at the Other Bank (own GL account)."""
	ensure_other_bank()
	bank_gl = ensure_account("Treasury Bank EGP Other", "Bank Accounts", "Bank")
	name = f"Treasury EGP Other - {OTHER_BANK}"
	if not frappe.db.exists("Bank Account", name):
		frappe.get_doc(
			{
				"doctype": "Bank Account",
				"account_name": "Treasury EGP Other",
				"bank": OTHER_BANK,
				"account": bank_gl,
				"company": COMPANY,
				"is_company_account": 1,
			}
		).insert(ignore_permissions=True)
	return frappe._dict(currency="EGP", bank_gl=bank_gl, bank_account=name)


def ensure_party_bank_account(
	party_type: str, party: str, bank: str | None = None, iban: str | None = None, is_default: int = 1
) -> str:
	"""A bank account of a party (not a company account), at `bank` (default the Other Bank)."""
	bank = bank or ensure_other_bank()
	return (
		frappe.get_doc(
			{
				"doctype": "Bank Account",
				"account_name": f"Party {frappe.generate_hash(length=6)}",
				"bank": bank,
				"party_type": party_type,
				"party": party,
				"iban": iban,
				"is_company_account": 0,
				"is_default": is_default,
			}
		)
		.insert(ignore_permissions=True)
		.name
	)


def _child_rows(doctype: str, parentfield: str) -> list[dict]:
	fields = [df.fieldname for df in frappe.get_meta(doctype).fields if df.fieldtype not in ("Column Break",)]
	return frappe.get_all(
		doctype,
		filters={"parenttype": "Treasury Company Settings", "parent": COMPANY, "parentfield": parentfield},
		fields=fields,
		order_by="idx asc",
	)


def _write_child_rows(doctype: str, parentfield: str, rows: list[dict]):
	frappe.db.delete(
		doctype, {"parenttype": "Treasury Company Settings", "parent": COMPANY, "parentfield": parentfield}
	)
	for idx, row in enumerate(rows, 1):
		frappe.get_doc(
			{
				"doctype": doctype,
				"name": frappe.generate_hash(length=10),
				"parenttype": "Treasury Company Settings",
				"parentfield": parentfield,
				"parent": COMPANY,
				"idx": idx,
				**row,
			}
		).db_insert()


def set_fee_rules(testcase, rows: list[dict]):
	"""Replace the Transfer Fee Rules of the test company for the current test (restored by a cleanup).
	Written straight to the table (no settings validation): the validation has tests of its own."""
	previous = [dict(row) for row in _child_rows(FEE_RULE_DOCTYPE, "transfer_fee_rules")]
	_write_child_rows(FEE_RULE_DOCTYPE, "transfer_fee_rules", rows)
	testcase.addCleanup(_write_child_rows, FEE_RULE_DOCTYPE, "transfer_fee_rules", previous)


def set_transfer_limits(testcase, rows: list[dict]):
	"""Replace the Transfer Limits of the test company for the current test (restored by a cleanup)."""
	previous = [dict(row) for row in _child_rows(LIMIT_DOCTYPE, "transfer_limits")]
	_write_child_rows(LIMIT_DOCTYPE, "transfer_limits", rows)
	testcase.addCleanup(_write_child_rows, LIMIT_DOCTYPE, "transfer_limits", previous)


def fee_rule(
	bank_account: str, method: str, direction: str = "Outgoing", scope: str = "Other Bank", **values
):
	return {
		"bank_account": bank_account,
		"method": method,
		"direction": direction,
		"scope": scope,
		"amount_from": 0,
		"amount_to": 0,
		"fixed_fee": 0,
		"fee_percent": 0,
		"min_fee": 0,
		"max_fee": 0,
		"charge_bearer": None,
		"correspondent_fee": 0,
		"expense_account": acc("Treasury Bank Charges"),
		**values,
	}


def standard_fee_rules() -> list[dict]:
	"""The rules R1-R7 of spec 7b §7 (EGP bank B1, USD bank BU; expense Treasury Bank Charges)."""
	b1, bu = bank_account_name("EGP"), bank_account_name("USD")
	return [
		fee_rule(b1, "Fawran", amount_from=0, amount_to=1000, fixed_fee=0.5),
		fee_rule(b1, "Fawran", amount_from=1000.01, amount_to=50000, fixed_fee=2),
		fee_rule(b1, "Normal", fixed_fee=10, fee_percent=0.1, min_fee=15, max_fee=50),
		fee_rule(b1, "Normal", direction="Incoming", scope="Any", fixed_fee=5),
		fee_rule(bu, "SWIFT", scope="International", charge_bearer="OUR", fixed_fee=25, correspondent_fee=15),
		fee_rule(bu, "SWIFT", scope="International", charge_bearer="SHA", fixed_fee=25),
		fee_rule(bu, "Normal", fixed_fee=5),
	]


def make_bank_transfer(submit: bool = True, **values):
	"""A Bank Transfer (a Normal payment of 1,000 from the EGP bank to SUPPLIER today, to an IBAN at the
	Other Bank, unless `values` say otherwise); submitted unless `submit` is False."""
	ensure_other_bank()
	transfer_type = values.get("transfer_type", "Party Payment")
	base = {
		"doctype": "Bank Transfer",
		"company": COMPANY,
		"transfer_type": transfer_type,
		"method": "Normal",
		"bank_account": bank_account_name("EGP"),
		"amount": 1000,
		"request_date": getdate(today()),
		"execution_date": getdate(today()),
	}
	if transfer_type != "Internal Transfer":
		base.update(
			party_type="Supplier" if transfer_type == "Party Payment" else "Customer",
			party=SUPPLIER if transfer_type == "Party Payment" else CUSTOMER,
		)
		if transfer_type == "Party Payment":
			base["beneficiary_iban"] = make_iban("OTHB")
	doc = frappe.get_doc({**base, **values})
	doc.insert()
	if submit:
		doc.submit()
		doc.reload()
	return doc


def get_transfer(name: str):
	return frappe.get_doc("Bank Transfer", name)


def assert_deductions(testcase, pe, expected: list[tuple]):
	"""Deduction rows of a Payment Entry, as (account, amount, is_exchange_gain_loss), order-independent."""
	actual = sorted((d.account, flt(d.amount, 2), int(d.is_exchange_gain_loss)) for d in pe.deductions)
	wanted = sorted((account, flt(amount, 2), int(flag)) for account, amount, flag in expected)
	testcase.assertEqual(wanted, actual, f"deductions of {pe.name}")


def make_bank_transaction(bank_account: str, date, deposit=0, withdrawal=0, **values):
	"""A submitted Bank Transaction in the currency of the bank account."""
	gl_account = frappe.db.get_value("Bank Account", bank_account, "account")
	currency = frappe.get_cached_value("Account", gl_account, "account_currency")
	doc = frappe.get_doc(
		{
			"doctype": "Bank Transaction",
			"date": getdate(date),
			"bank_account": bank_account,
			"currency": currency,
			"deposit": deposit,
			"withdrawal": withdrawal,
			**values,
		}
	)
	doc.insert(ignore_permissions=True)
	doc.submit()
	return doc


# ---------------------------------------------------------------------------
# Phase 7d: bank charges
# ---------------------------------------------------------------------------

CHARGE_CATEGORY = "_Test Treasury Charge"
# The Bank Charge settings every 7d test starts from (the shipped defaults).
BANK_CHARGE_SETTINGS = {"bank_charge_voucher": "Payment Entry", "bank_charge_clearance": "On Posting"}
BANK_SUPPLIER_DOCTYPE = "Treasury Bank Supplier"


def ensure_charge_category(
	name: str = CHARGE_CATEGORY,
	keywords=("acct maint fee",),
	account: str | None = None,
	company: str = COMPANY,
	auto_select: int = 1,
	**values,
) -> str:
	"""A Bank Charge Category with `keywords` and an account row of `company` (`account` False: no row)."""
	if not frappe.db.exists("Bank Charge Category", name):
		accounts = []
		if account is not False:
			accounts = [{"company": company, "expense_account": account or acc("Treasury Bank Charges")}]
		frappe.get_doc(
			{
				"doctype": "Bank Charge Category",
				"category_name": name,
				"keywords": "\n".join(keywords or ()),
				"auto_select_proposals": auto_select,
				"accounts": accounts,
				**values,
			}
		).insert(ignore_permissions=True)
	return name


def make_bank_charge(submit: bool = True, **values):
	"""A Bank Charge of 150 on the EGP bank dated day(-5), of the test category; submitted unless `submit` is
	False."""
	category = values.pop("category", None) or ensure_charge_category()
	doc = frappe.get_doc(
		{
			"doctype": "Bank Charge",
			"entry_type": "Charge",
			"company": COMPANY,
			"bank_account": bank_account_name("EGP"),
			"category": category,
			"amount": 150,
			"posting_date": day(-5),
			**values,
		}
	)
	doc.insert()
	if submit:
		doc.submit()
		doc.reload()
	return doc


def get_charge(name: str):
	return frappe.get_doc("Bank Charge", name)


def _bank_supplier_rows() -> list[dict]:
	return frappe.get_all(
		BANK_SUPPLIER_DOCTYPE,
		filters={"parenttype": "Treasury Settings", "parent": "Treasury Settings"},
		fields=["bank", "supplier"],
		order_by="idx asc",
	)


def _write_bank_supplier_rows(rows: list[dict]):
	frappe.db.delete(
		BANK_SUPPLIER_DOCTYPE, {"parenttype": "Treasury Settings", "parent": "Treasury Settings"}
	)
	for idx, row in enumerate(rows, 1):
		frappe.get_doc(
			{
				"doctype": BANK_SUPPLIER_DOCTYPE,
				"name": frappe.generate_hash(length=10),
				"parenttype": "Treasury Settings",
				"parentfield": "bank_suppliers",
				"parent": "Treasury Settings",
				"idx": idx,
				**row,
			}
		).db_insert()


def set_bank_suppliers(testcase, rows: list[dict]):
	"""Replace Treasury Settings → Bank Suppliers for the current test (restored by a cleanup)."""
	previous = [dict(row) for row in _bank_supplier_rows()]
	_write_bank_supplier_rows(rows)
	testcase.addCleanup(_write_bank_supplier_rows, previous)


# ---------------------------------------------------------------------------
# Phase 7e: bank position alerts
# ---------------------------------------------------------------------------

THRESHOLD_DOCTYPE = "Treasury Balance Threshold"
RECIPIENT_DOCTYPE = "Treasury Alert Recipient"
ALERT_LOG_DOCTYPE = "Treasury Alert Log"
# The Treasury Settings every 7e test starts from (the shipped defaults).
ALERT_SETTINGS = {
	"balance_alert_horizon": 7,
	"balance_digest_hour": 7,
	"send_balance_digest": 1,
	"send_balance_alerts": 1,
	"balance_alert_notification": 1,
	"balance_alert_email": 1,
	"digest_zero_days": 1,
}


def ensure_alert_bank(currency: str = "EGP", name: str | None = None) -> frappe._dict:
	"""A company bank account with a GL account (account type Bank) and PDC accounts of its own, plus its PDC
	Accounts row (so cheques can be drawn on it): its balances belong to one test only. A random name unless
	`name` is given. Returns {bank_account, account, currency, pdc_payable, pdc_receivable, under_collection}."""
	name = name or f"Treasury Alert {currency} {frappe.generate_hash(length=6)}"
	cur = None if currency == "EGP" else currency
	account = ensure_account(f"{name} GL", "Bank Accounts", "Bank", cur)
	bank = frappe._dict(
		currency=currency,
		account=account,
		bank_gl=account,
		bank_account=ensure_bank_account(name, account),
		pdc_payable=ensure_account(f"{name} PDC Payable", "Current Liabilities", "Bank", cur),
		pdc_receivable=ensure_account(f"{name} PDC Receivable", "Current Assets", "Bank", cur),
		under_collection=ensure_account(f"{name} Under Collection", "Current Assets", "Bank", cur),
		bank_charges=ensure_account("Treasury Bank Charges", "Indirect Expenses"),
	)
	if not frappe.db.exists(
		"PDC Account Mapping", {"parenttype": "Treasury Company Settings", "bank_account": bank.bank_account}
	):
		# written directly: saving the settings would validate every other row (the thresholds included)
		idx = frappe.db.count(
			"PDC Account Mapping", {"parenttype": "Treasury Company Settings", "parent": COMPANY}
		)
		frappe.get_doc(
			{
				"doctype": "PDC Account Mapping",
				"name": frappe.generate_hash(length=10),
				"parenttype": "Treasury Company Settings",
				"parentfield": "pdc_accounts",
				"parent": COMPANY,
				"idx": idx + 1,
				**mapping_row(bank),
			}
		).db_insert()
	return bank


def fund_bank(bank: frappe._dict, amount: float, date, rate: float = 1.0) -> str:
	"""Money on the bank GL on `date` (amount in the bank currency), against Sales: a submitted Journal Entry."""
	je = frappe.get_doc(
		{
			"doctype": "Journal Entry",
			"company": COMPANY,
			"posting_date": getdate(date),
			"multi_currency": 1 if bank.currency != "EGP" else 0,
			"accounts": [
				{"account": bank.account, "debit_in_account_currency": amount, "exchange_rate": rate},
				{
					"account": acc("Sales"),
					"cost_center": acc("Main"),
					"credit_in_account_currency": flt(amount * rate, 2),
					"exchange_rate": 1,
				},
			],
		}
	)
	je.insert(ignore_permissions=True)
	je.submit()
	return je.name


def set_thresholds(testcase, rows: list[dict]):
	"""Replace the Balance Thresholds of the test company for the current test (restored by a cleanup).
	Rows: {bank_account, warning_balance, minimum_balance}; the currency is taken from the GL account.
	Written straight to the table (the validation has tests of its own)."""
	previous = [dict(row) for row in _child_rows(THRESHOLD_DOCTYPE, "balance_thresholds")]
	values = []
	for row in rows:
		gl_account = frappe.db.get_value("Bank Account", row["bank_account"], "account")
		values.append({"currency": frappe.get_cached_value("Account", gl_account, "account_currency"), **row})
	_write_child_rows(THRESHOLD_DOCTYPE, "balance_thresholds", values)
	testcase.addCleanup(_write_child_rows, THRESHOLD_DOCTYPE, "balance_thresholds", previous)


def set_alert_settings(testcase, **values):
	"""Treasury Settings values for the current test (restored by a cleanup; a value never stored before is
	removed again). Without values: ALERT_SETTINGS."""
	from treasury.utils.settings import stored_single_value

	values = values or ALERT_SETTINGS
	for fieldname, value in values.items():
		previous = stored_single_value("Treasury Settings", fieldname)
		frappe.db.set_single_value("Treasury Settings", fieldname, value)
		testcase.addCleanup(_restore_single, fieldname, previous)
	frappe.clear_document_cache("Treasury Settings", "Treasury Settings")


def _restore_single(fieldname: str, value):
	if value is None:
		frappe.db.delete("Singles", {"doctype": "Treasury Settings", "field": fieldname})
	else:
		frappe.db.set_single_value("Treasury Settings", fieldname, value)
	frappe.clear_document_cache("Treasury Settings", "Treasury Settings")


def _recipient_rows() -> list[dict]:
	return frappe.get_all(
		RECIPIENT_DOCTYPE,
		filters={"parenttype": "Treasury Settings", "parent": "Treasury Settings"},
		fields=["recipient_type", "role", "user"],
		order_by="idx asc",
	)


def _write_recipient_rows(rows: list[dict]):
	frappe.db.delete(RECIPIENT_DOCTYPE, {"parenttype": "Treasury Settings", "parent": "Treasury Settings"})
	for idx, row in enumerate(rows, 1):
		frappe.get_doc(
			{
				"doctype": RECIPIENT_DOCTYPE,
				"name": frappe.generate_hash(length=10),
				"parenttype": "Treasury Settings",
				"parentfield": "balance_alert_recipients",
				"parent": "Treasury Settings",
				"idx": idx,
				"recipient_type": "User" if row.get("user") else "Role",
				**row,
			}
		).db_insert()


def set_recipients(testcase, rows: list[dict]):
	"""Replace Treasury Settings → Recipients for the current test (restored by a cleanup). Rows: {"user": u}
	or {"role": r}."""
	previous = [dict(row) for row in _recipient_rows()]
	_write_recipient_rows(rows)
	testcase.addCleanup(_write_recipient_rows, previous)


def alert_logs(**filters) -> list[frappe._dict]:
	"""Treasury Alert Logs matching `filters`, oldest first."""
	return frappe.get_all(
		ALERT_LOG_DOCTYPE,
		filters=filters,
		fields=[
			"name",
			"alert_type",
			"level",
			"company",
			"bank_account",
			"alert_date",
			"projected_date",
			"balance",
			"threshold",
			"is_escalation",
			"user",
			"recipients",
			"notified",
			"emailed",
			"reference_doctype",
			"reference_name",
			"subject",
		],
		order_by="creation asc, projected_date asc",
	)


def make_alert_fixture_e1(testcase) -> frappe._dict:
	"""Spec 7e §4, E1: an EGP bank of its own, days T .. T+6 (T = real today), warning 50,000 / minimum 20,000.

	Opening (GL up to T-1) 100,000 = 60,000 funded on T-2 + a 40,000 drawdown on T-1; no GL movement today.
	T+1 outgoing cheque C1 30,000 delivered; T+2 incoming cheque C2 10,000 received; T+3 Scheduled transfer TR1
	50,000 + fee 20; T+5 the drawdown's single installment 40,000 (interest 0).
	Closings: T 100,000 · T+1 70,000 · T+2 80,000 · T+3 29,980 (Warning) · T+4 29,980 · T+5 -10,020 (Critical)
	· T+6 -10,020. The thresholds are set last (the fixture itself is not checked)."""
	t0 = today_date()
	bank = ensure_alert_bank()
	fund_bank(bank, 60000, add_days(t0, -2))
	facility = make_bank_facility(
		bank_account=bank.bank_account,
		start_date=add_days(t0, -30),
		expiry_date=add_days(t0, 365),
		limits=[{"limit_type": "Short Term Loan", "limit_amount": 1_000_000, "interest_rate": 0}],
	)
	drawdown = make_drawdown(
		facility,
		amount=40000,
		drawdown_date=add_days(t0, -1),
		tenor_months=1,
		frequency="Bullet",
		method="Bullet",
		first_repayment_date=add_days(t0, 5),
	)
	c1 = make_pdc_cheque(
		amount=30000, issue_date=t0, due_date=add_days(t0, 1), bank_account=bank.bank_account
	)
	c1.deliver(handover_date=t0)
	c2 = make_pdc_cheque(
		"Incoming", amount=10000, issue_date=t0, due_date=add_days(t0, 2), bank_account=bank.bank_account
	)
	c2.receive(receipt_date=t0)
	set_fee_rules(testcase, [fee_rule(bank.bank_account, "Normal", fixed_fee=20)])
	tr1 = make_bank_transfer(amount=50000, bank_account=bank.bank_account, execution_date=add_days(t0, 3))
	set_thresholds(
		testcase, [{"bank_account": bank.bank_account, "warning_balance": 50000, "minimum_balance": 20000}]
	)
	return frappe._dict(
		bank=bank,
		bank_account=bank.bank_account,
		today=t0,
		facility=facility.name,
		drawdown=drawdown.name,
		c1=c1.name,
		c2=c2.name,
		tr1=tr1.name,
	)


def today_date():
	return getdate(today())


class BalanceAlertTestMixin(TreasuryTestMixin):
	"""Phase 7e test classes: dates relative to the real today; every test starts from the shipped alert
	settings, no thresholds, no recipients and On Low Projected Balance = Warn; `frappe.enqueue` is replaced by
	a mock (`self.enqueue`), so the event trigger is recorded, not queued.

	IntegrationTestCase rolls back only at the end of the class, so the alert logs and bank notifications of
	the previous tests of the class would still be there: the dedupe keys (Digest: company, user, day) would
	suppress this test's sends. Every test therefore starts without Treasury Alert Logs of the test company and
	without Bank Account notifications (deleted in the class transaction, restored by its rollback)."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		for offset in (-35, 0, 40, 400):
			ensure_fiscal_year(COMPANY, add_days(today(), offset))

	def setUp(self):
		from unittest.mock import patch

		super().setUp()
		self.use_company_settings(
			**DEFAULT_PDC_SETTINGS, **TRANSFER_SETTINGS, **FACILITY_JE, on_low_balance="Warn"
		)
		set_alert_settings(self)
		set_thresholds(self, [])
		set_recipients(self, [])
		frappe.db.delete(ALERT_LOG_DOCTYPE, {"company": COMPANY})
		frappe.db.delete("Notification Log", {"document_type": "Bank Account"})
		enqueue = patch("frappe.enqueue")
		self.enqueue = enqueue.start()
		self.addCleanup(enqueue.stop)
		self.today = today_date()

	def day(self, offset: int):
		"""The real today + `offset` days."""
		return getdate(add_days(self.today, offset))

	def balance_jobs(self) -> list:
		"""The event-trigger calls recorded on the `frappe.enqueue` mock."""
		return [
			c
			for c in self.enqueue.call_args_list
			if c.args and c.args[0] == "treasury.balance_alerts.scheduler.evaluate_company"
		]


# ---------------------------------------------------------------------------
# Phase 7f: cheque leaves, cheque layouts
# ---------------------------------------------------------------------------


def make_cheque_layout(bank_account: str | None = None, **values):
	"""A Cheque Layout (default of its bank account unless `values` say otherwise) with the DocType defaults."""
	doc = frappe.get_doc(
		{
			"doctype": "Cheque Layout",
			"layout_name": f"Layout {frappe.generate_hash(length=6)}",
			"bank_account": bank_account or bank_account_name("EGP"),
			"is_default": 1,
			**values,
		}
	)
	doc.insert()
	return doc


def ensure_currency(code: str, fraction: str, units: int) -> str:
	"""A Currency master (created only if missing, e.g. QAR Dirham/100, KWD Fils/1000)."""
	if not frappe.db.exists("Currency", code):
		frappe.get_doc(
			{
				"doctype": "Currency",
				"currency_name": code,
				"fraction": fraction,
				"fraction_units": units,
				"smallest_currency_fraction_value": 1 / units,
				"enabled": 1,
			}
		).insert(ignore_permissions=True)
	return code


def leaf_rows(filters: dict) -> dict[int, frappe._dict]:
	"""The leaf rows of the Cheque Leaf Register (view Leaves), keyed by leaf number."""
	from treasury.pdc.report.cheque_leaf_register.cheque_leaf_register import execute

	result = execute({"company": COMPANY, **filters})
	return {row.leaf_number: row for row in result[1] if not row.get("is_group")}


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
				filters={
					"against_voucher_type": voucher_type,
					"against_voucher_no": voucher_no,
					"delinked": 0,
				},
				pluck="amount_in_account_currency",
			)
		),
		2,
	)
