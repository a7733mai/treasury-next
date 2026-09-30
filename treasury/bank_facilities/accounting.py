"""Accounting of the Bank Facilities module: the vouchers of drawdowns, disbursement stages, repayments,
late payment charity and interest accruals.

Every voucher is created by `treasury.accounting.posting` (idempotent on source, event and cycle) and cancelled
by `posting.cancel_voucher`; nothing here writes GL rows itself.

Voucher (spec 7g §4.1, Treasury Company Settings `facility_voucher`): in the company currency with the setting
"Payment Entry", a drawdown (to the bank), a stage disbursement to the bank and a repayment are Payment Entries
with the lender as Supplier and the whole amount in deduction rows (`posting.create_deductions_payment`: no
party row, no Payment Ledger Entry, the supplier's balance is untouched). A foreign currency facility (deduction
rows are booked in the company currency) and the setting "Journal Entry" keep the Journal Entries. A
disbursement to another account (Ijarah / Istisna'a paid by the financier to the supplier / contractor), the
upfront fees of such a disbursement or of an Istisna'a, the late payment charity and the accruals are always
Journal Entries (no bank and party side). The party posting of an installment cheque (PDC to Lender) is always
a Payment Entry from the PDC Payable account (`post_repayment` with `paid_from`).

Currencies: the facility currency is the currency of the bank account, the liability accounts and the accrued
interest account; one voucher converts all of them at one exchange rate. Expense accounts (interest, fees,
penalty) are in the company currency and get the converted amount; the cents a per-row rounding may leave are
booked on the company's Exchange Gain / Loss account (`balance_exchange_difference`). Differences between the
drawdown rate and the repayment rate stay on the liability account, for ERPNext's Exchange Rate Revaluation.

Bank clearance: the bank itself credits the drawdown and debits the fees and repayments on the document's
date, so these vouchers carry `clearance_date` = their posting date (the Bank Transaction date of an auto debit
matched with the statement) and do not wait in the Bank Reconciliation Tool. Accruals, charity and disbursements
to another account have no bank row; an installment cheque's party posting is cleared by the cheque itself.
"""

import erpnext
import frappe
from frappe import _
from frappe.utils import flt

from treasury.accounting import payment_modes, posting
from treasury.bank_facilities import lenders

DRAWDOWN = "Facility Drawdown"
REPAYMENT = "Facility Repayment"
EVENT_DRAWDOWN = "facility_drawdown"
EVENT_FEES = "facility_upfront_fees"
EVENT_REPAYMENT = "facility_repayment"
# cycle = the stage row's idx
EVENT_STAGE = "facility_stage_disbursement"
EVENT_CHARITY = "facility_late_payment_charity"
# `interest_accrual:YYYY-MM`: one accrual per drawdown and month (idempotency key).
EVENT_ACCRUAL_PREFIX = "interest_accrual:"

PAYMENT_ENTRY_MODE = "Payment Entry"
JOURNAL_ENTRY_MODE = "Journal Entry"
DISBURSE_TO_BANK = "Bank Account"
DISBURSE_TO_OTHER = "Other Account"
SETTLED_BY_BANK, SETTLED_BY_AUTO_DEBIT, SETTLED_BY_CHEQUE = "Bank", "Auto Debit", "PDC Cheque"
PAID_TO_LENDER, PAYABLE_TO_CHARITY = "Paid to Lender", "Payable to Charity"


def accrual_event(month_end) -> str:
	return f"{EVENT_ACCRUAL_PREFIX}{month_end.strftime('%Y-%m')}"


def bank_gl_account(bank_account: str) -> str:
	account = frappe.db.get_value("Bank Account", bank_account, "account")
	if not account:
		frappe.throw(_("Bank Account {0} has no GL account.").format(frappe.bold(bank_account)))
	return account


def voucher_mode(company: str, currency: str) -> str:
	""" "Payment Entry" when the company's `facility_voucher` says so (the default) and the facility is in the
	company currency (spec 7g F2), else "Journal Entry"."""
	setting = frappe.db.get_value("Treasury Company Settings", company, "facility_voucher")
	if (setting or PAYMENT_ENTRY_MODE) == PAYMENT_ENTRY_MODE and not _is_foreign(company, currency):
		return PAYMENT_ENTRY_MODE
	return JOURNAL_ENTRY_MODE


def _is_foreign(company: str, currency: str) -> bool:
	return currency != erpnext.get_company_currency(company)


def _row(company: str, currency: str, account: str, rate: float, debit=0, credit=0) -> dict:
	"""Row on an account in the facility currency."""
	row = {
		"account": account,
		"debit_in_account_currency": flt(debit),
		"credit_in_account_currency": flt(credit),
	}
	if _is_foreign(company, currency):
		row["exchange_rate"] = flt(rate)
	return row


def _expense_row(company: str, account: str, amount: float, rate: float, precision: int) -> dict:
	"""Debit of a company-currency expense account with a facility-currency amount."""
	return {
		"account": account,
		"debit_in_account_currency": flt(flt(amount) * flt(rate), precision),
		"cost_center": erpnext.get_default_cost_center(company),
	}


def _precision() -> int:
	return frappe.get_precision("Journal Entry Account", "debit") or 2


def _details(company: str, bank_account: str, method: str) -> dict:
	"""Mode of Payment / series of the bank and method (Treasury Company Settings), the bank account and the
	company's default cost center (posting._apply_details)."""
	return {
		**payment_modes.resolve(company, bank_account, method),
		"bank_account": bank_account,
		"cost_center": erpnext.get_default_cost_center(company),
	}


def _lender(drawdown) -> str:
	return lenders.get_lender_supplier(frappe.get_doc("Bank Facility", drawdown.facility))


# ---------------------------------------------------------------------------
# Drawdown and disbursement stages
# ---------------------------------------------------------------------------


def post_drawdown(doc) -> frappe._dict:
	"""The principal and the upfront fees of a drawdown (spec 7g §4.2). Returns {journal_entry,
	fees_journal_entry, payment_entry}: in Payment Entry mode one Receive from the lender (deductions: the
	liability as a negative row, the fees as a positive one), in Journal Entry mode Dr Bank / Cr Liability and
	Dr Fees / Cr Bank, to another account Dr that account / Cr Liability plus the fees Journal Entry."""
	result = frappe._dict(journal_entry=None, fees_journal_entry=None, payment_entry=None)
	rate = flt(doc.exchange_rate) or 1.0
	remarks = _("Drawdown {0} of Bank Facility {1}").format(doc.name, doc.facility)
	if doc.get("disburse_to") == DISBURSE_TO_OTHER:
		result.journal_entry = _post_other_account(
			doc, EVENT_DRAWDOWN, 1, doc.amount, doc.drawdown_date, rate
		)
		result.fees_journal_entry = post_fees(doc)
		return result
	if voucher_mode(doc.company, doc.currency) == PAYMENT_ENTRY_MODE:
		precision = _precision()
		deductions = [{"account": doc.liability_account, "amount": -flt(flt(doc.amount) * rate, precision)}]
		if flt(doc.upfront_fees) > 0:
			deductions.append(
				{"account": doc.fees_account, "amount": flt(flt(doc.upfront_fees) * rate, precision)}
			)
		result.payment_entry = posting.create_deductions_payment(
			source_doctype=DRAWDOWN,
			source_name=doc.name,
			event=EVENT_DRAWDOWN,
			payment_type="Receive",
			company=doc.company,
			posting_date=doc.drawdown_date,
			party_type="Supplier",
			party=_lender(doc),
			bank_account_head=bank_gl_account(doc.bank_account),
			deductions=deductions,
			reference_no=doc.name,
			reference_date=doc.drawdown_date,
			remarks=remarks,
			details=_details(doc.company, doc.bank_account, "Bank Transfer"),
			clearance_date=doc.drawdown_date,
		)
		return result
	bank_gl = bank_gl_account(doc.bank_account)
	result.journal_entry = posting.create_journal_entry(
		source_doctype=DRAWDOWN,
		source_name=doc.name,
		event=EVENT_DRAWDOWN,
		company=doc.company,
		posting_date=doc.drawdown_date,
		accounts=[
			_row(doc.company, doc.currency, bank_gl, rate, debit=doc.amount),
			_row(doc.company, doc.currency, doc.liability_account, rate, credit=doc.amount),
		],
		cheque_no=doc.name,
		cheque_date=doc.drawdown_date,
		clearance_date=doc.drawdown_date,
		remarks=remarks,
	)
	result.fees_journal_entry = post_fees(doc)
	return result


def post_fees(doc) -> str | None:
	"""The upfront fees paid from the bank on the drawdown date: Dr Fees / Cr Bank."""
	if flt(doc.upfront_fees) <= 0:
		return None
	rate = flt(doc.exchange_rate) or 1.0
	return posting.create_journal_entry(
		source_doctype=DRAWDOWN,
		source_name=doc.name,
		event=EVENT_FEES,
		company=doc.company,
		posting_date=doc.drawdown_date,
		accounts=[
			_expense_row(doc.company, doc.fees_account, doc.upfront_fees, rate, _precision()),
			_row(doc.company, doc.currency, bank_gl_account(doc.bank_account), rate, credit=doc.upfront_fees),
		],
		cheque_no=doc.name,
		cheque_date=doc.drawdown_date,
		clearance_date=doc.drawdown_date,
		remarks=_("Upfront fees of drawdown {0}").format(doc.name),
		balance_exchange_difference=True,
	)


def _post_other_account(doc, event: str, cycle: int, amount: float, date, rate: float) -> str:
	"""The financier paid the supplier / contractor directly: Dr Disbursement Account / Cr Liability."""
	return posting.create_journal_entry(
		source_doctype=DRAWDOWN,
		source_name=doc.name,
		event=event,
		company=doc.company,
		posting_date=date,
		accounts=[
			_row(doc.company, doc.currency, doc.disbursement_account, rate, debit=amount),
			_row(doc.company, doc.currency, doc.liability_account, rate, credit=amount),
		],
		cheque_no=doc.name,
		cheque_date=date,
		remarks=_("Disbursement of drawdown {0} to {1}").format(doc.name, doc.disbursement_account),
		cycle=cycle,
		balance_exchange_difference=True,
	)


def post_stage(drawdown, stage, date, rate) -> tuple[str, str]:
	"""One disbursement stage of an Istisna'a (event `facility_stage_disbursement`, cycle = the stage row's
	idx), the same voucher choice as a drawdown. Returns (voucher doctype, name)."""
	rate = flt(rate) or 1.0
	amount = flt(stage.amount)
	cycle = stage.idx
	if drawdown.get("disburse_to") == DISBURSE_TO_OTHER:
		return "Journal Entry", _post_other_account(drawdown, EVENT_STAGE, cycle, amount, date, rate)
	remarks = _("Stage {0} of drawdown {1}").format(stage.idx, drawdown.name)
	if voucher_mode(drawdown.company, drawdown.currency) == PAYMENT_ENTRY_MODE:
		name = posting.create_deductions_payment(
			source_doctype=DRAWDOWN,
			source_name=drawdown.name,
			event=EVENT_STAGE,
			payment_type="Receive",
			company=drawdown.company,
			posting_date=date,
			party_type="Supplier",
			party=_lender(drawdown),
			bank_account_head=bank_gl_account(drawdown.bank_account),
			deductions=[{"account": drawdown.liability_account, "amount": -flt(amount * rate, _precision())}],
			reference_no=drawdown.name,
			reference_date=date,
			remarks=remarks,
			details=_details(drawdown.company, drawdown.bank_account, "Bank Transfer"),
			cycle=cycle,
			clearance_date=date,
		)
		return "Payment Entry", name
	bank_gl = bank_gl_account(drawdown.bank_account)
	name = posting.create_journal_entry(
		source_doctype=DRAWDOWN,
		source_name=drawdown.name,
		event=EVENT_STAGE,
		company=drawdown.company,
		posting_date=date,
		accounts=[
			_row(drawdown.company, drawdown.currency, bank_gl, rate, debit=amount),
			_row(drawdown.company, drawdown.currency, drawdown.liability_account, rate, credit=amount),
		],
		cheque_no=drawdown.name,
		cheque_date=date,
		clearance_date=date,
		remarks=remarks,
		cycle=cycle,
		balance_exchange_difference=True,
	)
	return "Journal Entry", name


# ---------------------------------------------------------------------------
# Repayment
# ---------------------------------------------------------------------------


def penalty_paid_to_lender(doc) -> bool:
	return flt(doc.penalty) > 0 and (doc.get("penalty_treatment") or PAID_TO_LENDER) == PAID_TO_LENDER


def _interest_expense_account(facility) -> str:
	if not facility.interest_expense_account:
		frappe.throw(
			_("Set the Interest Expense Account of Bank Facility {0}.").format(frappe.bold(facility.name)),
			title=_("Missing Settings"),
		)
	return facility.interest_expense_account


def repayment_deductions(doc, drawdown, facility) -> list[dict]:
	"""The deduction rows (company currency) of a repayment Payment Entry (spec 7g §4.3): principal on the
	liability account, the accrued part of the interest on the accrued interest account, the rest on the
	interest expense account, a penalty paid to the lender on the penalty account."""
	precision = _precision()
	rate = flt(doc.exchange_rate) or 1.0
	rows = []

	def add(account, amount):
		if flt(amount):
			rows.append({"account": account, "amount": flt(flt(amount) * rate, precision)})

	add(drawdown.liability_account, doc.principal_paid)
	add(facility.accrued_interest_account, doc.accrued_interest_cleared)
	if flt(doc.interest_expensed):
		add(_interest_expense_account(facility), doc.interest_expensed)
	if penalty_paid_to_lender(doc):
		add(doc.penalty_account, doc.penalty)
	return rows


def post_repayment(
	doc,
	drawdown,
	facility,
	paid_from: str | None = None,
	source: tuple | None = None,
	details: dict | None = None,
	reference_no: str | None = None,
) -> tuple[str, str]:
	"""The repayment voucher; returns (voucher doctype, name).

	Payment Entry (company currency, `facility_voucher` Payment Entry, or always with `paid_from`): Pay to the
	lender from the bank (or from `paid_from`, the PDC Payable account of an installment cheque: no clearance
	date, the cheque clears it), whole amount in deductions. Journal Entry: Dr Liability + Dr Accrued Interest +
	Dr Interest Expense + Dr Penalty (paid to the lender) / Cr Bank.

	`source`: (doctype, name, event, cycle) of the voucher, default the repayment itself; an installment
	cheque's party posting is sourced to the cheque (`treasury.bank_facilities.facility_cheques`)."""
	source_doctype, source_name, event, cycle = source or (REPAYMENT, doc.name, EVENT_REPAYMENT, 1)
	remarks = _("Repayment {0} of drawdown {1}").format(doc.name, drawdown.name)
	if paid_from or voucher_mode(doc.company, doc.currency) == PAYMENT_ENTRY_MODE:
		method = "Auto Debit" if doc.get("settlement") == SETTLED_BY_AUTO_DEBIT else "Bank Transfer"
		clearance_date = None
		if not paid_from:
			clearance_date = doc.repayment_date
			if doc.get("bank_transaction"):
				clearance_date = frappe.db.get_value("Bank Transaction", doc.bank_transaction, "date")
		name = posting.create_deductions_payment(
			source_doctype=source_doctype,
			source_name=source_name,
			event=event,
			payment_type="Pay",
			company=doc.company,
			posting_date=doc.repayment_date,
			party_type="Supplier",
			party=lenders.get_lender_supplier(facility),
			bank_account_head=paid_from or bank_gl_account(doc.bank_account),
			deductions=repayment_deductions(doc, drawdown, facility),
			reference_no=reference_no or doc.name,
			reference_date=doc.repayment_date,
			remarks=remarks,
			details=details or _details(doc.company, doc.bank_account, method),
			cycle=cycle,
			clearance_date=clearance_date,
		)
		return "Payment Entry", name

	precision = _precision()
	rate = flt(doc.exchange_rate) or 1.0
	company, currency = doc.company, doc.currency
	accounts = []
	if flt(doc.principal_paid):
		accounts.append(_row(company, currency, drawdown.liability_account, rate, debit=doc.principal_paid))
	if flt(doc.accrued_interest_cleared):
		accounts.append(
			_row(
				company, currency, facility.accrued_interest_account, rate, debit=doc.accrued_interest_cleared
			)
		)
	if flt(doc.interest_expensed):
		accounts.append(
			_expense_row(company, _interest_expense_account(facility), doc.interest_expensed, rate, precision)
		)
	if penalty_paid_to_lender(doc):
		accounts.append(_expense_row(company, doc.penalty_account, doc.penalty, rate, precision))
	accounts.append(_row(company, currency, bank_gl_account(doc.bank_account), rate, credit=doc.total_paid))
	clearance_date = doc.repayment_date
	if doc.get("bank_transaction"):
		clearance_date = frappe.db.get_value("Bank Transaction", doc.bank_transaction, "date")
	name = posting.create_journal_entry(
		source_doctype=source_doctype,
		source_name=source_name,
		event=event,
		company=company,
		posting_date=doc.repayment_date,
		accounts=accounts,
		cheque_no=reference_no or doc.name,
		cheque_date=doc.repayment_date,
		clearance_date=clearance_date,
		remarks=remarks,
		cycle=cycle,
		balance_exchange_difference=True,
	)
	return "Journal Entry", name


def post_charity(doc, facility) -> str | None:
	"""A late payment penalty payable to charity (spec 7g §4.3 R6): Dr Late Payment (Charity) / Cr Charity
	Payable, company currency, no bank row (the penalty is not paid through the bank)."""
	if flt(doc.penalty) <= 0 or doc.get("penalty_treatment") != PAYABLE_TO_CHARITY:
		return None
	if not facility.charity_payable_account:
		frappe.throw(_("Set the Late Payment (Charity) Account and the Charity Payable Account."))
	amount = flt(flt(doc.penalty) * (flt(doc.exchange_rate) or 1.0), _precision())
	cost_center = erpnext.get_default_cost_center(doc.company)
	return posting.create_journal_entry(
		source_doctype=REPAYMENT,
		source_name=doc.name,
		event=EVENT_CHARITY,
		company=doc.company,
		posting_date=doc.repayment_date,
		accounts=[
			{"account": doc.penalty_account, "debit_in_account_currency": amount, "cost_center": cost_center},
			{
				"account": facility.charity_payable_account,
				"credit_in_account_currency": amount,
				"cost_center": cost_center,
			},
		],
		cheque_no=doc.name,
		cheque_date=doc.repayment_date,
		remarks=_("Late payment of repayment {0}, payable to charity").format(doc.name),
	)


# ---------------------------------------------------------------------------
# Accrual, cancel
# ---------------------------------------------------------------------------


def post_accrual(drawdown, facility, month_end, amount: float, rate: float) -> str:
	"""Dr Interest Expense / Cr Accrued Interest for the interest of one month, dated the month end."""
	for fieldname in ("interest_expense_account", "accrued_interest_account"):
		if not facility.get(fieldname):
			frappe.throw(
				_("Set the {0} of Bank Facility {1}.").format(
					_(facility.meta.get_label(fieldname)), frappe.bold(facility.name)
				),
				title=_("Missing Settings"),
			)
	if facility.get("is_islamic"):
		remarks = _("Profit accrual of drawdown {0} for {1}")
	else:
		remarks = _("Interest accrual of drawdown {0} for {1}")
	return posting.create_journal_entry(
		source_doctype=DRAWDOWN,
		source_name=drawdown.name,
		event=accrual_event(month_end),
		company=drawdown.company,
		posting_date=month_end,
		accounts=[
			_expense_row(drawdown.company, facility.interest_expense_account, amount, rate, _precision()),
			_row(drawdown.company, drawdown.currency, facility.accrued_interest_account, rate, credit=amount),
		],
		remarks=remarks.format(drawdown.name, month_end.strftime("%Y-%m")),
		balance_exchange_difference=True,
	)


def cancel_vouchers(
	source_doctype: str, source_name: str, ignore_linked_doctypes: tuple[str, ...] = ()
) -> list[str]:
	"""Cancel every submitted voucher of a source, newest first (`ignore_linked_doctypes`: see
	posting.cancel_voucher)."""
	cancelled = []
	for voucher in reversed(posting.get_linked_vouchers(source_doctype, source_name)):
		if voucher.docstatus < 2:
			posting.cancel_voucher(voucher.doctype, voucher.name, ignore_linked_doctypes)
			cancelled.append(voucher.name)
	return cancelled
