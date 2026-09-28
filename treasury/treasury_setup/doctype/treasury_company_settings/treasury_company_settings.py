# Copyright (c) 2026, Ahmed Abdelmageed and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

import erpnext

# PDC accounts must be of type "Bank" so that Payment Entry accepts them as the bank side,
# and must sit on the correct side of the balance sheet.
PDC_ACCOUNT_RULES = {
	"pdc_payable_account": "Liability",
	"pdc_receivable_account": "Asset",
	"under_collection_account": "Asset",
}


class TreasuryCompanySettings(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from treasury.treasury_setup.doctype.pdc_account_mapping.pdc_account_mapping import PDCAccountMapping

		bounce_charges_account: DF.Link | None
		company: DF.Link
		incoming_bank_posting: DF.Literal["On Actual Clearance", "On Bank Transaction Date", "Manual"]
		incoming_party_posting: DF.Literal["On Receipt", "On Cheque Date", "Manual"]
		incoming_under_collection: DF.Literal["On Deposit", "Disabled"]
		on_bounce: DF.Literal["Cancel Payment Entry", "Reverse by Journal Entry"]
		outgoing_bank_posting: DF.Literal["On Actual Clearance", "On Bank Transaction Date", "Manual"]
		outgoing_party_posting: DF.Literal["On Handover", "On Cheque Date", "On Creation", "Manual"]
		pdc_accounts: DF.Table[PDCAccountMapping]
	# end: auto-generated types

	def validate(self):
		self.warn_outgoing_on_creation()
		if self.bounce_charges_account:
			label = _(self.meta.get_label("bounce_charges_account"))
			self.validate_expense_account(self.bounce_charges_account, label)
		self.validate_pdc_accounts()

	def warn_outgoing_on_creation(self):
		if self.outgoing_party_posting == "On Creation":
			frappe.msgprint(
				_(
					"Outgoing cheques will be posted to the party as soon as they are created, even if they are never handed over."
				),
				title=_("Warning"),
				indicator="orange",
			)

	def validate_pdc_accounts(self):
		seen = set()
		for row in self.pdc_accounts:
			if row.bank_account in seen:
				frappe.throw(
					_("Row #{0}: Bank Account {1} appears more than once.").format(
						row.idx, frappe.bold(row.bank_account)
					)
				)
			seen.add(row.bank_account)
			bank_gl_account = self.validate_bank_account(row)
			bank_currency = frappe.get_cached_value("Account", bank_gl_account, "account_currency")
			self.validate_under_collection_required(row)

			for fieldname, root_type in PDC_ACCOUNT_RULES.items():
				account = row.get(fieldname)
				if not account:
					continue
				label = _(row.meta.get_label(fieldname))
				details = self.validate_account_basics(account, label, row.idx)
				if details.account_type != "Bank":
					frappe.throw(
						_("Row #{0}: {1} {2} must have Account Type 'Bank'.").format(
							row.idx, label, frappe.bold(account)
						)
					)
				if details.root_type != root_type:
					frappe.throw(
						_("Row #{0}: {1} {2} must have Root Type '{3}'.").format(
							row.idx, label, frappe.bold(account), _(root_type)
						)
					)
				if bank_currency and details.account_currency != bank_currency:
					frappe.throw(
						_("Row #{0}: {1} {2} must be in the bank account currency {3}.").format(
							row.idx, label, frappe.bold(account), bank_currency
						)
					)

			if row.bank_charges_account:
				label = _(row.meta.get_label("bank_charges_account"))
				self.validate_expense_account(row.bank_charges_account, label, row.idx)

	def validate_under_collection_required(self, row):
		if self.incoming_under_collection == "On Deposit" and not row.under_collection_account:
			frappe.throw(
				_("Row #{0}: {1} is required when {2} is '{3}'.").format(
					row.idx,
					_(row.meta.get_label("under_collection_account")),
					_(self.meta.get_label("incoming_under_collection")),
					_(self.incoming_under_collection),
				)
			)

	def validate_bank_account(self, row) -> str:
		bank_account = frappe.db.get_value(
			"Bank Account",
			row.bank_account,
			["company", "is_company_account", "account"],
			as_dict=True,
		)
		if not bank_account or not bank_account.is_company_account or bank_account.company != self.company:
			frappe.throw(
				_("Row #{0}: Bank Account {1} must be a company bank account of {2}.").format(
					row.idx, frappe.bold(row.bank_account), frappe.bold(self.company)
				)
			)
		if (
			not bank_account.account
			or frappe.get_cached_value("Account", bank_account.account, "account_type") != "Bank"
		):
			frappe.throw(
				_("Row #{0}: The GL account of Bank Account {1} must have Account Type 'Bank'.").format(
					row.idx, frappe.bold(row.bank_account)
				)
			)
		return bank_account.account

	def validate_expense_account(self, account: str, label: str, idx: int | None = None):
		"""Charges accounts: Expense root type, in the company currency."""
		details = self.validate_account_basics(account, label, idx)
		if details.root_type != "Expense":
			self.throw_for_row(
				idx,
				_("Row #{0}: {1} {2} must have Root Type '{3}'."),
				_("{0} {1} must have Root Type '{2}'."),
				label,
				frappe.bold(account),
				_("Expense"),
			)
		company_currency = erpnext.get_company_currency(self.company)
		if details.account_currency != company_currency:
			self.throw_for_row(
				idx,
				_("Row #{0}: {1} {2} must be in the company currency {3}."),
				_("{0} {1} must be in the company currency {2}."),
				label,
				frappe.bold(account),
				company_currency,
			)

	def validate_account_basics(self, account: str, label: str, idx: int | None = None) -> frappe._dict:
		details = frappe.db.get_value(
			"Account",
			account,
			["company", "is_group", "account_type", "root_type", "account_currency"],
			as_dict=True,
		)
		if not details or details.company != self.company:
			self.throw_for_row(
				idx,
				_("Row #{0}: {1} {2} does not belong to company {3}."),
				_("{0} {1} does not belong to company {2}."),
				label,
				frappe.bold(account),
				frappe.bold(self.company),
			)
		if details.is_group:
			self.throw_for_row(
				idx,
				_("Row #{0}: {1} {2} cannot be a group account."),
				_("{0} {1} cannot be a group account."),
				label,
				frappe.bold(account),
			)
		return details

	@staticmethod
	def throw_for_row(idx: int | None, row_message: str, message: str, *args):
		"""Throw the whole-sentence message that fits: with the row number (child table) or without."""
		frappe.throw(row_message.format(idx, *args) if idx else message.format(*args))
