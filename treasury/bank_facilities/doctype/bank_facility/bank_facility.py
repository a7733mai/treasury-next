# Copyright (c) 2026, Ahmed Abdelmageed and contributors
# For license information, please see license.txt

import erpnext
import frappe
from erpnext.accounts.doctype.account.account import get_account_currency
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, getdate

from treasury.bank_facilities import facility as lifecycle
from treasury.bank_facilities import lenders
from treasury.bank_facilities.utilisation import LOAN_TYPES, get_utilisation, refresh_utilisation
from treasury.utils.protected_fields import changed_fields, changed_tables

# Written only by treasury.bank_facilities.facility (flag `treasury_lifecycle`).
PROTECTED_FIELDS = ("status", "renewal_date")
PROTECTED_TABLES = ("status_log",)
# The agreement's key terms: fixed once the facility left Draft (they move through Renew only). An empty
# `lender` of a bank facility is the one exception: `lenders.get_lender_supplier` fills it (db write).
LOCKED_FIELDS = (
	"company",
	"bank",
	"currency",
	"total_limit",
	"start_date",
	"expiry_date",
	"lender_type",
	"lender",
	"is_islamic",
)
LATE_PAYMENT_TO_LENDER = "Paid to Lender"
LATE_PAYMENT_TO_CHARITY = "Payable to Charity"
# A sub-limit row keeps these once the facility left Draft (new sub-limits may still be added).
LOCKED_LIMIT_FIELDS = ("limit_type", "limit_amount", "liability_account")
# Display fields refreshed by treasury.bank_facilities.utilisation (never trusted from the client).
DISPLAY_FIELDS = ("total_utilised", "total_available", "utilisation_refreshed_on")


class BankFacility(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from treasury.bank_facilities.doctype.bank_facility_limit.bank_facility_limit import BankFacilityLimit
		from treasury.bank_facilities.doctype.bank_facility_security_cheque.bank_facility_security_cheque import (
			BankFacilitySecurityCheque,
		)
		from treasury.bank_facilities.doctype.bank_facility_status_log.bank_facility_status_log import (
			BankFacilityStatusLog,
		)

		accrued_interest_account: DF.Link | None
		bank: DF.Link | None
		charity_payable_account: DF.Link | None
		collateral_notes: DF.SmallText | None
		commission_expense_account: DF.Link | None
		company: DF.Link
		contract_no: DF.Data | None
		currency: DF.Link
		default_bank_account: DF.Link | None
		expiry_date: DF.Date
		facility_name: DF.Data
		interest_expense_account: DF.Link | None
		is_islamic: DF.Check
		late_payment_charity_account: DF.Link | None
		late_payment_mode: DF.Literal["Paid to Lender", "Payable to Charity"]
		lender: DF.Link | None
		lender_name: DF.Data | None
		lender_type: DF.Literal["Bank", "Finance Company"]
		limits: DF.Table[BankFacilityLimit]
		naming_series: DF.Literal["BF-.YYYY.-.#####"]
		renewal_date: DF.Date | None
		security_cheques: DF.Table[BankFacilitySecurityCheque]
		start_date: DF.Date
		status: DF.Literal["Draft", "Active", "Suspended", "Expired", "Closed"]
		status_log: DF.Table[BankFacilityStatusLog]
		total_available: DF.Currency
		total_limit: DF.Currency
		total_utilised: DF.Currency
		utilisation_refreshed_on: DF.Datetime | None
	# end: auto-generated types

	# ------------------------------------------------------------------ hooks

	def insert(self, *args, **kwargs):
		# Frappe validates links before `before_insert`: reset whatever the client sent first.
		if not self.flags.treasury_lifecycle:
			self.reset_protected_fields()
		return super().insert(*args, **kwargs)

	def before_insert(self):
		lifecycle.log_creation(self)

	def onload(self):
		# design 7g P5: "{0} security cheque(s) · {1}" on the form
		self.set_onload("security_summary", self.get_security_summary())

	def get_security_summary(self) -> dict | None:
		"""Count and total of the facility's security cheques not returned / encashed / lost / cancelled (the
		cheques the user may read)."""
		from treasury.security_cheques.lifecycle import FINAL_STATUSES

		names = [row.security_cheque for row in self.security_cheques if row.security_cheque]
		if not names:
			return None
		cheques = frappe.get_list(
			"Security Cheque",
			filters={"name": ["in", names], "status": ["not in", FINAL_STATUSES]},
			fields=["amount", "currency"],
		)
		if not cheques:
			return None
		currency = cheques[0].currency or self.currency
		total = sum(flt(c.amount) for c in cheques if (c.currency or currency) == currency)
		return {"count": len(cheques), "total": flt(total, self.precision("total_limit")), "currency": currency}

	def validate(self):
		self.validate_protected_fields()
		self.validate_dates()
		self.validate_lender()
		self.validate_limits()
		self.validate_bank_account()
		self.validate_accounts()
		self.validate_security_cheques()
		self.validate_locked_fields()

	def on_trash(self):
		if self.status != lifecycle.DRAFT or frappe.db.exists("Facility Drawdown", {"facility": self.name}):
			frappe.throw(
				_(
					"Bank Facility {0} cannot be deleted once activated or when it has drawdowns. Close it instead."
				).format(frappe.bold(self.name))
			)

	# ------------------------------------------------------------------ validation

	def reset_protected_fields(self):
		for fieldname in (*PROTECTED_FIELDS, *DISPLAY_FIELDS):
			self.set(fieldname, None)
		for table in PROTECTED_TABLES:
			self.set(table, [])
		for row in self.limits:
			row.utilised = row.available = 0

	def validate_protected_fields(self):
		"""Status, renewal date and the status log change only through the facility actions."""
		if self.is_new():
			return
		before = self.get_doc_before_save()
		if not before:
			return
		lifecycle_save = bool(self.flags.treasury_lifecycle)
		changed = [] if lifecycle_save else changed_fields(self, before, PROTECTED_FIELDS)
		changed += changed_tables(self, before, PROTECTED_TABLES, lifecycle_save)
		if changed:
			frappe.throw(
				_("{0} can only be changed by the Bank Facility actions.").format(
					", ".join(_(self.meta.get_label(f)) for f in changed)
				),
				title=_("Not Allowed"),
			)
		# display fields: keep the stored values, whatever the client sent
		for fieldname in DISPLAY_FIELDS:
			self.set(fieldname, before.get(fieldname))

	def validate_dates(self):
		if getdate(self.expiry_date) < getdate(self.start_date):
			frappe.throw(_("The Expiry Date cannot be before the Start Date."))

	def validate_lender(self):
		"""The lender (a Supplier; for a bank created on demand, `lenders.get_lender_supplier`), the sub-limit
		types a finance company may grant, the lender's name and the Islamic flag (spec 7g §2.1)."""
		self.lender_type = self.lender_type or lenders.BANK
		if self.lender_type == lenders.FINANCE_COMPANY:
			if not self.lender:
				frappe.throw(_("A Finance Company facility needs a Lender (Supplier)."))
			for row in self.limits:
				if row.limit_type not in lenders.FINANCE_COMPANY_TYPES:
					frappe.throw(
						_("Row #{0}: A finance company facility cannot have a {1} sub-limit.").format(
							row.idx, _(row.limit_type, context=self.doctype)
						)
					)
		elif not self.bank:
			frappe.throw(_("Bank is required when the Lender Type is Bank."))
		if not self.lender and not self.is_new() and self.status != lifecycle.DRAFT:
			# (locked) filled by `lenders.get_lender_supplier` since the form was loaded
			self.lender = frappe.db.get_value(self.doctype, self.name, "lender")
		self.lender_name = None
		if self.lender:
			supplier = frappe.db.get_value("Supplier", self.lender, ["supplier_name", "disabled"], as_dict=True)
			if not supplier:
				frappe.throw(_("{0} {1} does not exist").format(_("Supplier"), self.lender))
			if supplier.disabled:
				frappe.throw(_("Supplier {0} is disabled.").format(frappe.bold(self.lender)))
			self.lender_name = supplier.supplier_name
		elif self.bank:
			self.lender_name = frappe.db.get_value("Bank", self.bank, "bank_name") or self.bank
		if self.status in (None, "", lifecycle.DRAFT) and any(
			row.limit_type in lenders.ISLAMIC_TYPES for row in self.limits
		):
			self.is_islamic = 1

	def validate_limits(self):
		precision = self.precision("total_limit")
		self.total_limit = flt(self.total_limit, precision)
		if self.total_limit <= 0:
			frappe.throw(_("Total Limit must be greater than zero."))
		if not self.limits:
			frappe.throw(_("Add at least one sub-limit."))
		seen = set()
		for row in self.limits:
			if row.limit_type in seen:
				frappe.throw(
					_("Row #{0}: Sub-limit {1} appears more than once.").format(
						row.idx, _(row.limit_type, context=self.doctype)
					)
				)
			seen.add(row.limit_type)
			row.limit_amount = flt(row.limit_amount, precision)
			if row.limit_amount <= 0:
				frappe.throw(_("Row #{0}: Limit Amount must be greater than zero.").format(row.idx))
			if row.limit_amount > self.total_limit:
				frappe.throw(
					_("Row #{0}: Limit Amount {1} cannot exceed the Total Limit {2}.").format(
						row.idx, row.limit_amount, self.total_limit
					)
				)
			for fieldname in ("interest_rate", "margin_rate", "commission_rate", "cash_margin_percent"):
				if flt(row.get(fieldname)) < 0:
					frappe.throw(
						_("Row #{0}: {1} cannot be negative.").format(
							row.idx, _(row.meta.get_label(fieldname))
						)
					)
			if row.limit_type in LOAN_TYPES and not row.liability_account:
				frappe.throw(
					_("Row #{0}: Liability Account is required for sub-limit {1}.").format(
						row.idx, _(row.limit_type, context=self.doctype)
					)
				)
			if row.liability_account:
				label = _("Row #{0}: Liability Account").format(row.idx)
				self.validate_account(row.liability_account, label, "Liability", self.currency)

	def validate_bank_account(self):
		if not self.default_bank_account:
			if any(row.limit_type == "Overdraft" for row in self.limits):
				frappe.throw(_("Default Bank Account is required for an Overdraft sub-limit."))
			return
		bank_account = frappe.db.get_value(
			"Bank Account",
			self.default_bank_account,
			["company", "is_company_account", "account", "bank"],
			as_dict=True,
		)
		if not bank_account or not bank_account.is_company_account or bank_account.company != self.company:
			frappe.throw(
				_("Bank Account {0} must be a company bank account of {1}.").format(
					frappe.bold(self.default_bank_account), frappe.bold(self.company)
				)
			)
		if self.lender_type == lenders.BANK and bank_account.bank != self.bank:
			frappe.throw(
				_("Bank Account {0} is not at bank {1}.").format(
					frappe.bold(self.default_bank_account), frappe.bold(self.bank)
				)
			)
		if not bank_account.account:
			frappe.throw(
				_("Bank Account {0} has no GL account.").format(frappe.bold(self.default_bank_account))
			)
		currency = get_account_currency(bank_account.account)
		if currency != self.currency:
			frappe.throw(
				_("Bank Account {0} is in {1}, but the facility is in {2}.").format(
					frappe.bold(self.default_bank_account), currency, self.currency
				)
			)

	def validate_accounts(self):
		company_currency = erpnext.get_company_currency(self.company)
		for fieldname in ("interest_expense_account", "commission_expense_account"):
			if self.get(fieldname):
				self.validate_account(
					self.get(fieldname), _(self.meta.get_label(fieldname)), "Expense", company_currency
				)
		if self.accrued_interest_account:
			label = _(self.meta.get_label("accrued_interest_account"))
			self.validate_account(self.accrued_interest_account, label, "Liability", self.currency)
			# accruals are sourced to the drawdowns: on a liability account they would count as principal
			for row in self.limits:
				if row.liability_account == self.accrued_interest_account:
					frappe.throw(
						_("Row #{0}: The Liability Account cannot be the {1} {2}.").format(
							row.idx, label, frappe.bold(self.accrued_interest_account)
						)
					)
		self.validate_accrued_interest_account_unchanged()
		self.validate_late_payment_settings(company_currency)

	def validate_late_payment_settings(self, company_currency: str):
		"""Late payment penalties (spec 7g §2.1): the charity expense account and, when they are payable to
		charity, the liability account they are credited to."""
		self.late_payment_mode = self.late_payment_mode or LATE_PAYMENT_TO_LENDER
		if self.late_payment_charity_account:
			label = _(self.meta.get_label("late_payment_charity_account"))
			self.validate_account(self.late_payment_charity_account, label, "Expense", company_currency)
		if self.charity_payable_account:
			label = _(self.meta.get_label("charity_payable_account"))
			self.validate_account(self.charity_payable_account, label, "Liability", company_currency)
		if self.late_payment_mode == LATE_PAYMENT_TO_CHARITY and not (
			self.late_payment_charity_account and self.charity_payable_account
		):
			frappe.throw(_("Set the Late Payment (Charity) Account and the Charity Payable Account."))

	def validate_accrued_interest_account_unchanged(self):
		"""The accrued interest of submitted drawdowns sits on this account (accruals credit it, repayments clear
		it): it cannot move to another account while a drawdown is submitted."""
		if self.is_new():
			return
		before = self.get_doc_before_save()
		if not before or (before.accrued_interest_account or None) == (self.accrued_interest_account or None):
			return
		if frappe.db.exists("Facility Drawdown", {"facility": self.name, "docstatus": 1}):
			frappe.throw(
				_("{0} cannot be changed while Bank Facility {1} has submitted drawdowns.").format(
					_(self.meta.get_label("accrued_interest_account")), frappe.bold(self.name)
				),
				title=_("Not Allowed"),
			)

	def validate_account(self, account: str, label: str, root_type: str, currency: str):
		"""A ledger (not group) account of the company with the given root type and currency; liability accounts
		may not be party accounts (Payable / Receivable), since facility entries carry no party."""
		details = frappe.db.get_value(
			"Account",
			account,
			["company", "is_group", "root_type", "account_type", "account_currency"],
			as_dict=True,
		)
		if not details or details.company != self.company:
			frappe.throw(
				_("{0} {1} does not belong to company {2}.").format(
					label, frappe.bold(account), frappe.bold(self.company)
				)
			)
		if details.is_group:
			frappe.throw(_("{0} {1} cannot be a group account.").format(label, frappe.bold(account)))
		if details.root_type != root_type:
			frappe.throw(
				_("{0} {1} must have Root Type '{2}'.").format(label, frappe.bold(account), _(root_type))
			)
		if details.account_type in ("Payable", "Receivable"):
			frappe.throw(
				_("{0} {1} cannot be a Payable or Receivable account.").format(label, frappe.bold(account))
			)
		if details.account_currency != currency:
			frappe.throw(_("{0} {1} must be in {2}.").format(label, frappe.bold(account), currency))

	def validate_security_cheques(self):
		seen = set()
		for row in self.security_cheques:
			if row.security_cheque in seen:
				frappe.throw(
					_("Row #{0}: Security Cheque {1} appears more than once.").format(
						row.idx, frappe.bold(row.security_cheque)
					)
				)
			seen.add(row.security_cheque)
			company = frappe.db.get_value("Security Cheque", row.security_cheque, "company")
			if company != self.company:
				frappe.throw(
					_("Row #{0}: Security Cheque {1} does not belong to company {2}.").format(
						row.idx, frappe.bold(row.security_cheque), frappe.bold(self.company)
					)
				)

	def validate_locked_fields(self):
		"""Once the facility left Draft, its key terms move only through Renew (sub-limits may be added)."""
		if self.is_new() or self.flags.treasury_lifecycle:
			return
		before = self.get_doc_before_save()
		if not before or before.status == lifecycle.DRAFT:
			return
		changed = changed_fields(self, before, LOCKED_FIELDS)
		rows = {row.name: row for row in self.limits}
		for old in before.limits:
			new = rows.get(old.name)
			if not new or any(
				(old.get(f) or None) != (new.get(f) or None)
				if f != "limit_amount"
				else flt(old.get(f)) != flt(new.get(f))
				for f in LOCKED_LIMIT_FIELDS
			):
				changed.append("limits")
				break
		if changed:
			frappe.throw(
				_("{0} cannot be changed after the facility is activated. Use the Renew action.").format(
					", ".join(_(self.meta.get_label(f)) for f in changed)
				),
				title=_("Not Allowed"),
			)

	# ------------------------------------------------------------------ actions (bank_facilities.facility)
	# POST only: they change data, and Frappe checks the CSRF token only on POST requests.

	@frappe.whitelist(methods=["POST"])
	def activate(self, activation_date=None, remarks=None):
		return lifecycle.activate(self.name, activation_date, remarks)

	@frappe.whitelist(methods=["POST"])
	def suspend(self, suspension_date=None, reason=None):
		return lifecycle.suspend(self.name, suspension_date, reason)

	@frappe.whitelist(methods=["POST"])
	def close(self, closing_date=None, remarks=None):
		return lifecycle.close(self.name, closing_date, remarks)

	@frappe.whitelist(methods=["POST"])
	def renew(
		self, new_expiry_date=None, renewal_date=None, new_total_limit=None, new_limits=None, remarks=None
	):
		return lifecycle.renew(
			self.name,
			new_expiry_date,
			renewal_date,
			None if new_total_limit in (None, "") else flt(new_total_limit),
			new_limits,
			remarks,
		)

	@frappe.whitelist(methods=["POST"])
	def refresh_utilisation(self):
		self.check_permission("read")
		return refresh_utilisation(self.name)

	@frappe.whitelist()
	def get_utilisation(self, as_on=None):
		self.check_permission("read")
		return get_utilisation(self, as_on=getdate(as_on) if as_on else None)


def on_doctype_update():
	frappe.db.add_index("Bank Facility", ["company", "status", "expiry_date"])
