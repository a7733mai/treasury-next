# Copyright (c) 2026, Ahmed Abdelmageed and contributors
# For license information, please see license.txt

import erpnext
import frappe
from erpnext.accounts.doctype.account.account import get_account_currency
from erpnext.accounts.doctype.bank_account.bank_account import get_party_bank_account
from frappe import _
from frappe.contacts.doctype.contact.contact import get_default_contact
from frappe.model.document import Document
from frappe.utils import cint, flt, fmt_money

from treasury.accounting import dimensions, posting
from treasury.bank_facilities import instruments
from treasury.bank_transfers import fees, lifecycle
from treasury.treasury_setup.doctype.treasury_company_settings.treasury_company_settings import (
	TRANSFER_DEFAULT_BANK_FIELDS,
	validate_expense_account,
)
from treasury.utils import references, voucher_summary
from treasury.utils.party import get_party_name, validate_party_type

# Written only by treasury.bank_transfers.lifecycle (on submit, or by the actions with `treasury_lifecycle`).
LIFECYCLE_FIELDS = (
	"status",
	"accounting_status",
	"payment_entry",
	"reversal_payment_entry",
	"reversal_journal",
	"completion_date",
	"bank_transaction_date",
	"return_date",
	"return_reason",
	"base_fee_amount",
	"fees_refunded",
)
# Allowed on submit, changed only through Send Now / Reschedule.
LIFECYCLE_ON_SUBMIT_EDITABLE = ("execution_date",)
DRAFT_RESET_FIELDS = tuple(f for f in LIFECYCLE_FIELDS if f not in ("status", "accounting_status"))
PARTY_TYPES = {lifecycle.PARTY_PAYMENT: "Supplier", lifecycle.PARTY_RECEIPT: "Customer"}
# Party details defaulted from the party: dropped when the party changes (unless chosen for the new one).
PARTY_DETAIL_FIELDS = ("party_bank_account", "contact_person")
BENEFICIARY_FIELDS = ("beneficiary_iban", "beneficiary_bank", "beneficiary_swift")
# A Bank Reference may be entered after submit while the bank has not confirmed the transfer yet.
BANK_REFERENCE_STATUSES = (lifecycle.SCHEDULED, lifecycle.SENT)


class BankTransfer(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from treasury.bank_transfers.doctype.bank_transfer_status_log.bank_transfer_status_log import (
			BankTransferStatusLog,
		)
		from treasury.pdc.doctype.pdc_cheque_reference.pdc_cheque_reference import PDCChequeReference

		accounting_status: DF.Literal["Not Posted", "Posted", "Reversed"]
		amended_from: DF.Link | None
		amount: DF.Currency
		bank: DF.Link | None
		bank_account: DF.Link
		bank_amount: DF.Currency
		bank_reference: DF.Data | None
		bank_transaction_date: DF.Date | None
		base_amount: DF.Currency
		base_fee_amount: DF.Currency
		beneficiary_bank: DF.Link | None
		beneficiary_iban: DF.Data | None
		beneficiary_swift: DF.Data | None
		charge_bearer: DF.Literal["OUR", "SHA", "BEN"]
		company: DF.Link
		completion_date: DF.Date | None
		computed_fee: DF.Currency
		contact_person: DF.Link | None
		correspondent_fee: DF.Currency
		cost_center: DF.Link | None
		currency: DF.Link | None
		exchange_rate: DF.Float
		execution_date: DF.Date
		fee_account: DF.Link | None
		fee_amount: DF.Currency
		fee_basis: DF.SmallText | None
		fee_override_reason: DF.SmallText | None
		fee_rule: DF.Data | None
		fees_refunded: DF.Check
		method: DF.Literal["Normal", "Fawran", "SWIFT"]
		mode_of_payment: DF.Link | None
		naming_series: DF.Literal["BT-.YYYY.-.#####"]
		override_fee: DF.Check
		party: DF.DynamicLink | None
		party_bank_account: DF.Link | None
		party_name: DF.Data | None
		party_type: DF.Link | None
		payment_entry: DF.Link | None
		project: DF.Link | None
		received_amount: DF.Currency
		references: DF.Table[PDCChequeReference]
		remarks: DF.SmallText | None
		request_date: DF.Date
		return_date: DF.Date | None
		return_reason: DF.SmallText | None
		reversal_journal: DF.Link | None
		reversal_payment_entry: DF.Link | None
		scope: DF.Literal["Same Bank", "Other Bank", "International"]
		status: DF.Literal["Draft", "Scheduled", "Sent", "Completed", "Returned", "Cancelled"]
		status_log: DF.Table[BankTransferStatusLog]
		title: DF.Data | None
		to_bank: DF.Link | None
		to_bank_account: DF.Link | None
		to_currency: DF.Link | None
		transfer_type: DF.Literal["Party Payment", "Party Receipt", "Internal Transfer"]
		vouchers_html: DF.HTML | None
	# end: auto-generated types

	# ------------------------------------------------------------------ hooks

	def onload(self):
		if not self.is_new():
			self.set_onload("voucher_summary", voucher_summary.render(self.doctype, [self.name]))
			self.set_onload("payment_entries", voucher_summary.payment_entries(self.doctype, [self.name]))
		self.set_onload("transfer_settings", transfer_settings(self.company))
		self.set_onload("is_manager", bool(set(lifecycle.MANAGER_ROLES) & set(frappe.get_roles())))

	def insert(self, *args, **kwargs):
		# a new transfer starts with an empty log, whatever the client sent (before_insert writes the first row)
		self.set("status_log", [])
		return super().insert(*args, **kwargs)

	def before_insert(self):
		self.status = lifecycle.DRAFT
		self.accounting_status = lifecycle.NOT_POSTED
		lifecycle.log(self, "Created", lifecycle.DRAFT, lifecycle.NOT_POSTED)

	def validate(self):
		# draft saves and the submit itself: the lifecycle values are set by on_submit only
		self.status = lifecycle.DRAFT
		self.accounting_status = lifecycle.NOT_POSTED
		instruments.reset_draft_fields(self, DRAFT_RESET_FIELDS)
		self.fees_refunded = 0
		self.set_defaults()
		self.validate_bank_accounts()
		self.validate_party()
		self.validate_amounts()
		self.set_fee()
		self.log_fee_override()
		self.set_bank_amount()
		self.validate_references()
		self.validate_accounting_details()
		for message in fees.check_limits(self):
			frappe.msgprint(message, title=_("Transfer Limit Exceeded"), indicator="orange")
		self.title = self.party_name or f"{self.bank_account} → {self.to_bank_account}"

	def before_submit(self):
		lifecycle.check_approval(self)
		if fees.get_limit(self.company, self.bank_account, self.method):
			# serialise the submits on one bank account: the daily limit sees every earlier transfer
			frappe.db.get_value("Bank Account", self.bank_account, "name", for_update=True)
		lifecycle.check_limits_on_submit(self)
		if self.transfer_type == lifecycle.PARTY_PAYMENT:
			if self.method == fees.SWIFT:
				if not self.beneficiary_swift:
					frappe.throw(_("Beneficiary SWIFT / BIC is required for a SWIFT transfer."))
			elif not (self.beneficiary_iban or self.party_bank_account):
				frappe.throw(_("Beneficiary IBAN or Bank Account is required."))

	def on_submit(self):
		lifecycle.on_submit(self)

	def before_update_after_submit(self):
		instruments.validate_protected_fields(self, (*LIFECYCLE_FIELDS, *LIFECYCLE_ON_SUBMIT_EDITABLE))
		before = self.get_doc_before_save()
		if (
			before
			and not self.flags.treasury_lifecycle
			and (before.bank_reference or "") != (self.bank_reference or "")
			and self.status not in BANK_REFERENCE_STATUSES
		):
			frappe.throw(
				_("Bank Reference cannot be changed while the transfer is {0}.").format(
					_(self.status, context=self.doctype)
				),
				title=_("Not Allowed"),
			)

	def before_cancel(self):
		lifecycle.before_cancel(self)

	def on_cancel(self):
		lifecycle.on_cancel(self)

	# ------------------------------------------------------------------ validation

	def set_defaults(self):
		"""The company's default method, its bank account for the method, the party type of the type and the
		default charge bearer of a SWIFT transfer (the settings must exist: they hold the fee rules)."""
		settings = posting.get_company_settings(self.company)
		if not self.method:
			self.method = settings.default_transfer_method or "Normal"
		if not self.bank_account:
			self.bank_account = settings.get(TRANSFER_DEFAULT_BANK_FIELDS.get(self.method))
		if self.transfer_type in PARTY_TYPES and not self.party_type:
			self.party_type = PARTY_TYPES[self.transfer_type]
		if not self.charge_bearer:
			self.charge_bearer = settings.default_charge_bearer or fees.SHA

	def validate_bank_accounts(self):
		if not self.bank_account:
			frappe.throw(_("Bank Account is required."))
		self.bank, self.currency = self._company_bank(self.bank_account)
		if self.transfer_type != lifecycle.INTERNAL:
			self.to_bank_account = self.to_bank = self.to_currency = None
			return
		if not self.to_bank_account:
			frappe.throw(_("The target Bank Account is required for an Internal Transfer."))
		if self.to_bank_account == self.bank_account:
			frappe.throw(_("The source and target Bank Accounts must be different."))
		self.to_bank, self.to_currency = self._company_bank(self.to_bank_account)

	def _company_bank(self, bank_account: str) -> tuple[str | None, str]:
		"""(Bank, currency of the GL account) of an enabled company bank account of the company."""
		details = frappe.db.get_value(
			"Bank Account",
			bank_account,
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
					frappe.bold(bank_account), frappe.bold(self.company)
				)
			)
		return details.bank, get_account_currency(details.account)

	def validate_party(self):
		"""Any ERPNext Party Type; the beneficiary's bank details default from its bank account."""
		if self.transfer_type == lifecycle.INTERNAL:
			for fieldname in ("party_type", "party", "party_name", *PARTY_DETAIL_FIELDS, *BENEFICIARY_FIELDS):
				self.set(fieldname, None)
			self.set("references", [])
			return
		validate_party_type(self.party_type)
		if not self.party:
			frappe.throw(_("Party is required."))
		if not frappe.db.exists(self.party_type, self.party):
			frappe.throw(_("{0} {1} does not exist").format(_(self.party_type), self.party))
		self.party_name = get_party_name(self.party_type, self.party)
		# before the fee: the beneficiary's bank decides the scope
		self._drop_stale_party_details()
		if not self.party_bank_account:
			self.party_bank_account = get_party_bank_account(self.party_type, self.party)
		if not self.contact_person and self.party_type != "Employee":
			self.contact_person = get_default_contact(self.party_type, self.party)
		self._validate_party_bank_account()

	def validate_amounts(self):
		self.amount = flt(self.amount, self.precision("amount"))
		if self.amount <= 0:
			frappe.throw(_("Amount must be greater than zero."))
		self.set_exchange_rate()
		if self.transfer_type == lifecycle.INTERNAL:
			if self.to_currency == self.currency:
				self.received_amount = self.amount
			else:
				self.received_amount = flt(self.received_amount, self.precision("received_amount"))
				if self.received_amount <= 0:
					frappe.throw(
						_("Received Amount is required for a transfer between {0} and {1}.").format(
							self.currency, self.to_currency
						)
					)
		else:
			self.received_amount = 0
		self.base_amount = flt(self.amount * flt(self.exchange_rate), self.precision("base_amount"))

	def set_exchange_rate(self):
		"""Bank currency → company currency: 1 for the company currency, else the user's rate, else the rate
		on the execution date (a rate left over from another bank currency is not kept)."""
		if self.currency == erpnext.get_company_currency(self.company):
			self.exchange_rate = 1
			return
		if flt(self.exchange_rate) < 0:
			frappe.throw(_("Exchange Rate cannot be negative."))
		before = None if self.is_new() else self.get_doc_before_save()
		stale = before and before.currency != self.currency and flt(before.exchange_rate) == flt(self.exchange_rate)
		if stale or flt(self.exchange_rate) in (0, 1):
			self.exchange_rate = posting.get_cheque_exchange_rate(self.currency, self.company, self.execution_date)

	def set_fee(self):
		"""The fee of the matching Transfer Fee Rule, or the user's fee (`override_fee`, with a reason)."""
		self.scope = fees.detect_scope(self)
		bearer = self.charge_bearer if self.method == fees.SWIFT else None
		result = fees.compute_fee(
			self.company,
			self.bank_account,
			self.method,
			fees.DIRECTION[self.transfer_type],
			self.scope,
			self.amount,
			bearer,
		)
		self.computed_fee = result.fee
		self.correspondent_fee = result.correspondent_fee
		self.fee_rule = _("Row #{0}").format(result.rule_idx) if result.rule_idx else None
		self.fee_basis = result.basis
		default_account = result.account or self._mapped_charges_account()
		precision = self.precision("fee_amount")
		if cint(self.override_fee):
			self.fee_amount = flt(self.fee_amount, precision)
			if self.fee_amount < 0:
				frappe.throw(_("The fee cannot be negative."))
			if not (self.fee_override_reason or "").strip():
				frappe.throw(_("A reason is required to override the fee."))
			self.fee_account = self.fee_account or default_account
		else:
			self.fee_amount = flt(result.fee, precision)
			self.fee_account = default_account
			self.fee_override_reason = None
		if self.fee_amount > 0:
			if not self.fee_account:
				frappe.throw(_("Set the Expense Account of the fee (Transfer Fee Rule or Fee Account)."))
			validate_expense_account(
				self.company, self.fee_account, _(self.meta.get_label("fee_account"), context=self.doctype)
			)
		if self.transfer_type == lifecycle.PARTY_RECEIPT and self.fee_amount >= self.amount:
			frappe.throw(_("The fee must be less than the amount."))

	def _mapped_charges_account(self) -> str | None:
		"""The Bank Charges Account of the bank's PDC Account Mapping row, if any."""
		accounts = frappe.get_all(
			"PDC Account Mapping",
			filters={
				"parenttype": "Treasury Company Settings",
				"parent": self.company,
				"bank_account": self.bank_account,
			},
			pluck="bank_charges_account",
			limit=1,
		)
		return accounts[0] if accounts else None

	def log_fee_override(self):
		"""An overridden fee is logged when it is set or changed (the only draft log row after Created)."""
		if not cint(self.override_fee):
			return
		before = None if self.is_new() else self.get_doc_before_save()
		if (
			before
			and cint(before.override_fee)
			and flt(before.fee_amount, 9) == flt(self.fee_amount, 9)
			and (before.fee_override_reason or "") == (self.fee_override_reason or "")
		):
			return
		lifecycle.log(
			self,
			"Fee Overridden",
			remarks="{} → {}: {}".format(
				fmt_money(self.computed_fee, currency=self.currency),
				fmt_money(self.fee_amount, currency=self.currency),
				self.fee_override_reason,
			),
		)

	def set_bank_amount(self):
		fee = flt(self.fee_amount)
		amount = self.amount - fee if self.transfer_type == lifecycle.PARTY_RECEIPT else self.amount + fee
		self.bank_amount = flt(amount, self.precision("bank_amount"))

	def validate_references(self):
		if self.transfer_type == lifecycle.INTERNAL:
			return
		references.validate_references(
			self,
			fees.DIRECTION[self.transfer_type],
			_("The total allocated amount cannot be greater than the transfer amount."),
		)

	def validate_accounting_details(self):
		"""Defaults (cost center, dimensions) and checks; the party's default bank account and contact are set
		by `validate_party`. An empty Mode of Payment is taken from the Modes of Payment map when the transfer
		is posted."""
		if not self.cost_center:
			self.cost_center = erpnext.get_default_cost_center(self.company)
		for fieldname, value in dimensions.default_dimensions(self.company).items():
			if value and self.meta.has_field(fieldname) and not self.get(fieldname):
				self.set(fieldname, value)
		self._validate_cost_center_and_project()
		if self.mode_of_payment and not frappe.db.get_value("Mode of Payment", self.mode_of_payment, "enabled"):
			frappe.throw(_("Mode of Payment {0} is disabled.").format(frappe.bold(self.mode_of_payment)))
		self._validate_contact()

	def _drop_stale_party_details(self):
		if self.is_new():
			return
		before = self.get_doc_before_save()
		if not before or (before.party_type, before.party) == (self.party_type, self.party):
			return
		for fieldname in (*PARTY_DETAIL_FIELDS, *BENEFICIARY_FIELDS):
			if self.get(fieldname) and self.get(fieldname) == before.get(fieldname):
				self.set(fieldname, None)

	def _validate_cost_center_and_project(self):
		cost_center = frappe.db.get_value("Cost Center", self.cost_center, ["company", "is_group"], as_dict=True)
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

	def _validate_party_bank_account(self):
		"""The beneficiary's bank account belongs to the party; its IBAN / bank / SWIFT fill the empty ones."""
		if not self.party_bank_account:
			return
		account = frappe.db.get_value(
			"Bank Account",
			self.party_bank_account,
			["party_type", "party", "is_company_account", "iban", "bank"],
			as_dict=True,
		)
		if (
			not account
			or account.is_company_account
			or (account.party_type, account.party) != (self.party_type, self.party)
		):
			frappe.throw(
				_("Bank Account {0} does not belong to {1} {2}.").format(
					frappe.bold(self.party_bank_account), _(self.party_type), frappe.bold(self.party)
				)
			)
		self.beneficiary_iban = self.beneficiary_iban or account.iban
		self.beneficiary_bank = self.beneficiary_bank or account.bank
		if not self.beneficiary_swift and self.beneficiary_bank:
			self.beneficiary_swift = frappe.db.get_value("Bank", self.beneficiary_bank, "swift_number")

	def _validate_contact(self):
		if not self.contact_person:
			return
		linked = frappe.db.exists(
			"Dynamic Link",
			{
				"parenttype": "Contact",
				"parent": self.contact_person,
				"link_doctype": self.party_type,
				"link_name": self.party,
			},
		)
		if not linked:
			frappe.throw(
				_("Contact {0} is not linked to {1} {2}.").format(
					frappe.bold(self.contact_person), _(self.party_type), frappe.bold(self.party)
				)
			)

	# ------------------------------------------------------------------ actions (see bank_transfers.lifecycle)
	# POST only: they change data, and Frappe checks the CSRF token only on POST requests.

	@frappe.whitelist(methods=["POST"])
	def send_now(self, execution_date=None, remarks=None):
		return lifecycle.send_now(self.name, execution_date or None, remarks)

	@frappe.whitelist(methods=["POST"])
	def reschedule(self, execution_date=None, remarks=None):
		return lifecycle.reschedule(self.name, execution_date or None, remarks)

	@frappe.whitelist(methods=["POST"])
	def mark_completed(self, completion_date=None, bank_transaction_date=None, remarks=None):
		return lifecycle.complete(self.name, completion_date or None, bank_transaction_date or None, remarks)

	@frappe.whitelist(methods=["POST"])
	def mark_returned(self, return_date=None, reason=None):
		return lifecycle.return_transfer(self.name, return_date or None, reason)


def transfer_settings(company: str | None) -> dict:
	"""The Bank Transfer values of the company's Treasury Company Settings the form needs (defaults of a new
	transfer, the approval threshold)."""
	if not company or not frappe.db.exists("Treasury Company Settings", company):
		return {}
	fields = [
		"default_transfer_method",
		"default_charge_bearer",
		"transfer_completion",
		"transfer_limit_action",
		"transfer_approval_threshold",
		*TRANSFER_DEFAULT_BANK_FIELDS.values(),
	]
	values = frappe.db.get_value("Treasury Company Settings", company, fields, as_dict=True)
	values["default_bank_accounts"] = {
		method: values.pop(fieldname) for method, fieldname in TRANSFER_DEFAULT_BANK_FIELDS.items()
	}
	values["company_currency"] = erpnext.get_company_currency(company)
	return values


def on_doctype_update():
	frappe.db.add_index("Bank Transfer", ["company", "status", "execution_date"])
