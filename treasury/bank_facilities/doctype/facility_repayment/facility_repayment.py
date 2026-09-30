# Copyright (c) 2026, Ahmed Abdelmageed and contributors
# For license information, please see license.txt

import erpnext
import frappe
from erpnext.accounts.doctype.account.account import get_account_currency
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, formatdate, getdate

from treasury.accounting import posting
from treasury.balance_alerts import guard
from treasury.bank_facilities import accounting, repayment
from treasury.bank_facilities.interest import accrued_balance
from treasury.bank_facilities.utilisation import refresh_utilisation
from treasury.utils import voucher_summary

DRAWDOWN = "Facility Drawdown"
# Computed by the controller / posting service; never taken from the client.
SYSTEM_FIELDS = (
	"journal_entry",
	"schedule_snapshot",
	"prepayment_amount",
	"accrued_interest_cleared",
	"interest_expensed",
	"submission_seq",
)


class FacilityRepayment(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from treasury.bank_facilities.doctype.facility_repayment_allocation.facility_repayment_allocation import (
			FacilityRepaymentAllocation,
		)

		accrued_interest_cleared: DF.Currency
		allocation_mode: DF.Literal["Oldest First", "Selected Row"]
		allocations: DF.Table[FacilityRepaymentAllocation]
		allow_low_balance: DF.Check
		amended_from: DF.Link | None
		bank: DF.Link | None
		bank_account: DF.Link
		company: DF.Link | None
		currency: DF.Link | None
		drawdown: DF.Link
		exchange_rate: DF.Float
		facility: DF.Link | None
		installment_no: DF.Int
		interest_expensed: DF.Currency
		interest_paid: DF.Currency
		journal_entry: DF.Link | None
		naming_series: DF.Literal["FRP-.YYYY.-.#####"]
		penalty: DF.Currency
		penalty_account: DF.Link | None
		prepayment: DF.Check
		prepayment_amount: DF.Currency
		principal_paid: DF.Currency
		remarks: DF.SmallText | None
		repayment_date: DF.Date
		schedule_snapshot: DF.LongText | None
		submission_seq: DF.Int
		total_paid: DF.Currency
		vouchers_html: DF.HTML | None
	# end: auto-generated types

	# ------------------------------------------------------------------ hooks

	def onload(self):
		if not self.is_new():
			self.set_onload("voucher_summary", voucher_summary.render(self.doctype, [self.name]))

	def validate(self):
		# on submit too: the submitted document comes from the client (it is recomputed in before_submit)
		if self.docstatus == 0 or getattr(self, "_action", None) == "submit":
			for fieldname in SYSTEM_FIELDS:
				self.set(fieldname, None)
			self.submission_seq = 0
		drawdown = self.get_drawdown()
		self.set_drawdown_details(drawdown)
		self.validate_date(drawdown)
		self.validate_bank_account()
		self.set_exchange_rate()
		self.set_default_amounts(drawdown)
		self.validate_amounts()
		self.validate_penalty_account()
		self.allocate(drawdown)

	def before_submit(self):
		guard.check_override_flag(self.allow_low_balance)
		# serialise the repayments of one drawdown: allocate on the schedule as it is now, under the lock
		repayment.lock_drawdown(self.drawdown)
		# low balance guard (pre-state; the drawdown row is locked first); finished in on_submit
		self.flags.balance_snapshot = guard.begin(self.company, [self.bank_account])
		drawdown = self.get_drawdown()
		self.validate_date(drawdown)
		self.allocate(drawdown)
		self.schedule_snapshot = repayment.snapshot(drawdown)
		self.submission_seq = repayment.next_submission_seq(self.drawdown)

	def on_submit(self):
		drawdown = self.get_drawdown()
		facility = frappe.get_doc("Bank Facility", drawdown.facility)
		journal = accounting.post_repayment(self, drawdown, facility)
		self.db_set("journal_entry", journal)
		repayment.apply_repayment(drawdown, self, journal)
		refresh_utilisation(drawdown.facility)
		text = guard.finish(
			self.flags.pop("balance_snapshot", None),
			doctype=self.doctype,
			name=self.name,
			allow_override=self.allow_low_balance,
		)
		if text:
			self.add_comment("Info", text)

	def before_cancel(self):
		# only the last submitted repayment of a drawdown may be cancelled (its schedule snapshot is the current one)
		repayment.lock_drawdown(self.drawdown)
		repayment.check_no_later_repayment(self.drawdown, self)

	def on_cancel(self):
		drawdown = self.get_drawdown()
		# restore the schedule first: its rows link to this repayment and to its Journal Entry
		repayment.undo_repayment(drawdown, self)
		accounting.cancel_vouchers(self.doctype, self.name)
		refresh_utilisation(drawdown.facility)

	# ------------------------------------------------------------------ validation

	def get_drawdown(self):
		drawdown = frappe.get_doc(DRAWDOWN, self.drawdown)
		if drawdown.docstatus != 1:
			frappe.throw(_("Facility Drawdown {0} is not submitted.").format(frappe.bold(self.drawdown)))
		return drawdown

	def set_drawdown_details(self, drawdown):
		self.facility, self.company = drawdown.facility, drawdown.company
		self.bank, self.currency = drawdown.bank, drawdown.currency
		if not self.bank_account:
			self.bank_account = drawdown.bank_account

	def validate_date(self, drawdown):
		date = getdate(self.repayment_date)
		if date < getdate(drawdown.drawdown_date):
			frappe.throw(
				_("The Repayment Date cannot be before the Drawdown Date {0}.").format(
					formatdate(drawdown.drawdown_date)
				)
			)
		# the schedule is changed in date order, so a repayment cannot be back-dated before a submitted one
		latest = frappe.get_all(
			self.doctype,
			filters={"drawdown": self.drawdown, "docstatus": 1, "name": ["!=", self.name or ""]},
			order_by="repayment_date desc",
			limit=1,
			pluck="repayment_date",
		)
		if latest and date < getdate(latest[0]):
			frappe.throw(
				_("The Repayment Date cannot be before the latest submitted repayment ({0}).").format(
					formatdate(latest[0])
				)
			)

	def validate_bank_account(self):
		bank_account = frappe.db.get_value(
			"Bank Account", self.bank_account, ["company", "is_company_account", "account"], as_dict=True
		)
		if not bank_account or not bank_account.is_company_account or bank_account.company != self.company:
			frappe.throw(
				_("Bank Account {0} must be a company bank account of {1}.").format(
					frappe.bold(self.bank_account), frappe.bold(self.company)
				)
			)
		if not bank_account.account:
			frappe.throw(_("Bank Account {0} has no GL account.").format(frappe.bold(self.bank_account)))
		currency = get_account_currency(bank_account.account)
		if currency != self.currency:
			frappe.throw(
				_("Bank Account {0} is in {1}, but the facility is in {2}.").format(
					frappe.bold(self.bank_account), currency, self.currency
				)
			)

	def set_exchange_rate(self):
		if self.currency == erpnext.get_company_currency(self.company):
			self.exchange_rate = 1
		elif not flt(self.exchange_rate) or flt(self.exchange_rate) == 1:
			self.exchange_rate = posting.get_cheque_exchange_rate(
				self.currency, self.company, self.repayment_date
			)

	def set_default_amounts(self, drawdown):
		"""Nothing entered: pay the unpaid principal and interest of the next installment (or the selected one)."""
		if self.docstatus != 0 or flt(self.principal_paid) or flt(self.interest_paid):
			return
		rows = sorted(
			(
				r
				for r in drawdown.repayment_schedule
				if not r.is_prepayment and (repayment.unpaid_principal(r) or repayment.unpaid_interest(r))
			),
			key=lambda r: getdate(r.due_date),
		)
		if self.allocation_mode == repayment.SELECTED_ROW:
			rows = [r for r in rows if r.installment_no == self.installment_no]
		if rows:
			self.principal_paid = repayment.unpaid_principal(rows[0])
			self.interest_paid = repayment.unpaid_interest(rows[0])

	def validate_amounts(self):
		for fieldname in ("principal_paid", "interest_paid", "penalty"):
			self.set(fieldname, flt(self.get(fieldname), self.precision(fieldname)))
			if self.get(fieldname) < 0:
				frappe.throw(_("{0} cannot be negative.").format(_(self.meta.get_label(fieldname))))
		self.total_paid = flt(
			self.principal_paid + self.interest_paid + self.penalty, self.precision("total_paid")
		)
		if self.total_paid <= 0:
			frappe.throw(_("Enter the principal, interest or penalty paid."))

	def validate_penalty_account(self):
		if not self.penalty:
			self.penalty_account = None
			return
		if not self.penalty_account:
			frappe.throw(_("Penalty Account is required for the penalty."))
		details = frappe.db.get_value(
			"Account",
			self.penalty_account,
			["company", "is_group", "root_type", "account_currency"],
			as_dict=True,
		)
		company_currency = erpnext.get_company_currency(self.company)
		if (
			not details
			or details.company != self.company
			or details.is_group
			or details.root_type != "Expense"
			or details.account_currency != company_currency
		):
			frappe.throw(
				_("Penalty Account {0} must be a ledger Expense account of {1} in {2}.").format(
					frappe.bold(self.penalty_account), frappe.bold(self.company), company_currency
				)
			)

	def allocate(self, drawdown):
		"""Allocation to the schedule rows and the split of the interest into accrued / expensed."""
		precision = self.precision("total_paid")
		result = repayment.allocate(drawdown, self)
		self.set("allocations", [])
		for entry in sorted(result.rows, key=lambda e: getdate(e.row.due_date)):
			self.append(
				"allocations",
				{
					"installment_no": entry.row.installment_no,
					"due_date": entry.row.due_date,
					"principal": entry.principal,
					"interest": entry.interest,
					"schedule_row": entry.row.name,
				},
			)
		self.prepayment_amount = result.prepayment
		accrued = accrued_balance(drawdown, self.repayment_date)
		self.accrued_interest_cleared = flt(min(self.interest_paid, accrued), precision)
		self.interest_expensed = flt(self.interest_paid - self.accrued_interest_cleared, precision)
