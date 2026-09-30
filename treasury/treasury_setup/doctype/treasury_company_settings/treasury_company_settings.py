# Copyright (c) 2026, Ahmed Abdelmageed and contributors
# For license information, please see license.txt

import erpnext
import frappe
from erpnext.accounts.doctype.account.account import get_account_currency
from frappe import _
from frappe.model.document import Document
from frappe.model.naming import NamingSeries
from frappe.utils import cint, flt

from treasury.accounting import payment_modes
from treasury.utils.context import elevated

# Roles allowed to generate Modes of Payment from the settings.
GENERATE_ROLES = ("Treasury Manager", "System Manager")
# Bank Transfers: the default company bank account of each transfer method.
TRANSFER_DEFAULT_BANK_FIELDS = {
	"Normal": "default_normal_bank_account",
	"Fawran": "default_fawran_bank_account",
	"SWIFT": "default_swift_bank_account",
}
SWIFT = "SWIFT"
INTERNATIONAL = "International"
# Pay / receive from documents: the default bank of the cheques created from ERPNext documents.
DOCUMENT_DEFAULT_BANK_FIELDS = (
	"default_outgoing_cheque_bank_account",
	"default_incoming_cheque_bank_account",
)

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

		from treasury.treasury_setup.doctype.bank_transfer_fee_rule.bank_transfer_fee_rule import (
			BankTransferFeeRule,
		)
		from treasury.treasury_setup.doctype.bank_transfer_limit.bank_transfer_limit import BankTransferLimit
		from treasury.treasury_setup.doctype.pdc_account_mapping.pdc_account_mapping import PDCAccountMapping
		from treasury.treasury_setup.doctype.treasury_balance_threshold.treasury_balance_threshold import (
			TreasuryBalanceThreshold,
		)
		from treasury.treasury_setup.doctype.treasury_payment_mode_map.treasury_payment_mode_map import (
			TreasuryPaymentModeMap,
		)

		accrue_facility_interest: DF.Check
		auto_select_cheque_book: DF.Check
		balance_alerts_help: DF.HTML | None
		balance_thresholds: DF.Table[TreasuryBalanceThreshold]
		bank_charge_clearance: DF.Literal["On Posting", "On Bank Transaction"]
		bank_charge_voucher: DF.Literal["Payment Entry", "Journal Entry"]
		bank_settlement_voucher: DF.Literal["Payment Entry", "Journal Entry"]
		bounce_charges_account: DF.Link | None
		company: DF.Link
		default_charge_bearer: DF.Literal["OUR", "SHA", "BEN"]
		default_fawran_bank_account: DF.Link | None
		default_incoming_cheque_bank_account: DF.Link | None
		default_installment_count: DF.Int
		default_installment_frequency: DF.Literal[
			"Monthly", "Every 2 Months", "Quarterly", "Semi-Annually", "Yearly", "Weekly"
		]
		default_normal_bank_account: DF.Link | None
		default_outgoing_cheque_bank_account: DF.Link | None
		default_swift_bank_account: DF.Link | None
		default_transfer_method: DF.Literal["Normal", "Fawran", "SWIFT"]
		incoming_bank_posting: DF.Literal["On Actual Clearance", "On Bank Transaction Date", "Manual"]
		incoming_party_posting: DF.Literal["On Receipt", "On Cheque Date", "Manual"]
		incoming_under_collection: DF.Literal["On Deposit", "Disabled"]
		mode_name_template: DF.Data
		on_bounce: DF.Literal["Cancel Payment Entry", "Reverse by Journal Entry"]
		on_low_balance: DF.Literal["Ignore", "Warn", "Block"]
		outgoing_bank_posting: DF.Literal["On Actual Clearance", "On Bank Transaction Date", "Manual"]
		outgoing_party_posting: DF.Literal["On Handover", "On Cheque Date", "On Creation", "Manual"]
		payment_modes: DF.Table[TreasuryPaymentModeMap]
		pdc_accounts: DF.Table[PDCAccountMapping]
		performance_bond_percent: DF.Percent
		refund_fees_on_return: DF.Check
		tender_bond_percent: DF.Percent
		transfer_approval_threshold: DF.Currency
		transfer_completion: DF.Literal["On Bank Confirmation", "On Send for Fawran", "On Send"]
		transfer_fee_rules: DF.Table[BankTransferFeeRule]
		transfer_fees_help: DF.HTML | None
		transfer_limit_action: DF.Literal["Warn", "Block"]
		transfer_limits: DF.Table[BankTransferLimit]
		vouchers_help: DF.HTML | None
	# end: auto-generated types

	def onload(self):
		self.set_onload("unregistered_series", payment_modes.unregistered_series(self))
		# for the form: the confirmation names the default series, the Generate dialog previews the names
		series = payment_modes.payment_entry_series()
		self.set_onload("pe_default_series", series[0] if series else None)
		banks = [row.bank_account for row in self.get("pdc_accounts") if row.bank_account]
		accounts = (
			frappe.get_all(
				"Bank Account", filters={"name": ["in", banks]}, fields=["name", "bank", "account_name"]
			)
			if banks
			else []
		)
		self.set_onload(
			"bank_accounts",
			{
				d.name: {"bank": d.bank or d.account_name or d.name, "account": d.account_name or d.name}
				for d in accounts
			},
		)

	def validate(self):
		self.warn_outgoing_on_creation()
		if self.bounce_charges_account:
			label = _(self.meta.get_label("bounce_charges_account"))
			self.validate_expense_account(self.bounce_charges_account, label)
		self.validate_pdc_accounts()
		self.mode_name_template = (
			self.mode_name_template or ""
		).strip() or payment_modes.DEFAULT_NAME_TEMPLATE
		payment_modes.validate_name_template(self.mode_name_template)
		self.validate_payment_modes()
		self.validate_transfer_settings()
		self.validate_document_settings()
		self.validate_balance_thresholds()

	# ------------------------------------------------------------------ bank balance alerts

	def validate_balance_thresholds(self):
		"""One row per company bank account (with or without PDC Accounts), its currency from the GL account,
		warning not below minimum; negative amounts are allowed (overdraft accounts). Spec 7e §2.3."""
		seen = set()
		for row in self.get("balance_thresholds"):
			if row.bank_account in seen:
				frappe.throw(
					_("Row #{0}: Bank Account {1} already has thresholds.").format(
						row.idx, frappe.bold(row.bank_account)
					)
				)
			seen.add(row.bank_account)
			gl_account = self.validate_bank_account(row)
			row.currency = get_account_currency(gl_account)
			precision = row.precision("minimum_balance")
			row.minimum_balance = flt(row.minimum_balance, precision)
			row.warning_balance = flt(row.warning_balance, precision)
			if row.warning_balance < row.minimum_balance:
				frappe.throw(
					_("Row #{0}: Warning Balance cannot be lower than Minimum Balance.").format(row.idx)
				)

	# ------------------------------------------------------------------ pay / receive from documents

	def validate_document_settings(self):
		"""Default cheque banks (company banks with a PDC Accounts row), installment count, tender and
		performance bond %."""
		from treasury.pdc.batch import MAX_ROWS

		mapped = {row.bank_account for row in self.get("pdc_accounts")}
		for fieldname in DOCUMENT_DEFAULT_BANK_FIELDS:
			bank_account = self.get(fieldname)
			if not bank_account:
				continue
			label = _(self.meta.get_label(fieldname))
			self.validate_bank_account(frappe._dict(idx=0, bank_account=bank_account), label)
			if bank_account not in mapped:
				frappe.throw(
					_("{0} {1} needs a row in PDC Accounts.").format(label, frappe.bold(bank_account))
				)
		if not 1 <= cint(self.default_installment_count) <= MAX_ROWS:
			frappe.throw(_("Installment Cheques (Count) must be between 1 and {0}.").format(MAX_ROWS))
		if not 0 <= flt(self.tender_bond_percent) <= 100:
			frappe.throw(_("Tender Bond % must be between 0 and 100."))
		if not 0 <= flt(self.performance_bond_percent) <= 100:
			frappe.throw(_("Performance Bond % must be between 0 and 100."))

	# ------------------------------------------------------------------ bank transfers

	def validate_transfer_settings(self):
		"""Default banks per method, fee rules (ranges, percentages, SWIFT-only fields, overlaps) and limits."""
		for fieldname in TRANSFER_DEFAULT_BANK_FIELDS.values():
			if self.get(fieldname):
				label = _(self.meta.get_label(fieldname))
				self.validate_bank_account(frappe._dict(idx=0, bank_account=self.get(fieldname)), label)
		for row in self.get("transfer_fee_rules"):
			self.validate_fee_rule(row)
		self.validate_fee_rule_overlaps()
		self.validate_transfer_limits()

	def validate_fee_rule(self, row):
		self.validate_bank_account(row)
		if not row.expense_account:
			# not a grid column: the bank's charges account (else the mandatory check names the field)
			row.expense_account = self.default_fee_expense_account(row.bank_account)
		if row.expense_account:
			label = _(row.meta.get_label("expense_account"))
			self.validate_expense_account(row.expense_account, label, row.idx)
		for fieldname in ("amount_from", "amount_to", "fixed_fee", "min_fee", "max_fee", "correspondent_fee"):
			if flt(row.get(fieldname)) < 0:
				frappe.throw(
					_("Row #{0}: {1} cannot be negative.").format(row.idx, _(row.meta.get_label(fieldname)))
				)
		if flt(row.amount_to) and flt(row.amount_to) < flt(row.amount_from):
			frappe.throw(_("Row #{0}: Amount To must be 0 or not less than Amount From.").format(row.idx))
		if not 0 <= flt(row.fee_percent) <= 100:
			frappe.throw(
				_("Row #{0}: {1} must be between 0 and 100.").format(
					row.idx, _(row.meta.get_label("fee_percent"))
				)
			)
		if flt(row.max_fee) and flt(row.max_fee) < flt(row.min_fee):
			frappe.throw(_("Row #{0}: Max Fee must be 0 or not less than Min Fee.").format(row.idx))
		if row.method != SWIFT:
			if flt(row.correspondent_fee):
				frappe.throw(
					_("Row #{0}: Charge Bearer and Correspondent Fee apply to SWIFT only.").format(row.idx)
				)
			if row.scope == INTERNATIONAL:
				frappe.throw(_("Row #{0}: Scope International is only for SWIFT.").format(row.idx))
			row.charge_bearer = None
			row.correspondent_fee = 0

	def default_fee_expense_account(self, bank_account: str | None) -> str | None:
		"""The Bank Charges Account of the bank's PDC Accounts row, else the Bounce Charges Account."""
		for row in self.get("pdc_accounts"):
			if row.bank_account == bank_account and row.bank_charges_account:
				return row.bank_charges_account
		return self.bounce_charges_account or None

	def validate_fee_rule_overlaps(self):
		"""Two rules of the same bank, method, direction, scope and charge bearer may not share an amount."""
		rows = list(self.get("transfer_fee_rules"))
		for index, row in enumerate(rows):
			for other in rows[index + 1 :]:
				key = (row.bank_account, row.method, row.direction, row.scope, row.charge_bearer or "")
				other_key = (
					other.bank_account,
					other.method,
					other.direction,
					other.scope,
					other.charge_bearer or "",
				)
				if key == other_key and _ranges_overlap(row, other):
					frappe.throw(
						_("Rows #{0} and #{1} overlap for Bank Account {2}, {3}.").format(
							row.idx,
							other.idx,
							frappe.bold(row.bank_account),
							_(row.method, context=row.doctype),
						)
					)

	def validate_transfer_limits(self):
		seen = set()
		for row in self.get("transfer_limits"):
			key = (row.bank_account, row.method)
			if key in seen:
				frappe.throw(
					_("Row #{0}: Bank Account {1} already has a limit for {2}.").format(
						row.idx, frappe.bold(row.bank_account), _(row.method, context=row.doctype)
					)
				)
			seen.add(key)
			self.validate_bank_account(row)
			for fieldname in ("per_transaction_limit", "daily_limit"):
				if flt(row.get(fieldname)) < 0:
					frappe.throw(
						_("Row #{0}: {1} cannot be negative.").format(
							row.idx, _(row.meta.get_label(fieldname))
						)
					)

	def validate_payment_modes(self):
		seen = set()
		unregistered = []
		registered = set(payment_modes.payment_entry_series())
		for row in self.get("payment_modes"):
			key = (row.bank_account, row.method)
			if key in seen:
				frappe.throw(
					_("Row #{0}: Bank Account {1} already has a row for {2}.").format(
						row.idx, frappe.bold(row.bank_account), _(row.method, context=row.doctype)
					)
				)
			seen.add(key)
			bank_gl_account = self.validate_bank_account(row)
			self.validate_mode_of_payment(row, bank_gl_account)
			row.pe_naming_series = (row.pe_naming_series or "").strip() or None
			if row.pe_naming_series:
				NamingSeries(row.pe_naming_series).validate()
				if row.pe_naming_series not in registered and row.pe_naming_series not in unregistered:
					unregistered.append(row.pe_naming_series)
		for series in unregistered:
			frappe.msgprint(
				_(
					'Series {0} is not a Payment Entry series yet. A System Manager must click "Add Series to Payment Entry".'
				).format(frappe.bold(series)),
				title=_("Warning"),
				indicator="orange",
			)

	def validate_mode_of_payment(self, row, bank_gl_account: str):
		if not frappe.db.get_value("Mode of Payment", row.mode_of_payment, "enabled"):
			frappe.throw(
				_("Row #{0}: Mode of Payment {1} is disabled.").format(
					row.idx, frappe.bold(row.mode_of_payment)
				)
			)
		default_account = frappe.db.get_value(
			"Mode of Payment Account",
			{"parenttype": "Mode of Payment", "parent": row.mode_of_payment, "company": self.company},
			"default_account",
		)
		if default_account and default_account != bank_gl_account:
			frappe.throw(
				_(
					"Row #{0}: Mode of Payment {1} uses account {2} in company {3}, not the account {4} of Bank Account {5}."
				).format(
					row.idx,
					frappe.bold(row.mode_of_payment),
					frappe.bold(default_account),
					self.company,
					frappe.bold(bank_gl_account),
					frappe.bold(row.bank_account),
				)
			)

	# ------------------------------------------------------------------ actions
	# POST only: they change data, and Frappe checks the CSRF token only on POST requests. They work on the
	# saved settings, never on the (possibly edited, unvalidated) document the client sent.

	@frappe.whitelist(methods=["POST"])
	def generate_modes_of_payment(self, methods=None, bank_accounts=None):
		"""Create the missing Modes of Payment of the banks and fill the missing map rows (see
		treasury.accounting.payment_modes.generate); existing map rows are never changed, an existing mode of
		the same name without an account for the company is given one (see README)."""
		doc = self.saved_for_write()
		if not set(GENERATE_ROLES) & set(frappe.get_roles()):
			frappe.throw(
				_("This action requires one of the roles: {0}.").format(
					", ".join(_(r) for r in GENERATE_ROLES)
				),
				frappe.PermissionError,
				title=_("Not Permitted"),
			)
		methods = _parse_list(methods)
		bank_accounts = _parse_list(bank_accounts)
		# not given: the defaults; given but empty: an error (never silently the defaults)
		if methods == [] or bank_accounts == []:
			frappe.throw(_("Select at least one method and one bank account."))
		for method in methods or ():
			if method not in payment_modes.METHODS:
				frappe.throw(_("Unknown method {0}.").format(frappe.bold(method)))
		with elevated():
			result = payment_modes.generate(doc, methods, bank_accounts)
		# the user's write permission and role were checked above
		doc.flags.ignore_permissions = True
		doc.save()
		return result

	@frappe.whitelist(methods=["POST"])
	def add_series_to_payment_entry(self, confirm=0):
		"""Register the map's series as Payment Entry series (Document Naming Settings), after confirmation."""
		frappe.only_for("System Manager")
		doc = self.saved_for_write()
		if not cint(confirm):
			frappe.throw(_("Confirm to add the series to Payment Entry."))
		added = payment_modes.add_series_to_payment_entry(doc)
		if not added:
			frappe.msgprint(_("All series are already Payment Entry series."))
		return added

	@frappe.whitelist()
	def get_threshold_banks(self) -> list[dict]:
		"""Read only (design 7e P3): the company's enabled bank accounts with a GL account, each with the
		currency of its GL account ([{bank_account, currency}], name order), for "Add All Bank Accounts" and
		the currency preview of the thresholds grid. Treasury users need not read Bank Account or Account."""
		doc = frappe.get_doc(self.doctype, self.name)
		doc.check_permission("read")
		banks = frappe.get_all(
			"Bank Account",
			filters={"company": doc.company, "is_company_account": 1, "disabled": 0, "account": ["is", "set"]},
			fields=["name", "account"],
			order_by="name asc",
		)
		currencies = {}
		if banks:
			accounts = frappe.get_all(
				"Account",
				filters={"name": ["in", [b.account for b in banks]]},
				fields=["name", "account_currency"],
				as_list=True,
			)
			currencies = dict(accounts)
		return [{"bank_account": b.name, "currency": currencies.get(b.account)} for b in banks]

	def saved_for_write(self) -> "TreasuryCompanySettings":
		"""The saved settings of this company, after checking the user's write permission on them."""
		doc = frappe.get_doc(self.doctype, self.name)
		doc.check_permission("write")
		return doc

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

	def validate_bank_account(self, row, label: str | None = None) -> str:
		"""A company bank account of the company with a GL account of type Bank. `row.idx` 0: a field of the
		settings themselves (`label`), not a table row."""
		bank_account = frappe.db.get_value(
			"Bank Account",
			row.bank_account,
			["company", "is_company_account", "account"],
			as_dict=True,
		)
		name, company = frappe.bold(row.bank_account), frappe.bold(self.company)
		label = label or _("Bank Account", context="Treasury")
		if not bank_account or not bank_account.is_company_account or bank_account.company != self.company:
			if row.idx:
				frappe.throw(
					_("Row #{0}: Bank Account {1} must be a company bank account of {2}.").format(
						row.idx, name, company
					)
				)
			frappe.throw(_("{0} {1} must be a company bank account of {2}.").format(label, name, company))
		if (
			not bank_account.account
			or frappe.get_cached_value("Account", bank_account.account, "account_type") != "Bank"
		):
			if row.idx:
				frappe.throw(
					_("Row #{0}: The GL account of Bank Account {1} must have Account Type 'Bank'.").format(
						row.idx, name
					)
				)
			frappe.throw(_("The GL account of {0} {1} must have Account Type 'Bank'.").format(label, name))
		return bank_account.account

	def validate_expense_account(self, account: str, label: str, idx: int | None = None):
		"""Charges accounts: Expense root type, in the company currency."""
		validate_expense_account(self.company, account, label, idx)

	def validate_account_basics(self, account: str, label: str, idx: int | None = None) -> frappe._dict:
		return validate_account_basics(self.company, account, label, idx)

	@staticmethod
	def throw_for_row(idx: int | None, row_message: str, message: str, *args):
		"""Throw the whole-sentence message that fits: with the row number (child table) or without."""
		throw_for_row(idx, row_message, message, *args)


def validate_expense_account(company: str, account: str, label: str, idx: int | None = None):
	"""Charges / fee accounts: a ledger account of `company`, Expense root type, in the company currency."""
	details = validate_account_basics(company, account, label, idx)
	if details.root_type != "Expense":
		throw_for_row(
			idx,
			_("Row #{0}: {1} {2} must have Root Type '{3}'."),
			_("{0} {1} must have Root Type '{2}'."),
			label,
			frappe.bold(account),
			_("Expense"),
		)
	company_currency = erpnext.get_company_currency(company)
	if details.account_currency != company_currency:
		throw_for_row(
			idx,
			_("Row #{0}: {1} {2} must be in the company currency {3}."),
			_("{0} {1} must be in the company currency {2}."),
			label,
			frappe.bold(account),
			company_currency,
		)


def validate_account_basics(company: str, account: str, label: str, idx: int | None = None) -> frappe._dict:
	details = frappe.db.get_value(
		"Account",
		account,
		["company", "is_group", "account_type", "root_type", "account_currency"],
		as_dict=True,
	)
	if not details or details.company != company:
		throw_for_row(
			idx,
			_("Row #{0}: {1} {2} does not belong to company {3}."),
			_("{0} {1} does not belong to company {2}."),
			label,
			frappe.bold(account),
			frappe.bold(company),
		)
	if details.is_group:
		throw_for_row(
			idx,
			_("Row #{0}: {1} {2} cannot be a group account."),
			_("{0} {1} cannot be a group account."),
			label,
			frappe.bold(account),
		)
	return details


def throw_for_row(idx: int | None, row_message: str, message: str, *args):
	"""Throw the whole-sentence message that fits: with the row number (child table) or without."""
	frappe.throw(row_message.format(idx, *args) if idx else message.format(*args))


def _ranges_overlap(row, other) -> bool:
	"""[amount_from, amount_to] ranges, amount_to 0 = no upper limit."""
	first_end = flt(row.amount_to) or float("inf")
	second_end = flt(other.amount_to) or float("inf")
	return flt(row.amount_from) <= second_end and flt(other.amount_from) <= first_end


def _parse_list(value) -> list[str] | None:
	"""A list argument of a whitelisted method (a list or its JSON); None when not given."""
	if value in (None, ""):
		return None
	value = frappe.parse_json(value)
	if isinstance(value, str):
		value = [value]
	if not isinstance(value, list | tuple):
		frappe.throw(_("A list is expected."))
	return [str(v) for v in value if v]
