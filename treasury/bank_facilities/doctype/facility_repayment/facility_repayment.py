# Copyright (c) 2026, Ahmed Abdelmageed and contributors
# For license information, please see license.txt

import erpnext
import frappe
from erpnext.accounts.doctype.account.account import get_account_currency
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, flt, formatdate, getdate

from treasury.accounting import posting
from treasury.balance_alerts import guard
from treasury.bank_facilities import accounting, lenders, repayment
from treasury.bank_facilities.interest import accrued_balance
from treasury.bank_facilities.utilisation import refresh_utilisation
from treasury.utils import voucher_summary

DRAWDOWN = "Facility Drawdown"
# Computed by the controller / posting service; never taken from the client.
SYSTEM_FIELDS = (
	"journal_entry",
	"payment_entry",
	"charity_journal_entry",
	"penalty_treatment",
	"schedule_snapshot",
	"prepayment_amount",
	"accrued_interest_cleared",
	"interest_expensed",
	"submission_seq",
)
# Set only by the facility services (auto debit, installment cheques: flag `treasury_facility_service`); a
# repayment entered by a user is always settled by the bank.
SERVICE_FIELDS = ("settlement", "pdc_cheque", "cheque_cycle", "bank_transaction")


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
		bank_transaction: DF.Link | None
		charity_journal_entry: DF.Link | None
		cheque_cycle: DF.Int
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
		payment_entry: DF.Link | None
		pdc_cheque: DF.Link | None
		penalty: DF.Currency
		penalty_account: DF.Link | None
		penalty_treatment: DF.Literal["", "Paid to Lender", "Payable to Charity"]
		prepayment: DF.Check
		prepayment_amount: DF.Currency
		principal_paid: DF.Currency
		remarks: DF.SmallText | None
		repayment_date: DF.Date
		schedule_snapshot: DF.LongText | None
		settlement: DF.Literal["Bank", "Auto Debit", "PDC Cheque"]
		submission_seq: DF.Int
		total_paid: DF.Currency
		vouchers_html: DF.HTML | None
	# end: auto-generated types

	# ------------------------------------------------------------------ hooks

	def onload(self):
		if not self.is_new():
			self.set_onload("voucher_summary", voucher_summary.render(self.doctype, [self.name]))
			# design 7e P17: "Submitted with a low-balance override by ..." on the form
			self.set_onload("low_balance_override", guard.latest_override(self.doctype, self.name))
		if self.facility:
			is_islamic = cint(frappe.db.get_value("Bank Facility", self.facility, "is_islamic"))
			self.set_onload("terms", lenders.terms(is_islamic))
		# design 7g §4.2: the "Settled By" indicator and headline, the draft intro of a cheque drawdown
		self.set_onload(
			"settlement_info",
			{
				"cheque_no": self.pdc_cheque
				and frappe.db.get_value("PDC Cheque", self.pdc_cheque, "cheque_no"),
				"transaction_date": self.bank_transaction
				and frappe.db.get_value("Bank Transaction", self.bank_transaction, "date"),
				"repayment_mode": self.drawdown
				and frappe.db.get_value(DRAWDOWN, self.drawdown, "repayment_mode"),
			},
		)

	def validate(self):
		# on submit too: the submitted document comes from the client (it is recomputed in before_submit)
		if self.docstatus == 0 or getattr(self, "_action", None) == "submit":
			for fieldname in SYSTEM_FIELDS:
				self.set(fieldname, None)
			self.submission_seq = 0
			if not self.flags.treasury_facility_service:
				self.settlement = accounting.SETTLED_BY_BANK
				self.pdc_cheque = self.bank_transaction = None
				self.cheque_cycle = 0
		self.settlement = self.settlement or accounting.SETTLED_BY_BANK
		guard.reset_override_flag(self)
		drawdown = self.get_drawdown()
		facility = frappe.get_doc("Bank Facility", drawdown.facility)
		self.set_drawdown_details(drawdown)
		self.set_penalty_treatment(facility)
		self.validate_cheque_settlement()
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
		# low balance guard (pre-state; the drawdown row is locked first); finished in on_submit. A debit the
		# bank already made (scheduler, statement) or a cheque's party posting is never blocked.
		system = bool(self.flags.treasury_system_repayment) or self.is_cheque_settlement()
		self.flags.balance_snapshot = guard.begin(self.company, [self.bank_account], system=system)
		drawdown = self.get_drawdown()
		self.validate_date(drawdown)
		self.allocate(drawdown)
		self.schedule_snapshot = repayment.snapshot(drawdown)
		self.submission_seq = repayment.next_submission_seq(self.drawdown)

	def on_submit(self):
		drawdown = self.get_drawdown()
		facility = frappe.get_doc("Bank Facility", drawdown.facility)
		values = {}
		voucher = (None, None)
		# a penalty payable to charity alone moves nothing through the bank: only its charity Journal Entry
		if flt(self.total_paid) > 0:
			voucher = self.post_voucher(drawdown, facility)
			values["payment_entry" if voucher[0] == "Payment Entry" else "journal_entry"] = voucher[1]
		charity = accounting.post_charity(self, facility)
		if charity:
			values["charity_journal_entry"] = charity
		if values:
			self.db_set(values)
		repayment.apply_repayment(drawdown, self, voucher)
		refresh_utilisation(drawdown.facility)
		text = guard.finish(
			self.flags.pop("balance_snapshot", None),
			doctype=self.doctype,
			name=self.name,
			allow_override=self.allow_low_balance,
		)
		if text:
			self.add_comment("Info", text)

	def post_voucher(self, drawdown, facility) -> tuple[str, str]:
		"""The repayment's own voucher, or for an installment cheque the cheque's party posting: one Payment
		Entry from the PDC Payable account, sourced to the cheque (spec 7g §3.4, no double posting)."""
		if not self.is_cheque_settlement():
			return accounting.post_repayment(self, drawdown, facility)
		from treasury.pdc import lifecycle

		cheque = frappe.get_doc("PDC Cheque", self.pdc_cheque)
		mapping = posting.get_account_mapping(cheque.company, cheque.bank_account)
		return accounting.post_repayment(
			self,
			drawdown,
			facility,
			paid_from=mapping.pdc_payable_account,
			source=("PDC Cheque", cheque.name, lifecycle.EVENT_PARTY, cint(self.cheque_cycle) or 1),
			details=lifecycle.cheque_details(cheque),
			reference_no=cheque.cheque_no,
		)

	def before_cancel(self):
		if self.is_cheque_settlement() and not self.flags.treasury_facility_service:
			frappe.throw(
				_(
					"Repayment {0} is settled by cheque {1}: bounce, replace or cancel the cheque instead."
				).format(frappe.bold(self.name), frappe.bold(self.pdc_cheque)),
				frappe.ValidationError,
				title=_("Not Allowed"),
			)
		# the last submitted repayment of a drawdown (its schedule snapshot is the current one), or a cheque's
		repayment.lock_drawdown(self.drawdown)
		repayment.check_can_cancel(frappe.get_doc(DRAWDOWN, self.drawdown), self)

	def on_cancel(self):
		drawdown = self.get_drawdown()
		# restore the schedule first: its rows link to this repayment and to its voucher
		repayment.undo_repayment(drawdown, self)
		# its voucher and charity Journal Entry (a cheque repayment has none: the cheque's are undone by the cheque)
		accounting.cancel_vouchers(self.doctype, self.name)
		refresh_utilisation(drawdown.facility)

	# ------------------------------------------------------------------ validation

	def is_cheque_settlement(self) -> bool:
		return self.settlement == accounting.SETTLED_BY_CHEQUE

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

	def set_penalty_treatment(self, facility):
		"""An Islamic facility's late payment penalty goes to the lender or to charity (its setting); the charity
		account is the default penalty account there."""
		if cint(facility.is_islamic):
			self.penalty_treatment = facility.late_payment_mode or accounting.PAID_TO_LENDER
			if flt(self.penalty) and not self.penalty_account and facility.late_payment_charity_account:
				self.penalty_account = facility.late_payment_charity_account
		else:
			self.penalty_treatment = accounting.PAID_TO_LENDER

	def validate_cheque_settlement(self):
		"""I2: an installment cheque pays exactly one installment, no prepayment, no penalty."""
		if not self.is_cheque_settlement():
			return
		if not self.pdc_cheque:
			frappe.throw(_("Installment Cheque is required for a repayment settled by cheque."))
		self.allocation_mode = repayment.SELECTED_ROW
		self.prepayment = 0
		self.penalty = 0
		self.penalty_account = None

	def validate_date(self, drawdown):
		date = getdate(self.repayment_date)
		if date < getdate(drawdown.drawdown_date):
			frappe.throw(
				_("The Repayment Date cannot be before the Drawdown Date {0}.").format(
					formatdate(drawdown.drawdown_date)
				)
			)
		if self.is_cheque_settlement():
			# dated by its cheque (the cheque date or a represented cheque's), undone by delta (§3.5)
			return
		# the schedule is changed in date order, so a repayment cannot be back-dated before a submitted one
		latest = frappe.get_all(
			self.doctype,
			filters={
				"drawdown": self.drawdown,
				"docstatus": 1,
				"name": ["!=", self.name or ""],
				"settlement": ["!=", accounting.SETTLED_BY_CHEQUE],
			},
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
		"""Nothing entered: pay the unpaid principal and interest of the next installment (or the selected one);
		an installment with a live cheque is left to its cheque."""
		if self.docstatus != 0 or flt(self.principal_paid) or flt(self.interest_paid):
			return
		live = (
			{} if self.allocation_mode == repayment.SELECTED_ROW else repayment._live_cheques(drawdown.name)
		)
		rows = sorted(
			(
				r
				for r in drawdown.repayment_schedule
				if not r.is_prepayment
				and r.name not in live
				and (repayment.unpaid_principal(r) or repayment.unpaid_interest(r))
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
		# a penalty payable to charity is not paid through the bank (spec 7g §4.3 R6)
		penalty = self.penalty if accounting.penalty_paid_to_lender(self) else 0
		self.total_paid = flt(
			self.principal_paid + self.interest_paid + penalty, self.precision("total_paid")
		)
		if self.total_paid <= 0 and not self.penalty:
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
