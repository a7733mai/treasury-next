"""Accounting of the Bank Facilities module: the Journal Entries of drawdowns, repayments and interest accruals.

Every voucher is created by `treasury.accounting.posting.create_journal_entry` (idempotent on source, event and
cycle) and cancelled by `posting.cancel_voucher`; nothing here writes GL rows itself.

Currencies: the facility currency is the currency of the bank account, the liability accounts and the accrued
interest account; one voucher converts all of them at one exchange rate. Expense accounts (interest, fees,
penalty) are in the company currency and get the converted amount; the cents a per-row rounding may leave are
booked on the company's Exchange Gain / Loss account (`balance_exchange_difference`). Differences between the
drawdown rate and the repayment rate stay on the liability account, for ERPNext's Exchange Rate Revaluation.

Bank clearance: the bank itself credits the drawdown and debits the fees and repayments on the document's
date, so these Journal Entries carry `clearance_date` = their posting date (like the cleared cheques of the
other modules) and do not wait in the Bank Reconciliation Tool. Accruals have no bank row.
"""

import erpnext
import frappe
from frappe import _
from frappe.utils import flt

from treasury.accounting import posting

DRAWDOWN = "Facility Drawdown"
REPAYMENT = "Facility Repayment"
EVENT_DRAWDOWN = "facility_drawdown"
EVENT_FEES = "facility_upfront_fees"
EVENT_REPAYMENT = "facility_repayment"
# `interest_accrual:YYYY-MM`: one accrual per drawdown and month (idempotency key).
EVENT_ACCRUAL_PREFIX = "interest_accrual:"


def accrual_event(month_end) -> str:
	return f"{EVENT_ACCRUAL_PREFIX}{month_end.strftime('%Y-%m')}"


def bank_gl_account(bank_account: str) -> str:
	account = frappe.db.get_value("Bank Account", bank_account, "account")
	if not account:
		frappe.throw(_("Bank Account {0} has no GL account.").format(frappe.bold(bank_account)))
	return account


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


def post_drawdown(doc) -> tuple[str, str | None]:
	"""Dr Bank / Cr Liability for the principal, and Dr Fees / Cr Bank for the upfront fees (if any)."""
	bank_gl = bank_gl_account(doc.bank_account)
	rate = flt(doc.exchange_rate) or 1.0
	remarks = _("Drawdown {0} of Bank Facility {1}").format(doc.name, doc.facility)
	journal = posting.create_journal_entry(
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
	fees_journal = None
	if flt(doc.upfront_fees) > 0:
		fees_journal = posting.create_journal_entry(
			source_doctype=DRAWDOWN,
			source_name=doc.name,
			event=EVENT_FEES,
			company=doc.company,
			posting_date=doc.drawdown_date,
			accounts=[
				_expense_row(doc.company, doc.fees_account, doc.upfront_fees, rate, _precision()),
				_row(doc.company, doc.currency, bank_gl, rate, credit=doc.upfront_fees),
			],
			cheque_no=doc.name,
			cheque_date=doc.drawdown_date,
			clearance_date=doc.drawdown_date,
			remarks=_("Upfront fees of drawdown {0}").format(doc.name),
			balance_exchange_difference=True,
		)
	return journal, fees_journal


def post_repayment(doc, drawdown, facility) -> str:
	"""Dr Liability (principal) + Dr Accrued Interest (accrued part) + Dr Interest Expense (the rest of the
	interest) + Dr Penalty / Cr Bank (total paid)."""
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
		if not facility.interest_expense_account:
			frappe.throw(
				_("Set the Interest Expense Account of Bank Facility {0}.").format(
					frappe.bold(facility.name)
				),
				title=_("Missing Settings"),
			)
		accounts.append(
			_expense_row(company, facility.interest_expense_account, doc.interest_expensed, rate, precision)
		)
	if flt(doc.penalty):
		accounts.append(_expense_row(company, doc.penalty_account, doc.penalty, rate, precision))
	accounts.append(_row(company, currency, bank_gl_account(doc.bank_account), rate, credit=doc.total_paid))
	return posting.create_journal_entry(
		source_doctype=REPAYMENT,
		source_name=doc.name,
		event=EVENT_REPAYMENT,
		company=company,
		posting_date=doc.repayment_date,
		accounts=accounts,
		cheque_no=doc.name,
		cheque_date=doc.repayment_date,
		clearance_date=doc.repayment_date,
		remarks=_("Repayment {0} of drawdown {1}").format(doc.name, doc.drawdown),
		balance_exchange_difference=True,
	)


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
		remarks=_("Interest accrual of drawdown {0} for {1}").format(
			drawdown.name, month_end.strftime("%Y-%m")
		),
		balance_exchange_difference=True,
	)


def cancel_vouchers(source_doctype: str, source_name: str) -> list[str]:
	"""Cancel every submitted voucher of a source, newest first."""
	cancelled = []
	for voucher in reversed(posting.get_linked_vouchers(source_doctype, source_name)):
		if voucher.docstatus < 2:
			posting.cancel_voucher(voucher.doctype, voucher.name)
			cancelled.append(voucher.name)
	return cancelled
