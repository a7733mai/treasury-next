# Copyright (c) 2026, Ahmed Abdelmageed and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt

from treasury.bank_facilities import discounting, instruments
from treasury.bank_facilities.utilisation import refresh_utilisation
from treasury.pdc import lifecycle

# Written only by treasury.bank_facilities.discounting (on submit / cancel, or per cheque).
LIFECYCLE_FIELDS = ("outstanding_amount", "journal_entry")
ITEM_LIFECYCLE_FIELDS = ("closed_date", "closing_journal")


class ChequeDiscounting(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from treasury.bank_facilities.doctype.bank_instrument_status_log.bank_instrument_status_log import (
			BankInstrumentStatusLog,
		)
		from treasury.bank_facilities.doctype.cheque_discounting_item.cheque_discounting_item import (
			ChequeDiscountingItem,
		)

		amended_from: DF.Link | None
		bank: DF.Link | None
		bank_account: DF.Link
		commission: DF.Currency
		company: DF.Link | None
		currency: DF.Link | None
		day_count: DF.Literal["", "Actual/365", "Actual/360"]
		discount_charges: DF.Currency
		discount_date: DF.Date
		discount_expense_account: DF.Link | None
		discount_rate: DF.Percent
		exchange_rate: DF.Float
		facility: DF.Link
		items: DF.Table[ChequeDiscountingItem]
		journal_entry: DF.Link | None
		liability_account: DF.Link | None
		naming_series: DF.Literal["CHD-.YYYY.-.#####"]
		net_proceeds: DF.Currency
		outstanding_amount: DF.Currency
		remarks: DF.SmallText | None
		status: DF.Literal["Draft", "Discounted", "Partially Settled", "Settled", "Cancelled"]
		status_log: DF.Table[BankInstrumentStatusLog]
		total_amount: DF.Currency
		total_charges: DF.Currency
	# end: auto-generated types

	# ------------------------------------------------------------------ hooks

	def insert(self, *args, **kwargs):
		# a new batch starts with an empty log, whatever the client sent (before_insert writes the first row)
		self.set("status_log", [])
		return super().insert(*args, **kwargs)

	def before_insert(self):
		self.status = discounting.DRAFT
		instruments.log(self, "Created", discounting.DRAFT)

	def validate(self):
		# draft saves and the submit itself: the lifecycle values are set by on_submit only
		self.status = discounting.DRAFT
		instruments.reset_draft_fields(self, LIFECYCLE_FIELDS)
		facility, limit = instruments.set_facility_details(self, discounting.LIMIT_TYPE)
		instruments.validate_bank_account(self)
		instruments.set_exchange_rate(self, self.discount_date)
		self.set_terms(facility, limit)
		self.validate_items()
		self.set_totals(limit)
		self.validate_accounts()

	def before_submit(self):
		# serialise the instruments of one facility: the limit check must see every earlier one
		facility = instruments.lock_facility(self.facility)
		instruments.validate_facility_open(facility, self.discount_date, _(self.doctype))
		instruments.check_available(
			facility, discounting.LIMIT_TYPE, self.total_amount, self.currency, _(self.doctype)
		)

	def on_submit(self):
		discounting.discount_batch(self)
		refresh_utilisation(self.facility)

	def before_update_after_submit(self):
		instruments.validate_protected_fields(self, ("status", *LIFECYCLE_FIELDS))
		before = self.get_doc_before_save()
		if before and _items_key(before) != _items_key(self):
			frappe.throw(_("The cheques of a submitted Cheque Discounting cannot be changed."), title=_("Not Allowed"))

	def before_cancel(self):
		discounting.validate_cancel(self)

	def on_cancel(self):
		# the cheques back to Received and the Journal Entry cancelled (before Frappe's back link check)
		discounting.cancel_batch(self)
		refresh_utilisation(self.facility)

	# ------------------------------------------------------------------ validation

	def set_terms(self, facility, limit):
		if not limit.liability_account:
			frappe.throw(
				_(
					"Set the Liability Account (discounted cheques) of the {0} sub-limit of Bank Facility {1}."
				).format(_(discounting.LIMIT_TYPE, context="Bank Facility"), frappe.bold(facility.name)),
				title=_("Missing Settings"),
			)
		self.liability_account = limit.liability_account
		if self.get("discount_rate") in (None, ""):
			self.discount_rate = flt(limit.interest_rate)
		if flt(self.discount_rate) < 0:
			frappe.throw(_("The discount rate cannot be negative."))
		self.day_count = self.day_count or limit.day_count or "Actual/365"
		if not self.discount_expense_account:
			self.discount_expense_account = facility.interest_expense_account

	def validate_items(self):
		if not self.items:
			frappe.throw(_("Add at least one cheque."))
		seen = set()
		for row in self.items:
			if row.pdc_cheque in seen:
				frappe.throw(_("Row #{0}: Cheque {1} appears more than once.").format(row.idx, frappe.bold(row.pdc_cheque)))
			seen.add(row.pdc_cheque)
			row.status = discounting.ITEM_DISCOUNTED
			for fieldname in ITEM_LIFECYCLE_FIELDS:
				row.set(fieldname, None)
			cheque = frappe.get_doc("PDC Cheque", row.pdc_cheque)
			if cheque.company != self.company:
				frappe.throw(
					_("Row #{0}: Cheque {1} does not belong to company {2}.").format(
						row.idx, frappe.bold(cheque.name), frappe.bold(self.company)
					)
				)
			if cheque.currency != self.currency:
				frappe.throw(
					_("Row #{0}: Cheque {1} is in {2}, but the facility is in {3}.").format(
						row.idx, frappe.bold(cheque.name), cheque.currency, self.currency
					)
				)
			if error := lifecycle.discount_error(cheque, self.discount_date):
				frappe.throw(_("Row #{0}: {1}").format(row.idx, error))
			row.cheque_no = cheque.cheque_no
			row.party_type = cheque.party_type
			row.party = cheque.party
			row.party_name = cheque.party_name
			row.due_date = cheque.effective_due_date
			row.days = discounting.days_to_due(self.discount_date, cheque.effective_due_date)
			row.amount = flt(cheque.amount, self.precision("total_amount"))

	def set_totals(self, limit):
		precision = self.precision("total_amount")
		for row in self.items:
			row.discount_charges = discounting.row_charges(
				row.amount, self.discount_rate, row.days, self.day_count, precision
			)
		self.total_amount = flt(sum(flt(row.amount) for row in self.items), precision)
		self.discount_charges = flt(sum(flt(row.discount_charges) for row in self.items), precision)
		if self.get("commission") in (None, ""):
			self.commission = flt(self.total_amount * flt(limit.commission_rate) / 100, precision)
		self.commission = flt(self.commission, precision)
		if self.commission < 0:
			frappe.throw(_("The commission cannot be negative."))
		self.total_charges = flt(self.discount_charges + self.commission, precision)
		self.net_proceeds = flt(self.total_amount - self.total_charges, precision)
		if self.net_proceeds <= 0:
			frappe.throw(_("The discount charges and the commission leave no net proceeds."))

	def validate_accounts(self):
		if flt(self.total_charges):
			if not self.discount_expense_account:
				frappe.throw(_("Discount Expense Account is required for the charges."))
		if self.discount_expense_account:
			instruments.validate_expense_account(self, self.discount_expense_account, _("Discount Expense Account"))

	# ------------------------------------------------------------------ actions

	@frappe.whitelist(methods=["POST"])
	def settle_cheques(self, cheques=None, settlement_date=None):
		"""Settle discounted cheques paid by their drawers (default: every open one due by the date)."""
		cheques = frappe.parse_json(cheques) if cheques else None
		if cheques is not None and not isinstance(cheques, list):
			frappe.throw(_("Cheques must be given as a list."))
		return discounting.settle_cheques(self.name, cheques, settlement_date)


def _items_key(doc) -> list[tuple]:
	return [(row.pdc_cheque, flt(row.amount, 9), flt(row.discount_charges, 9)) for row in doc.items]


def on_doctype_update():
	frappe.db.add_index("Cheque Discounting", ["facility", "status"])
