# Copyright (c) 2026, Ahmed Abdelmageed and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, flt, getdate

from treasury.bank_facilities import instruments, lc, trust_receipt
from treasury.bank_facilities.utilisation import refresh_utilisation
from treasury.utils import voucher_summary

# Written only by treasury.bank_facilities.lc (on submit, or by the actions with `treasury_lifecycle`).
LIFECYCLE_FIELDS = (
	"current_amount",
	"open_amount",
	"effective_expiry_date",
	"documents_date",
	"documents_amount",
	"acceptance_date",
	"maturity_date",
	"settlement_date",
	"settled_amount",
	"unused_amount",
	"payment_entry",
	"margin_held",
	"trust_receipt_drawdown",
)
SYSTEM_FIELDS = ("margin_journal", "commission_journal")


class LetterofCredit(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from treasury.bank_facilities.doctype.bank_instrument_status_log.bank_instrument_status_log import (
			BankInstrumentStatusLog,
		)

		acceptance_date: DF.Date | None
		amended_from: DF.Link | None
		amount: DF.Currency
		bank: DF.Link | None
		bank_account: DF.Link
		cash_margin_amount: DF.Currency
		cash_margin_percent: DF.Percent
		charges_account: DF.Link | None
		commission_account: DF.Link | None
		commission_amount: DF.Currency
		commission_journal: DF.Link | None
		commission_rate: DF.Percent
		company: DF.Link | None
		currency: DF.Link | None
		current_amount: DF.Currency
		documents_amount: DF.Currency
		documents_date: DF.Date | None
		effective_expiry_date: DF.Date | None
		exchange_rate: DF.Float
		expiry_date: DF.Date
		facility: DF.Link
		issue_date: DF.Date
		latest_shipment_date: DF.Date | None
		lc_no: DF.Data | None
		lc_type: DF.Literal["Sight", "Usance"]
		margin_account: DF.Link | None
		margin_held: DF.Currency
		margin_journal: DF.Link | None
		maturity_date: DF.Date | None
		naming_series: DF.Literal["LC-.YYYY.-.#####"]
		open_amount: DF.Currency
		payment_entry: DF.Link | None
		purchase_order: DF.Link | None
		remarks: DF.SmallText | None
		settled_amount: DF.Currency
		settlement_date: DF.Date | None
		status: DF.Literal[
			"Draft", "Opened", "Amended", "Documents Received", "Accepted", "Settled", "Expired", "Cancelled"
		]
		status_log: DF.Table[BankInstrumentStatusLog]
		supplier: DF.Link
		supplier_name: DF.Data | None
		tolerance_percent: DF.Percent
		trust_receipt_drawdown: DF.Link | None
		unused_amount: DF.Currency
		usance_days: DF.Int
		vouchers_html: DF.HTML | None
	# end: auto-generated types

	# ------------------------------------------------------------------ hooks

	def onload(self):
		if not self.is_new():
			self.set_onload("voucher_summary", voucher_summary.render(self.doctype, [self.name]))
		if self.facility:
			# the settle dialog offers "Finance by Trust Receipt" (spec 7g §3.9)
			limit = trust_receipt.get_limit(self.facility)
			self.set_onload("has_trust_receipt_limit", bool(limit))
			self.set_onload("trust_receipt_tenor", limit.default_tenor_months if limit else 0)

	def insert(self, *args, **kwargs):
		# a new credit starts with an empty log, whatever the client sent (before_insert writes the first row)
		self.set("status_log", [])
		return super().insert(*args, **kwargs)

	def before_insert(self):
		self.status = lc.DRAFT
		instruments.log(self, "Created", lc.DRAFT)

	def validate(self):
		# draft saves and the submit itself: the lifecycle values are set by on_submit only
		self.status = lc.DRAFT
		instruments.reset_draft_fields(self, (*LIFECYCLE_FIELDS, *SYSTEM_FIELDS))
		facility, limit = instruments.set_facility_details(self, lc.LIMIT_TYPE)
		instruments.validate_bank_account(self)
		instruments.set_exchange_rate(self, self.issue_date)
		self.validate_amount_and_dates()
		self.validate_supplier()
		instruments.validate_margin_and_commission(self, facility, limit)
		if self.charges_account:
			instruments.validate_expense_account(self, self.charges_account, _("Charges Account"))

	def before_submit(self):
		# serialise the instruments of one facility: the limit check must see every earlier one
		facility = instruments.lock_facility(self.facility)
		instruments.validate_facility_open(facility, self.issue_date, _(self.doctype))
		instruments.check_available(facility, lc.LIMIT_TYPE, self.amount, self.currency, _(self.doctype))

	def on_submit(self):
		lc.open_credit(self)
		refresh_utilisation(self.facility)

	def before_update_after_submit(self):
		instruments.validate_protected_fields(self, ("status", *LIFECYCLE_FIELDS))

	def before_cancel(self):
		if self.status == lc.SETTLED:
			frappe.throw(
				_("Letter of Credit {0} is settled and cannot be cancelled.").format(frappe.bold(self.name)),
				title=_("Not Allowed"),
			)

	def on_cancel(self):
		# the margin / commission / charges Journal Entries (before Frappe's check for submitted back links)
		lc.cancel(self)
		refresh_utilisation(self.facility)

	# ------------------------------------------------------------------ validation

	def validate_amount_and_dates(self):
		self.amount = flt(self.amount, self.precision("amount"))
		if self.amount <= 0:
			frappe.throw(_("Amount must be greater than zero."))
		if getdate(self.expiry_date) <= getdate(self.issue_date):
			frappe.throw(_("The Expiry Date must be after the Opening Date."))
		if self.latest_shipment_date and getdate(self.latest_shipment_date) > getdate(self.expiry_date):
			frappe.throw(_("The Latest Shipment Date cannot be after the Expiry Date."))
		if not 0 <= flt(self.tolerance_percent) <= 100:
			frappe.throw(_("{0} must be between 0 and 100.").format(_("Tolerance (%)")))
		if self.lc_type == lc.USANCE:
			if cint(self.usance_days) <= 0:
				frappe.throw(_("Usance Days are required for a Usance Letter of Credit."))
		else:
			self.usance_days = 0

	def validate_supplier(self):
		if not self.supplier or not frappe.db.exists("Supplier", self.supplier):
			frappe.throw(_("Select the Supplier."))
		if not self.purchase_order:
			return
		order = frappe.db.get_value(
			"Purchase Order", self.purchase_order, ["supplier", "company", "docstatus"], as_dict=True
		)
		if (
			not order
			or order.docstatus != 1
			or order.company != self.company
			or order.supplier != self.supplier
		):
			frappe.throw(
				_("Purchase Order {0} must be a submitted order of supplier {1} in company {2}.").format(
					frappe.bold(self.purchase_order), frappe.bold(self.supplier), frappe.bold(self.company)
				)
			)

	# ------------------------------------------------------------------ actions (bank_facilities.lc)
	# POST only: they change data, and Frappe checks the CSRF token only on POST requests.

	@frappe.whitelist(methods=["POST"])
	def amend(
		self,
		amendment_date=None,
		new_amount=None,
		new_expiry_date=None,
		charges=0,
		exchange_rate=None,
		remarks=None,
	):
		return lc.amend(
			self.name,
			amendment_date,
			None if new_amount in (None, "") else flt(new_amount),
			new_expiry_date or None,
			flt(charges),
			flt(exchange_rate) or None,
			remarks,
		)

	@frappe.whitelist(methods=["POST"])
	def receive_documents(
		self, documents_date=None, documents_amount=None, charges=0, exchange_rate=None, remarks=None
	):
		return lc.receive_documents(
			self.name,
			documents_date,
			None if documents_amount in (None, "") else flt(documents_amount),
			flt(charges),
			flt(exchange_rate) or None,
			remarks,
		)

	@frappe.whitelist(methods=["POST"])
	def accept(self, acceptance_date=None, maturity_date=None, remarks=None):
		return lc.accept(self.name, acceptance_date, maturity_date or None, remarks)

	@frappe.whitelist(methods=["POST"])
	def settle(
		self,
		settlement_date=None,
		amount=None,
		exchange_rate=None,
		purchase_invoice=None,
		remarks=None,
		finance_by_trust_receipt=0,
		tr_tenor_months=None,
		tr_first_repayment_date=None,
		tr_interest_rate=None,
	):
		return lc.settle(
			self.name,
			settlement_date,
			None if amount in (None, "") else flt(amount),
			flt(exchange_rate) or None,
			purchase_invoice or None,
			remarks,
			finance_by_trust_receipt=cint(finance_by_trust_receipt),
			tr_tenor_months=cint(tr_tenor_months) or None,
			tr_first_repayment_date=tr_first_repayment_date or None,
			tr_interest_rate=None if tr_interest_rate in (None, "") else flt(tr_interest_rate),
		)

	@frappe.whitelist(methods=["POST"])
	def release_margin(self, release_date=None, exchange_rate=None, remarks=None):
		return lc.release_margin(self.name, release_date, flt(exchange_rate) or None, remarks)


def on_doctype_update():
	frappe.db.add_index("Letter of Credit", ["facility", "status"])
