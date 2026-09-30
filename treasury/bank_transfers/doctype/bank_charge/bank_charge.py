# Copyright (c) 2026, Ahmed Abdelmageed and contributors
# For license information, please see license.txt

import erpnext
import frappe
from erpnext.accounts.doctype.account.account import get_account_currency
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, flt, fmt_money, getdate, today

from treasury.accounting import dimensions, payment_modes, posting
from treasury.balance_alerts import guard
from treasury.bank_facilities import instruments
from treasury.bank_transfers import charges
from treasury.treasury_setup.doctype.bank_charge_category.bank_charge_category import get_account_row
from treasury.treasury_setup.doctype.treasury_company_settings.treasury_company_settings import (
	validate_expense_account,
)
from treasury.utils import voucher_summary

DOCTYPE = "Bank Charge"
# Written only by treasury.bank_transfers.charges (on submit / cancel, the Reconcile action and refunds).
LIFECYCLE_FIELDS = (
	"status",
	"accounting_status",
	"payment_entry",
	"journal_entry",
	"clearance_date",
	"bank_transaction",
	"bank_transaction_date",
	"refunded_amount",
)
# `bank_transaction` may be set on a draft (PDC Bank Clearing, REST): it is validated, not reset.
DRAFT_RESET_FIELDS = tuple(
	f for f in LIFECYCLE_FIELDS if f not in ("status", "accounting_status", "bank_transaction")
)
# Defaults a refund takes from the charge it refunds.
REFUND_COPY_FIELDS = ("company", "bank_account", "category", "expense_account", "cost_center", "project")


class BankCharge(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from treasury.bank_transfers.doctype.bank_charge_status_log.bank_charge_status_log import (
			BankChargeStatusLog,
		)

		accounting_status: DF.Literal["Not Posted", "Posted"]
		against_bank_charge: DF.Link | None
		allow_low_balance: DF.Check
		amended_from: DF.Link | None
		amount: DF.Currency
		bank: DF.Link | None
		bank_account: DF.Link
		bank_reference: DF.Data | None
		bank_transaction: DF.Link | None
		bank_transaction_date: DF.Date | None
		base_amount: DF.Currency
		category: DF.Link
		clearance_date: DF.Date | None
		company: DF.Link
		cost_center: DF.Link | None
		currency: DF.Link | None
		description: DF.SmallText | None
		entry_type: DF.Literal["Charge", "Refund"]
		exchange_rate: DF.Float
		expense_account: DF.Link
		journal_entry: DF.Link | None
		mode_of_payment: DF.Link | None
		naming_series: DF.Literal["BCH-.YYYY.-.#####"]
		payment_entry: DF.Link | None
		posting_date: DF.Date
		project: DF.Link | None
		refunded_amount: DF.Currency
		remarks: DF.SmallText | None
		status: DF.Literal["Draft", "Posted", "Reconciled", "Cancelled"]
		status_log: DF.Table[BankChargeStatusLog]
		title: DF.Data | None
		vouchers_html: DF.HTML | None
	# end: auto-generated types

	# ------------------------------------------------------------------ hooks

	def onload(self):
		if not self.is_new():
			self.set_onload("voucher_summary", voucher_summary.render(self.doctype, [self.name]))
		self.set_onload("charge_settings", charge_settings(self.company))

	def insert(self, *args, **kwargs):
		# a new charge starts with an empty log, whatever the client sent (before_insert writes the first row)
		self.set("status_log", [])
		return super().insert(*args, **kwargs)

	def before_insert(self):
		self.status = charges.DRAFT
		self.accounting_status = charges.NOT_POSTED
		charges.log(self, "Created", charges.DRAFT, charges.NOT_POSTED)

	def validate(self):
		# draft saves and the submit itself: the lifecycle values are set by on_submit only
		self.status = charges.DRAFT
		self.accounting_status = charges.NOT_POSTED
		instruments.reset_draft_fields(self, DRAFT_RESET_FIELDS)
		guard.reset_override_flag(self)
		self.set_refund_defaults()
		if not (self.company and self.bank_account and self.category):
			return  # Frappe's mandatory check names the missing fields
		settings = posting.get_company_settings(self.company)
		self.validate_bank_account()
		self.validate_amounts()
		self.validate_category()
		self.validate_refund()
		if self.bank_transaction:
			charges.validate_bank_transaction(self, self.bank_transaction)
		self.validate_accounting_details(settings)
		self.title = f"{_(self.category)} · {self.bank_account}"

	def before_submit(self):
		# low balance guard (a refund raises the balance: not guarded); finished in on_submit
		guard.check_override_flag(self.allow_low_balance)
		if self.entry_type == charges.CHARGE:
			self.flags.balance_snapshot = guard.begin(self.company, [self.bank_account])

	def on_submit(self):
		charges.on_submit(self)

	def before_update_after_submit(self):
		instruments.validate_protected_fields(self, LIFECYCLE_FIELDS)

	def before_cancel(self):
		charges.before_cancel(self)

	def on_cancel(self):
		charges.on_cancel(self)

	# ------------------------------------------------------------------ validation

	def validate_bank_account(self):
		details = frappe.db.get_value(
			"Bank Account",
			self.bank_account,
			["company", "is_company_account", "account", "disabled", "bank"],
			as_dict=True,
		)
		if (
			not details
			or not details.is_company_account
			or details.company != self.company
			or details.disabled
			or not details.account
			or frappe.get_cached_value("Account", details.account, "account_type") != "Bank"
		):
			frappe.throw(
				_("Bank Account {0} must be a company bank account of {1}.").format(
					frappe.bold(self.bank_account), frappe.bold(self.company)
				)
			)
		self.bank = details.bank
		self.currency = get_account_currency(details.account)

	def validate_amounts(self):
		self.amount = flt(self.amount, self.precision("amount"))
		if self.amount <= 0:
			frappe.throw(_("Amount must be greater than zero."))
		if self.currency == erpnext.get_company_currency(self.company):
			self.exchange_rate = 1
		elif flt(self.exchange_rate) <= 0:
			self.exchange_rate = posting.get_cheque_exchange_rate(
				self.currency, self.company, self.posting_date
			)
		self.base_amount = flt(self.amount * flt(self.exchange_rate), self.precision("base_amount"))

	def set_refund_defaults(self):
		"""A refund of a charge takes the charge's category and accounts when they are empty; a charge refunds
		nothing."""
		if self.entry_type != charges.REFUND:
			self.against_bank_charge = None
			return
		if not self.against_bank_charge:
			return
		charge = frappe.db.get_value(DOCTYPE, self.against_bank_charge, REFUND_COPY_FIELDS, as_dict=True)
		for fieldname in REFUND_COPY_FIELDS[2:]:
			if charge and not self.get(fieldname):
				self.set(fieldname, charge.get(fieldname))

	def validate_category(self):
		details = frappe.db.get_value(
			"Bank Charge Category", self.category, ["disabled", "default_description"], as_dict=True
		)
		if not details:
			frappe.throw(_("{0} {1} does not exist").format(_("Bank Charge Category"), self.category))
		if cint(details.disabled) and self.is_new():
			frappe.throw(_("Bank Charge Category {0} is disabled.").format(frappe.bold(self.category)))
		row = get_account_row(self.category, self.company) or frappe._dict()
		if not self.expense_account:
			self.expense_account = row.expense_account
		if not self.expense_account:
			frappe.throw(
				_("Set the Expense Account of Bank Charge Category {0} for company {1}.").format(
					frappe.bold(self.category), frappe.bold(self.company)
				)
			)
		validate_expense_account(
			self.company, self.expense_account, _(self.meta.get_label("expense_account"), context=DOCTYPE)
		)
		if not self.cost_center:
			self.cost_center = row.cost_center or erpnext.get_default_cost_center(self.company)
		if not self.description and details.default_description:
			self.description = details.default_description

	def validate_refund(self):
		if self.entry_type != charges.REFUND or not self.against_bank_charge:
			return
		charge = frappe.db.get_value(
			DOCTYPE,
			self.against_bank_charge,
			[
				"docstatus",
				"entry_type",
				"company",
				"bank_account",
				"currency",
				"category",
				"expense_account",
				"amount",
			],
			as_dict=True,
		)
		if (
			not charge
			or charge.docstatus != 1
			or charge.entry_type != charges.CHARGE
			or (charge.company, charge.bank_account, charge.currency)
			!= (self.company, self.bank_account, self.currency)
		):
			frappe.throw(
				_("Bank Charge {0} is not a submitted charge of this bank account.").format(
					frappe.bold(self.against_bank_charge)
				)
			)
		if (charge.category, charge.expense_account) != (self.category, self.expense_account):
			frappe.throw(
				_("A refund must use the category and expense account of Bank Charge {0}.").format(
					frappe.bold(self.against_bank_charge)
				)
			)
		refunded = sum(flt(r.amount) for r in charges.submitted_refunds(self.against_bank_charge, self.name))
		left = flt(flt(charge.amount) - refunded, self.precision("amount"))
		if self.amount > left:
			frappe.throw(
				_("Refunds of Bank Charge {0} cannot exceed its amount ({1} left).").format(
					frappe.bold(self.against_bank_charge), fmt_money(max(left, 0), currency=self.currency)
				)
			)

	def validate_accounting_details(self, settings):
		for fieldname, value in dimensions.default_dimensions(self.company).items():
			if value and self.meta.has_field(fieldname) and not self.get(fieldname):
				self.set(fieldname, value)
		cost_center = frappe.db.get_value(
			"Cost Center", self.cost_center, ["company", "is_group"], as_dict=True
		)
		if self.cost_center and (not cost_center or cost_center.company != self.company):
			frappe.throw(
				_("Cost Center {0} does not belong to company {1}.").format(
					frappe.bold(self.cost_center), frappe.bold(self.company)
				)
			)
		if cost_center and cost_center.is_group:
			frappe.throw(_("Cost Center {0} is a group.").format(frappe.bold(self.cost_center)))
		if self.project:
			project_company = frappe.db.get_value("Project", self.project, "company")
			if project_company and project_company != self.company:
				frappe.throw(
					_("Project {0} does not belong to company {1}.").format(
						frappe.bold(self.project), frappe.bold(self.company)
					)
				)
		if not self.mode_of_payment and settings.bank_charge_voucher != charges.JOURNAL_ENTRY:
			self.mode_of_payment = payment_modes.resolve(
				self.company, self.bank_account, charges.MODE_METHOD
			).mode_of_payment
		if self.mode_of_payment and not frappe.db.get_value(
			"Mode of Payment", self.mode_of_payment, "enabled"
		):
			frappe.throw(_("Mode of Payment {0} is disabled.").format(frappe.bold(self.mode_of_payment)))

	# ------------------------------------------------------------------ actions
	# POST only: it changes data, and Frappe checks the CSRF token only on POST requests.

	@frappe.whitelist(methods=["POST"])
	def reconcile_bank_transaction(self, bank_transaction=None):
		return charges.reconcile(self.name, bank_transaction)


@frappe.whitelist(methods=["GET"])
def make_refund(source_name: str) -> dict:
	"""An unsaved Refund of a submitted charge, for what is left to refund (nothing is written)."""
	source = frappe.get_doc(DOCTYPE, source_name)
	source.check_permission("read")
	frappe.has_permission(DOCTYPE, "create", throw=True)
	left = flt(flt(source.amount) - flt(source.refunded_amount), source.precision("amount"))
	if source.docstatus != 1 or source.entry_type != charges.CHARGE or left <= 0:
		frappe.throw(_("Bank Charge {0} has nothing left to refund.").format(frappe.bold(source_name)))
	refund = frappe.new_doc(DOCTYPE)
	refund.update({fieldname: source.get(fieldname) for fieldname in REFUND_COPY_FIELDS})
	refund.update(dimensions.get_dimension_values(source))
	refund.update(
		{
			"entry_type": charges.REFUND,
			"against_bank_charge": source.name,
			"amount": left,
			"posting_date": getdate(today()),
			"bank": source.bank,
			"currency": source.currency,
		}
	)
	result = refund.as_dict()
	result["__islocal"] = 1
	return result


@frappe.whitelist(methods=["GET"])
def get_category_defaults(category: str, company: str) -> dict:
	"""The expense account, cost center and default description of a category for a company (form script).
	The user needs create or write permission on a Bank Charge of that company (User Permissions apply)."""
	probe = frappe.new_doc(DOCTYPE)
	probe.company = company
	if not any(frappe.has_permission(DOCTYPE, ptype, doc=probe) for ptype in ("create", "write")):
		frappe.throw(_("You are not permitted to create or change Bank Charges."), frappe.PermissionError)
	row = get_account_row(category, company) or frappe._dict()
	description = (
		frappe.db.get_value("Bank Charge Category", category, "default_description") if category else None
	)
	return {
		"expense_account": row.get("expense_account"),
		"cost_center": row.get("cost_center"),
		"default_description": description,
	}


def charge_settings(company: str | None) -> dict:
	"""What the form's draft intro says Submit will do (design 7d §1.3): the company's voucher shape and
	clearance setting, and its currency."""
	settings = (
		frappe.db.get_value(
			"Treasury Company Settings",
			company,
			["bank_charge_voucher", "bank_charge_clearance"],
			as_dict=True,
		)
		if company
		else None
	) or frappe._dict()
	return {
		"bank_charge_voucher": settings.bank_charge_voucher or charges.PAYMENT_ENTRY,
		"bank_charge_clearance": settings.bank_charge_clearance or charges.ON_POSTING,
		"company_currency": erpnext.get_company_currency(company) if company else None,
	}


def on_doctype_update():
	frappe.db.add_index("Bank Charge", ["company", "bank_account", "posting_date"])
	frappe.db.add_index("Bank Charge", ["bank_transaction"])
