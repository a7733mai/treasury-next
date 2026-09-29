# Copyright (c) 2026, Ahmed Abdelmageed and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, getdate

from treasury.bank_facilities import instruments, lg
from treasury.bank_facilities.utilisation import refresh_utilisation

# Written only by treasury.bank_facilities.lg (on submit, or by the actions with `treasury_lifecycle`).
LIFECYCLE_FIELDS = (
	"open_amount",
	"effective_expiry_date",
	"margin_held",
	"release_date",
	"claim_date",
	"claim_amount",
	"claim_account",
	"claim_outstanding",
)
SYSTEM_FIELDS = ("margin_journal", "commission_journal")
REFERENCE_DOCTYPES = ("Project", "Contract", "Sales Order", "Purchase Order", "Quotation")


class LetterofGuarantee(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from treasury.bank_facilities.doctype.bank_instrument_status_log.bank_instrument_status_log import (
			BankInstrumentStatusLog,
		)

		amended_from: DF.Link | None
		amount: DF.Currency
		bank: DF.Link | None
		bank_account: DF.Link
		bank_guarantee: DF.Link | None
		beneficiary: DF.DynamicLink | None
		beneficiary_name: DF.Data | None
		beneficiary_type: DF.Literal["Customer", "Supplier", "Other"]
		cash_margin_amount: DF.Currency
		cash_margin_percent: DF.Percent
		claim_account: DF.Link | None
		claim_amount: DF.Currency
		claim_date: DF.Date | None
		claim_outstanding: DF.Currency
		commission_account: DF.Link | None
		commission_amount: DF.Currency
		commission_journal: DF.Link | None
		commission_rate: DF.Percent
		company: DF.Link | None
		currency: DF.Link | None
		effective_expiry_date: DF.Date | None
		exchange_rate: DF.Float
		expiry_date: DF.Date
		facility: DF.Link
		issue_date: DF.Date
		lg_no: DF.Data | None
		lg_type: DF.Literal[
			"Bid Bond", "Performance", "Advance Payment", "Retention", "Financial", "Customs", "Other"
		]
		margin_account: DF.Link | None
		margin_held: DF.Currency
		margin_journal: DF.Link | None
		naming_series: DF.Literal["LG-.YYYY.-.#####"]
		open_amount: DF.Currency
		reference_doctype: DF.Link | None
		reference_name: DF.DynamicLink | None
		release_date: DF.Date | None
		remarks: DF.SmallText | None
		status: DF.Literal[
			"Draft", "Issued", "Extended", "Reduced", "Released", "Claimed", "Expired", "Cancelled"
		]
		status_log: DF.Table[BankInstrumentStatusLog]
	# end: auto-generated types

	# ------------------------------------------------------------------ hooks

	def insert(self, *args, **kwargs):
		# a new guarantee starts with an empty log, whatever the client sent (before_insert writes the first row)
		self.set("status_log", [])
		return super().insert(*args, **kwargs)

	def before_insert(self):
		self.status = lg.DRAFT
		instruments.log(self, "Created", lg.DRAFT)

	def validate(self):
		# draft saves and the submit itself: the lifecycle values are set by on_submit only
		self.status = lg.DRAFT
		instruments.reset_draft_fields(self, (*LIFECYCLE_FIELDS, *SYSTEM_FIELDS))
		facility, limit = instruments.set_facility_details(self, lg.LIMIT_TYPE)
		instruments.validate_bank_account(self)
		instruments.set_exchange_rate(self, self.issue_date)
		self.validate_amount_and_dates()
		self.validate_beneficiary()
		self.validate_reference()
		instruments.validate_margin_and_commission(self, facility, limit)

	def before_submit(self):
		# serialise the instruments of one facility: the limit check must see every earlier one
		facility = instruments.lock_facility(self.facility)
		instruments.validate_facility_open(facility, self.issue_date, _(self.doctype))
		instruments.check_available(facility, lg.LIMIT_TYPE, self.amount, self.currency, _(self.doctype))

	def on_submit(self):
		lg.issue(self)
		refresh_utilisation(self.facility)

	def before_update_after_submit(self):
		instruments.validate_protected_fields(self, ("status", *LIFECYCLE_FIELDS))

	def before_cancel(self):
		if self.status == lg.CLAIMED:
			frappe.throw(
				_("Letter of Guarantee {0} was claimed and cannot be cancelled.").format(frappe.bold(self.name)),
				title=_("Not Allowed"),
			)

	def on_cancel(self):
		# the margin / commission / release Journal Entries (before Frappe's check for submitted back links)
		lg.cancel(self)
		refresh_utilisation(self.facility)

	# ------------------------------------------------------------------ validation

	def validate_amount_and_dates(self):
		self.amount = flt(self.amount, self.precision("amount"))
		if self.amount <= 0:
			frappe.throw(_("Amount must be greater than zero."))
		if getdate(self.expiry_date) <= getdate(self.issue_date):
			frappe.throw(_("The Expiry Date must be after the Issue Date."))

	def validate_beneficiary(self):
		if self.beneficiary_type == "Other":
			self.beneficiary = None
			if not self.beneficiary_name:
				frappe.throw(_("Enter the Beneficiary Name."))
			return
		if self.beneficiary_type not in ("Customer", "Supplier") or not self.beneficiary:
			frappe.throw(_("Select the Beneficiary."))
		name_field = "customer_name" if self.beneficiary_type == "Customer" else "supplier_name"
		name = frappe.db.get_value(self.beneficiary_type, self.beneficiary, name_field)
		if name is None and not frappe.db.exists(self.beneficiary_type, self.beneficiary):
			frappe.throw(_("{0} {1} does not exist").format(_(self.beneficiary_type), self.beneficiary))
		self.beneficiary_name = name or self.beneficiary

	def validate_reference(self):
		# ERPNext's Bank Guarantee is only linked for reference (never changed by Treasury)
		if self.bank_guarantee and frappe.db.get_value("Bank Guarantee", self.bank_guarantee, "docstatus") == 2:
			frappe.throw(_("Bank Guarantee {0} is cancelled.").format(frappe.bold(self.bank_guarantee)))
		if not self.reference_doctype:
			self.reference_name = None
			return
		if self.reference_doctype not in REFERENCE_DOCTYPES:
			frappe.throw(
				_("Reference Type must be one of: {0}.").format(", ".join(_(d) for d in REFERENCE_DOCTYPES))
			)
		if not self.reference_name:
			frappe.throw(_("Select the Reference Name."))
		if not frappe.db.exists(self.reference_doctype, self.reference_name):
			frappe.throw(_("{0} {1} does not exist").format(_(self.reference_doctype), self.reference_name))
		company = None
		if frappe.get_meta(self.reference_doctype).has_field("company"):
			company = frappe.db.get_value(self.reference_doctype, self.reference_name, "company")
		if company and company != self.company:
			frappe.throw(
				_("{0} {1} does not belong to company {2}.").format(
					_(self.reference_doctype), frappe.bold(self.reference_name), frappe.bold(self.company)
				)
			)

	# ------------------------------------------------------------------ actions (bank_facilities.lg)
	# POST only: they change data, and Frappe checks the CSRF token only on POST requests.

	@frappe.whitelist(methods=["POST"])
	def extend(self, new_expiry_date=None, extension_date=None, extra_commission=0, remarks=None):
		return lg.extend(self.name, new_expiry_date, extension_date, flt(extra_commission), remarks)

	@frappe.whitelist(methods=["POST"])
	def reduce(self, new_amount=None, reduction_date=None, exchange_rate=None, remarks=None):
		return lg.reduce(self.name, flt(new_amount), reduction_date, flt(exchange_rate) or None, remarks)

	@frappe.whitelist(methods=["POST"])
	def release(self, release_date=None, exchange_rate=None, remarks=None):
		return lg.release(self.name, release_date, flt(exchange_rate) or None, remarks)

	@frappe.whitelist(methods=["POST"])
	def claim(
		self,
		amount=None,
		counter_account=None,
		claim_date=None,
		party_type=None,
		party=None,
		exchange_rate=None,
		remarks=None,
	):
		return lg.claim(
			self.name,
			flt(amount),
			counter_account,
			claim_date,
			party_type or None,
			party or None,
			flt(exchange_rate) or None,
			remarks,
		)

	@frappe.whitelist(methods=["POST"])
	def repay_claim(self, amount=None, repayment_date=None, exchange_rate=None, remarks=None):
		return lg.repay_claim(
			self.name,
			None if amount in (None, "") else flt(amount),
			repayment_date,
			flt(exchange_rate) or None,
			remarks,
		)


def on_doctype_update():
	frappe.db.add_index("Letter of Guarantee", ["facility", "status"])
