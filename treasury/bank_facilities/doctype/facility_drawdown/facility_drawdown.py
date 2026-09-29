# Copyright (c) 2026, Ahmed Abdelmageed and contributors
# For license information, please see license.txt

import erpnext
import frappe
from erpnext.accounts.doctype.account.account import get_account_currency
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, fmt_money, formatdate, getdate

from treasury.accounting import posting
from treasury.bank_facilities import accounting, schedule
from treasury.bank_facilities.facility import ACTIVE
from treasury.bank_facilities.repayment import refresh_repayment_status
from treasury.bank_facilities.utilisation import (
	LOAN_TYPES,
	get_drawdown_outstanding,
	get_utilisation,
	refresh_utilisation,
)

FACILITY = "Bank Facility"
# Filled by the posting / accrual services; never taken from the client.
SYSTEM_FIELDS = ("journal_entry", "fees_journal_entry", "last_accrual_date", "schedule_version")
# Written by the repayments only.
PAYMENT_FIELDS = ("paid_principal", "paid_interest", "payment_journal", "repayment")
# The terms the repayment schedule is built from: changing one requires a new schedule.
TERM_FIELDS = (
	"amount",
	"drawdown_date",
	"interest_rate",
	"day_count",
	"profit_amount",
	"tenor_months",
	"repayment_frequency",
	"repayment_method",
	"first_repayment_date",
	"grace_months",
)
# Facility values that change whenever its utilisation or status may have changed (concurrency check).
FACILITY_STAMP_FIELDS = ["modified", "status", "utilisation_refreshed_on"]


class FacilityDrawdown(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from treasury.bank_facilities.doctype.facility_repayment_schedule.facility_repayment_schedule import (
			FacilityRepaymentSchedule,
		)

		amended_from: DF.Link | None
		amount: DF.Currency
		bank: DF.Link | None
		bank_account: DF.Link
		company: DF.Link | None
		currency: DF.Link | None
		day_count: DF.Literal["Actual/365", "Actual/360"]
		drawdown_date: DF.Date
		exchange_rate: DF.Float
		facility: DF.Link
		fees_account: DF.Link | None
		fees_journal_entry: DF.Link | None
		first_repayment_date: DF.Date | None
		grace_months: DF.Int
		interest_rate: DF.Percent
		journal_entry: DF.Link | None
		last_accrual_date: DF.Date | None
		liability_account: DF.Link | None
		limit_type: DF.Literal["Short Term Loan", "Long Term Loan", "Murabaha", "Tawarruq", "Other"]
		naming_series: DF.Literal["FDD-.YYYY.-.#####"]
		profit_amount: DF.Currency
		rate_type: DF.Literal["Fixed", "Variable (reference + margin)"]
		remarks: DF.SmallText | None
		repayment_frequency: DF.Literal["Monthly", "Quarterly", "Semi-Annually", "Annually", "Bullet"]
		repayment_method: DF.Literal["Equal Principal", "Equal Installment (annuity)", "Bullet"]
		repayment_schedule: DF.Table[FacilityRepaymentSchedule]
		repayment_status: DF.Literal["Open", "Repaid"]
		schedule_version: DF.Int
		tenor_months: DF.Int
		total_interest: DF.Currency
		total_payable: DF.Currency
		total_principal: DF.Currency
		upfront_fees: DF.Currency
	# end: auto-generated types

	# ------------------------------------------------------------------ virtual / helpers

	@property
	def facility_doc(self):
		if not getattr(self, "_facility_doc", None) or self._facility_doc.name != self.facility:
			self._facility_doc = frappe.get_doc(FACILITY, self.facility)
		return self._facility_doc

	def get_limit(self):
		return next((row for row in self.facility_doc.limits if row.limit_type == self.limit_type), None)

	# ------------------------------------------------------------------ hooks

	def onload(self):
		if self.docstatus == 1:
			outstanding = get_drawdown_outstanding(
				[{"name": self.name, "liability_account": self.liability_account}]
			)
			self.set_onload(
				"outstanding_principal", flt(outstanding.get(self.name), self.precision("amount"))
			)

	def validate(self):
		if self.resets_system_fields():
			for fieldname in SYSTEM_FIELDS:
				self.set(fieldname, None)
			self.schedule_version = 0
			self.repayment_status = "Open"
		self.set_facility_details()
		self.set_terms_defaults()
		self.validate_bank_account()
		self.set_exchange_rate()
		self.validate_amounts()
		self.validate_fees_account()
		if not self.repayment_schedule:
			self.make_schedule()
		self.validate_schedule_matches_terms()
		self.validate_schedule()

	def resets_system_fields(self) -> bool:
		"""Draft saves and the submit itself: the submitted document comes from the client too."""
		return self.is_new() or self.docstatus == 0 or getattr(self, "_action", None) == "submit"

	def before_submit(self):
		# serialise drawdowns of one facility: the limit check below must see every earlier drawdown
		self.lock_facility()
		self.validate_facility_open()
		self.validate_available_limit()

	def lock_facility(self):
		"""Lock the facility row and make sure the facility / ledger data read so far is current.

		Under REPEATABLE READ, what was read before the lock comes from the transaction's snapshot. Every
		drawdown, repayment and facility action refreshes the facility's utilisation stamp (or its status /
		modified), so comparing the snapshot values with the locking (current) read detects a drawdown committed
		meanwhile; the user is asked to try again rather than checking the limit on stale ledger rows. (Overdraft
		utilisation from other bank postings is not covered: it moves with the bank balance at any time.)"""
		seen = frappe.db.get_value(FACILITY, self.facility, FACILITY_STAMP_FIELDS, as_dict=True)
		current = frappe.db.get_value(
			FACILITY, self.facility, FACILITY_STAMP_FIELDS, as_dict=True, for_update=True
		)
		if seen != current:
			frappe.throw(
				_("Bank Facility {0} was changed by another transaction. Please try again.").format(
					frappe.bold(self.facility)
				),
				title=_("Concurrent Update"),
			)

	def on_submit(self):
		journal, fees_journal = accounting.post_drawdown(self)
		self.db_set({"journal_entry": journal, "fees_journal_entry": fees_journal})
		refresh_utilisation(self.facility)

	def before_cancel(self):
		repayments = frappe.get_all(
			"Facility Repayment", filters={"drawdown": self.name, "docstatus": 1}, pluck="name"
		)
		if repayments:
			frappe.throw(
				_("Drawdown {0} has submitted repayments ({1}). Cancel them first.").format(
					frappe.bold(self.name), ", ".join(frappe.bold(r) for r in repayments)
				),
				title=_("Not Allowed"),
			)

	def on_cancel(self):
		# the drawdown, fees and accrual Journal Entries (before Frappe's check for submitted back links)
		accounting.cancel_vouchers(self.doctype, self.name)
		refresh_utilisation(self.facility)

	# ------------------------------------------------------------------ validation

	def set_facility_details(self):
		facility = self.facility_doc
		self.company, self.bank, self.currency = facility.company, facility.bank, facility.currency
		if self.limit_type not in LOAN_TYPES:
			frappe.throw(
				_("Limit Type must be one of: {0}.").format(
					", ".join(_(t, context=FACILITY) for t in LOAN_TYPES)
				)
			)
		limit = self.get_limit()
		if not limit:
			frappe.throw(
				_("Bank Facility {0} has no {1} sub-limit.").format(
					frappe.bold(self.facility), _(self.limit_type, context=FACILITY)
				)
			)
		if not limit.liability_account:
			frappe.throw(
				_("Set the Liability Account of the {0} sub-limit of Bank Facility {1}.").format(
					_(self.limit_type, context=FACILITY), frappe.bold(self.facility)
				)
			)
		self.liability_account = limit.liability_account
		if not self.bank_account:
			self.bank_account = facility.default_bank_account

	def set_terms_defaults(self):
		limit = self.get_limit()
		self.rate_type = self.rate_type or limit.rate_type or "Fixed"
		self.day_count = self.day_count or limit.day_count or "Actual/365"
		if not flt(self.interest_rate) and not flt(self.profit_amount):
			self.interest_rate = flt(limit.interest_rate)

	def validate_bank_account(self):
		if not self.bank_account:
			frappe.throw(_("Bank Account is required."))
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
				self.currency, self.company, self.drawdown_date
			)

	def validate_amounts(self):
		self.amount = flt(self.amount, self.precision("amount"))
		if self.amount <= 0:
			frappe.throw(_("Amount must be greater than zero."))
		if flt(self.interest_rate) < 0:
			frappe.throw(_("The interest rate and the profit amount cannot be negative."))

	def validate_fees_account(self):
		self.upfront_fees = flt(self.upfront_fees, self.precision("upfront_fees"))
		if not self.upfront_fees:
			return
		if not self.fees_account:
			frappe.throw(_("Fees Account is required for the upfront fees."))
		details = frappe.db.get_value(
			"Account",
			self.fees_account,
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
				_("Fees Account {0} must be a ledger Expense account of {1} in {2}.").format(
					frappe.bold(self.fees_account), frappe.bold(self.company), company_currency
				)
			)

	def make_schedule(self):
		rows = self.build_schedule_rows()
		self.set("repayment_schedule", [])
		for row in rows:
			self.append("repayment_schedule", row)
		if not self.first_repayment_date and rows:
			self.first_repayment_date = rows[0].due_date

	def build_schedule_rows(self) -> list:
		return schedule.build_schedule(
			amount=self.amount,
			start_date=self.drawdown_date,
			tenor_months=self.tenor_months,
			frequency=self.repayment_frequency,
			method=self.repayment_method,
			first_repayment_date=self.first_repayment_date,
			grace_months=self.grace_months,
			rate=self.interest_rate,
			day_count=self.day_count,
			profit_amount=self.profit_amount,
			precision=self.precision("amount"),
		)

	def validate_schedule_matches_terms(self):
		"""A schedule left unchanged while a term changed was built from the old terms: refuse it, unless the
		new terms give the very same schedule (the form clears the schedule when a term changes; an empty
		schedule is rebuilt on save)."""
		before = None if self.is_new() else self.get_doc_before_save()
		if not before or before.docstatus != 0:
			return
		changed = [f for f in TERM_FIELDS if _term_value(self, f) != _term_value(before, f)]
		if not changed or _schedule_key(self) != _schedule_key(before):
			return
		if _schedule_key(self) != _schedule_key(frappe._dict(repayment_schedule=self.build_schedule_rows())):
			frappe.throw(
				_("{0} changed: regenerate the repayment schedule (Generate Schedule) before saving.").format(
					", ".join(_(self.meta.get_label(f)) for f in changed)
				),
				title=_("Regenerate the Schedule"),
			)

	def validate_schedule(self):
		"""Rows in date order after the drawdown date, principal adding up to the amount exactly."""
		precision = self.precision("amount")
		previous = getdate(self.drawdown_date)
		outstanding = self.amount
		for index, row in enumerate(self.repayment_schedule, 1):
			if self.resets_system_fields():
				for fieldname in PAYMENT_FIELDS:
					row.set(fieldname, None)
				row.status, row.is_prepayment = "Pending", 0
			row.installment_no = index
			row.principal = flt(row.principal, precision)
			row.interest = flt(row.interest, precision)
			if row.principal < 0 or row.interest < 0:
				frappe.throw(_("Row #{0}: Principal and interest cannot be negative.").format(row.idx))
			if getdate(row.due_date) <= previous:
				frappe.throw(
					_("Row #{0}: Due Date {1} must be after {2}.").format(
						row.idx, formatdate(row.due_date), formatdate(previous)
					)
				)
			previous = getdate(row.due_date)
			outstanding = flt(outstanding - row.principal, precision)
			row.total = flt(row.principal + row.interest, precision)
			row.outstanding_after = outstanding
		self.total_principal = flt(sum(flt(r.principal) for r in self.repayment_schedule), precision)
		self.total_interest = flt(sum(flt(r.interest) for r in self.repayment_schedule), precision)
		self.total_payable = flt(self.total_principal + self.total_interest, precision)
		if self.repayment_schedule and self.total_principal != self.amount:
			frappe.throw(
				_(
					"The principal of the repayment schedule ({0}) must equal the drawdown amount ({1})."
				).format(
					fmt_money(self.total_principal, currency=self.currency),
					fmt_money(self.amount, currency=self.currency),
				)
			)

	def validate_facility_open(self):
		facility = frappe.get_doc(FACILITY, self.facility)
		self._facility_doc = facility
		if facility.status != ACTIVE:
			frappe.throw(
				_("Bank Facility {0} is {1}: drawdowns need an Active facility.").format(
					frappe.bold(facility.name), _(facility.status, context=FACILITY)
				)
			)
		date = getdate(self.drawdown_date)
		if date < getdate(facility.start_date) or date > getdate(facility.expiry_date):
			frappe.throw(
				_("The Drawdown Date {0} is outside the facility period {1} - {2}.").format(
					formatdate(date), formatdate(facility.start_date), formatdate(facility.expiry_date)
				)
			)

	def validate_available_limit(self):
		utilisation = get_utilisation(self._facility_doc, self.limit_type)
		available = utilisation.limits[0].available if utilisation.limits else 0
		if flt(self.amount) > flt(available):
			frappe.throw(
				_(
					"Drawdown {0} exceeds the available {1} limit of Bank Facility {2}: available {3} (total available {4})."
				).format(
					fmt_money(self.amount, currency=self.currency),
					_(self.limit_type, context=FACILITY),
					frappe.bold(self.facility),
					fmt_money(available, currency=self.currency),
					fmt_money(utilisation.available, currency=self.currency),
				),
				title=_("Limit Exceeded"),
			)

	# ------------------------------------------------------------------ actions

	@frappe.whitelist(methods=["POST"])
	def generate_schedule(self):
		"""Rebuild the repayment schedule from the terms (draft only). Returns the rows; the form saves them."""
		self.check_permission("write")
		if self.docstatus != 0:
			frappe.throw(_("The schedule can only be generated on a draft drawdown."))
		self.set_facility_details()
		self.set_terms_defaults()
		self.validate_amounts()
		self.make_schedule()
		self.validate_schedule()
		return self.as_dict()

	@frappe.whitelist(methods=["POST"])
	def refresh_status(self):
		# Read permission is enough on purpose: it only recomputes the derived row / repayment statuses from the
		# stored payments and today's date (what the daily job does); no user input is written.
		self.check_permission("read")
		refresh_repayment_status(self.name)


def _term_value(doc, fieldname: str):
	value = doc.get(fieldname)
	if fieldname in ("drawdown_date", "first_repayment_date"):
		return getdate(value) if value else None
	if fieldname in ("day_count", "repayment_frequency", "repayment_method"):
		return value or None
	return flt(value)


def _schedule_key(doc) -> list[tuple]:
	return [(getdate(r.due_date), flt(r.principal), flt(r.interest)) for r in doc.repayment_schedule]
