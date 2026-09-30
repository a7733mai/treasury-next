"""The lender of a Bank Facility (spec 7g §3.1): a bank or a finance company, and its Islamic wording.

- The lender is a Supplier: a finance company's is chosen on the facility (`lender`); a bank's is created on
  first use from the Bank (treasury.bank_transfers.bank_supplier, the Supplier the bank charges are booked to)
  and stored on the facility. Facility Payment Entries book no party row (the whole amount is in deductions),
  so the supplier's balance is never affected.
- A finance company grants loans and Islamic financing only (FINANCE_COMPANY_TYPES): no overdraft, no bank
  instruments (LG, LC, trust receipts, cheque discounting).
- An Islamic facility says Profit where a conventional one says Interest (`terms`): forms, reports, remarks.
"""

import frappe
from frappe import _

from treasury.bank_transfers import bank_supplier

BANK = "Bank"
FINANCE_COMPANY = "Finance Company"
ISLAMIC_TYPES = ("Murabaha", "Tawarruq", "Ijarah", "Istisna'a")
FINANCE_COMPANY_TYPES = (
	"Short Term Loan",
	"Long Term Loan",
	"Murabaha",
	"Tawarruq",
	"Ijarah",
	"Istisna'a",
	"Invoice Discounting",
	"Other",
)


def get_lender_supplier(facility) -> str:
	"""The Supplier of the facility's lender (name or document): `lender`, else for a bank the Bank's Supplier,
	created / mapped on first use and stored as the (locked) `lender` of the facility. The caller checked the
	user's permission for the action that needs it."""
	doc = facility if not isinstance(facility, str) else frappe.get_doc("Bank Facility", facility)
	lender = doc.get("lender") or frappe.db.get_value("Bank Facility", doc.name, "lender")
	if lender:
		return lender
	if doc.get("lender_type") == FINANCE_COMPANY or not doc.get("bank"):
		frappe.throw(_("A Finance Company facility needs a Lender (Supplier)."))
	supplier = bank_supplier.ensure_bank_supplier(doc.bank)
	# the lender is locked once the facility left Draft: only an empty one is filled, never by a document save
	frappe.db.set_value("Bank Facility", doc.name, "lender", supplier, update_modified=False)
	doc.lender = supplier
	return supplier


def terms(is_islamic: bool) -> frappe._dict:
	"""The translated labels of an Islamic (Profit) or conventional (Interest) facility."""
	if is_islamic:
		return frappe._dict(
			interest=_("Profit"),
			interest_rate=_("Profit Rate (% p.a.)"),
			interest_paid=_("Profit Paid"),
			accrued_interest=_("Accrued Profit"),
			interest_expense_account=_("Profit Expense Account"),
		)
	return frappe._dict(
		interest=_("Interest"),
		interest_rate=_("Interest Rate (% p.a.)"),
		interest_paid=_("Interest Paid"),
		accrued_interest=_("Accrued Interest"),
		interest_expense_account=_("Interest Expense Account"),
	)


def mixed_terms() -> frappe._dict:
	"""The labels of a view mixing Islamic and conventional facilities."""
	return frappe._dict(
		interest=_("Interest / Profit"),
		interest_rate=_("Interest / Profit Rate (% p.a.)"),
		interest_paid=_("Interest / Profit Paid"),
		accrued_interest=_("Accrued Interest"),
		interest_expense_account=_("Interest Expense Account"),
	)


def terms_for(flags) -> frappe._dict:
	"""The labels of a set of rows by their `is_islamic` flags: all Islamic, none, or mixed (spec 7g §3.12);
	no rows at all: the mixed labels."""
	flags = {1 if f else 0 for f in flags}
	if flags == {1}:
		return terms(True)
	if flags == {0}:
		return terms(False)
	return mixed_terms()
