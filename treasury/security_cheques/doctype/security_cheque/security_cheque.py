# Copyright (c) 2026, Ahmed Abdelmageed and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, getdate, today

import erpnext
from erpnext.accounts.doctype.account.account import get_account_currency
from treasury.pdc.doctype.cheque_book.cheque_book import allocate_next_cheque_no
from treasury.security_cheques import lifecycle
from treasury.utils.protected_fields import changed_fields, changed_tables

NAMING_SERIES = {"Incoming": "SEC-IN-.YYYY.-.#####", "Outgoing": "SEC-OUT-.YYYY.-.#####"}
PARTY_TYPES = ("Customer", "Supplier", "Employee", lifecycle.OTHER)
PARTY_NAME_FIELDS = {"Customer": "customer_name", "Supplier": "supplier_name", "Employee": "employee_name"}
# Documents a security cheque may guarantee. The optional ones are offered only once their DocType exists
# (Bank Facility comes with the Bank Facilities module).
REFERENCE_DOCTYPES = (
	"Project",
	"Contract",
	"Sales Order",
	"Purchase Order",
	"Employee",
	"Quotation",
	"Supplier Quotation",
)
OPTIONAL_REFERENCE_DOCTYPES = ("Bank Facility",)
# The cheque itself: fixed once the first action (receive / issue) happened.
LOCKED_FIELDS = (
	"direction",
	"company",
	"party_type",
	"party",
	"party_name_other",
	"bank_account",
	"cheque_book",
	"cheque_no",
	"drawer_bank",
	"amount_type",
	"amount",
	"currency",
	"issue_date",
	"cheque_date",
)
# Written only by treasury.security_cheques.lifecycle (flag `treasury_lifecycle`); `read_only` in the DocType
# only affects the form, so any other save changing them is rejected (see validate_protected_fields).
PROTECTED_FIELDS = (
	"status",
	"custody_location",
	"custodian",
	"handover_date",
	"return_date",
	"return_person",
	"lost_date",
	"encashment_date",
	"encashed_amount",
	"converted_pdc_cheque",
)
# Append-only history tables, appended to only by the lifecycle.
PROTECTED_TABLES = ("custody_log", "status_log")
# A PDC Cheque in these statuses no longer holds its cheque number (see PDCCheque.validate_cheque_no).
PDC_RELEASED_STATUSES = ("Cancelled", "Replaced")


class SecurityCheque(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from treasury.security_cheques.doctype.security_cheque_custody_log.security_cheque_custody_log import (
			SecurityChequeCustodyLog,
		)
		from treasury.security_cheques.doctype.security_cheque_status_log.security_cheque_status_log import (
			SecurityChequeStatusLog,
		)

		amount: DF.Currency
		amount_type: DF.Literal["Fixed", "Open / Blank"]
		bank_account: DF.Link | None
		cheque_book: DF.Link | None
		cheque_date: DF.Date | None
		cheque_image: DF.AttachImage | None
		cheque_no: DF.Data | None
		company: DF.Link
		converted_pdc_cheque: DF.Link | None
		currency: DF.Link | None
		custodian: DF.Link | None
		custody_location: DF.Link | None
		custody_log: DF.Table[SecurityChequeCustodyLog]
		direction: DF.Literal["Incoming", "Outgoing"]
		drawer_bank: DF.Data | None
		encashed_amount: DF.Currency
		encashment_date: DF.Date | None
		expiry_date: DF.Date | None
		handover_date: DF.Date | None
		is_expired: DF.Check
		issue_date: DF.Date
		lost_date: DF.Date | None
		naming_series: DF.Literal["SEC-IN-.YYYY.-.#####", "SEC-OUT-.YYYY.-.#####"]
		party: DF.DynamicLink | None
		party_name: DF.Data | None
		party_name_other: DF.Data | None
		party_type: DF.Literal["Customer", "Supplier", "Employee", "Other"]
		purpose: DF.Literal[
			"Tender",
			"Contract Performance",
			"Advance Payment",
			"Rent",
			"Employee Custody",
			"Facility Collateral",
			"Other",
		]
		reference_doctype: DF.Link | None
		reference_name: DF.DynamicLink | None
		remarks: DF.SmallText | None
		required_amount: DF.Currency
		return_date: DF.Date | None
		return_person: DF.Data | None
		status: DF.Literal[
			"Created", "Received", "In Custody", "Issued", "Returned", "Encashed", "Lost", "Cancelled"
		]
		status_log: DF.Table[SecurityChequeStatusLog]
	# end: auto-generated types

	# ------------------------------------------------------------------ virtual fields

	@property
	def is_expired(self) -> int:
		"""Indicator only: the guarantee ended and the cheque is still held / given (not returned)."""
		return int(
			bool(self.expiry_date)
			and self.status in lifecycle.LIVE_STATUSES
			and getdate(self.expiry_date) < getdate(today())
		)

	# ------------------------------------------------------------------ hooks

	def onload(self):
		self.set_onload("reference_doctypes", get_reference_doctypes())

	def insert(self, *args, **kwargs):
		# Frappe validates links before `before_insert`: reset whatever the client sent first.
		if not self.flags.treasury_lifecycle:
			self.reset_protected_fields()
		return super().insert(*args, **kwargs)

	def before_insert(self):
		self.naming_series = NAMING_SERIES.get(self.direction, NAMING_SERIES["Incoming"])
		if self.direction == lifecycle.OUTGOING and self.cheque_book and not self.cheque_no:
			self.validate_cheque_book()
			# the leaf is consumed for good: cancelling or returning the security cheque frees nothing
			self.cheque_no = allocate_next_cheque_no(self.cheque_book)
			self.flags.cheque_no_allocated = True
		lifecycle.log_creation(self)

	def validate(self):
		self.validate_protected_fields()
		self.validate_party()
		self.validate_bank_account()
		self.set_currency()
		self.validate_amount()
		self.validate_open_amount_role()
		self.validate_cheque_book()
		self.validate_cheque_no()
		self.validate_dates()
		self.validate_reference()
		self.validate_locked_fields()

	def on_trash(self):
		if self.status != lifecycle.CREATED or self.cheque_book:
			frappe.throw(
				_("Security cheque {0} cannot be deleted once acted on or when it uses a cheque book leaf. Cancel it instead.").format(
					frappe.bold(self.name)
				)
			)

	# ------------------------------------------------------------------ validation

	def reset_protected_fields(self):
		"""A new security cheque starts clean, whatever the client sent (lifecycle.log_creation sets the status)."""
		for fieldname in PROTECTED_FIELDS:
			self.set(fieldname, None)
		self.encashed_amount = 0
		for table in PROTECTED_TABLES:
			self.set(table, [])

	def validate_protected_fields(self):
		"""Status, custody, lifecycle dates, the PDC link and the logs change only through the lifecycle."""
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
				_("{0} can only be changed by the security cheque actions.").format(
					", ".join(_(self.meta.get_label(f)) for f in changed)
				),
				title=_("Not Allowed"),
			)

	def validate_party(self):
		if self.party_type not in PARTY_TYPES:
			frappe.throw(
				_("Party Type must be one of: {0}.").format(
					", ".join(_(d, context=self.doctype) for d in PARTY_TYPES)
				)
			)
		if self.party_type == lifecycle.OTHER:
			self.party = None
			self.party_name_other = (self.party_name_other or "").strip()
			if not self.party_name_other:
				frappe.throw(_("Enter the name of the party."))
			self.party_name = self.party_name_other
			return

		self.party_name_other = None
		if not self.party:
			frappe.throw(_("Party is required."))
		party_name = frappe.db.get_value(self.party_type, self.party, PARTY_NAME_FIELDS[self.party_type])
		if party_name is None and not frappe.db.exists(self.party_type, self.party):
			frappe.throw(_("{0} {1} does not exist").format(_(self.party_type), self.party))
		self.party_name = party_name or self.party

	def validate_bank_account(self):
		if self.direction != lifecycle.OUTGOING:
			if self.bank_account or self.cheque_book:
				frappe.throw(
					_("Bank Account and Cheque Book apply to outgoing security cheques only. Enter the Drawer Bank instead.")
				)
			return
		if not self.bank_account:
			frappe.throw(_("Bank Account is required for outgoing cheques."))
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

	def set_currency(self):
		"""Outgoing: the currency of the bank GL account. Incoming: the company currency unless chosen."""
		if self.direction == lifecycle.OUTGOING:
			bank_gl = frappe.db.get_value("Bank Account", self.bank_account, "account")
			self.currency = get_account_currency(bank_gl)
		elif not self.currency:
			self.currency = erpnext.get_company_currency(self.company)

	def validate_amount(self):
		precision = self.precision("amount")
		self.amount = flt(self.amount, precision)
		self.required_amount = flt(self.required_amount, self.precision("required_amount"))
		if self.amount_type == lifecycle.FIXED:
			if self.amount <= 0:
				frappe.throw(_("Amount must be greater than zero."))
		elif self.amount_type == lifecycle.OPEN:
			if self.amount:
				frappe.throw(_("An Open / Blank security cheque carries no amount: leave the Amount empty."))
		else:
			frappe.throw(
				_("Amount Type must be one of: {0}.").format(
					", ".join(_(t, context=self.doctype) for t in (lifecycle.FIXED, lifecycle.OPEN))
				)
			)
		if self.required_amount < 0:
			frappe.throw(_("Required Amount cannot be negative."))

	def validate_open_amount_role(self):
		"""Creating (or turning a cheque into) an Open / Blank security cheque needs the Treasury Manager."""
		if self.amount_type != lifecycle.OPEN or self.flags.treasury_lifecycle:
			return
		if self.is_new() or self.has_value_changed("amount_type"):
			lifecycle.require_open_amount_role(self)

	def validate_cheque_book(self):
		if not self.cheque_book:
			return
		book = frappe.db.get_value(
			"Cheque Book", self.cheque_book, ["company", "bank_account", "status"], as_dict=True
		)
		if not book or book.company != self.company or book.bank_account != self.bank_account:
			frappe.throw(
				_("Cheque Book {0} does not belong to Bank Account {1}.").format(
					frappe.bold(self.cheque_book), frappe.bold(self.bank_account)
				)
			)
		if self.is_new() and book.status != "Active" and not self.flags.cheque_no_allocated:
			frappe.throw(_("Cheque Book {0} is {1}.").format(frappe.bold(self.cheque_book), _(book.status)))
		if self.cheque_no and self.has_value_changed("cheque_no"):
			cheque_book = frappe.get_doc("Cheque Book", self.cheque_book)
			if cheque_book.parse_leaf(self.cheque_no) in cheque_book.cancelled_numbers():
				frappe.throw(
					_("Cheque No {0} is a cancelled leaf of Cheque Book {1}.").format(
						frappe.bold(self.cheque_no), frappe.bold(self.cheque_book)
					)
				)

	def validate_cheque_no(self):
		"""Our own (outgoing) cheque numbers are unique per bank account, across security and PDC cheques.

		A security cheque keeps its number whatever its status (a cancelled or returned leaf is used); the PDC
		Cheque its encashment created carries the same number on purpose.
		"""
		self.cheque_no = (self.cheque_no or "").strip()
		if not self.cheque_no:
			frappe.throw(_("Cheque No is required."))
		if self.direction != lifecycle.OUTGOING:
			return
		if self.has_value_changed("cheque_no") or self.has_value_changed("bank_account"):
			# serialise concurrent saves of cheque numbers on this bank account (see PDCCheque.validate_cheque_no)
			frappe.db.get_value("Bank Account", self.bank_account, "name", for_update=True)
		filters = {"direction": lifecycle.OUTGOING, "bank_account": self.bank_account, "cheque_no": self.cheque_no}
		duplicate = frappe.db.exists("Security Cheque", {**filters, "name": ["!=", self.name]})
		if not duplicate:
			pdc_cheques = frappe.get_all(
				"PDC Cheque",
				filters={**filters, "operational_status": ["not in", PDC_RELEASED_STATUSES]},
				fields=["name", "security_cheque"],
			)
			duplicate = next((c.name for c in pdc_cheques if c.security_cheque != self.name), None)
		if duplicate:
			frappe.throw(
				_("Cheque No {0} is already used on Bank Account {1} by {2}.").format(
					frappe.bold(self.cheque_no), frappe.bold(self.bank_account), frappe.bold(duplicate)
				)
			)

	def validate_dates(self):
		if self.expiry_date and getdate(self.expiry_date) < getdate(self.issue_date):
			frappe.throw(_("The Expiry Date cannot be before the Issue Date."))

	def validate_reference(self):
		if not self.reference_doctype:
			if self.reference_name:
				frappe.throw(_("Select the Reference Type of {0}.").format(frappe.bold(self.reference_name)))
			return
		allowed = get_reference_doctypes()
		if self.reference_doctype not in allowed:
			frappe.throw(_("Reference Type must be one of: {0}.").format(", ".join(_(d) for d in allowed)))
		if not self.reference_name:
			frappe.throw(_("Reference Name is required."))
		if not frappe.db.exists(self.reference_doctype, self.reference_name):
			frappe.throw(_("{0} {1} does not exist").format(_(self.reference_doctype), self.reference_name))
		if frappe.get_meta(self.reference_doctype).has_field("company"):
			company = frappe.db.get_value(self.reference_doctype, self.reference_name, "company")
			if company and company != self.company:
				frappe.throw(
					_("{0} {1} does not belong to company {2}.").format(
						_(self.reference_doctype), frappe.bold(self.reference_name), frappe.bold(self.company)
					)
				)

	def validate_locked_fields(self):
		"""The cheque in the party's (or our) hands is what it is: fixed after the first action."""
		if self.is_new() or self.flags.treasury_lifecycle:
			return
		before = self.get_doc_before_save()
		if not before or before.status == lifecycle.CREATED:
			return
		changed = changed_fields(self, before, LOCKED_FIELDS)
		if changed:
			frappe.throw(
				_("{0} cannot be changed after the security cheque is received or issued.").format(
					", ".join(_(self.meta.get_label(f)) for f in changed)
				),
				title=_("Not Allowed"),
			)

	# ------------------------------------------------------------------ actions (treasury.security_cheques.lifecycle)
	# POST only: they change data, and Frappe checks the CSRF token only on POST requests.

	@frappe.whitelist(methods=["POST"])
	def receive(self, receipt_date=None, custody_location=None, custodian=None, remarks=None):
		return lifecycle.receive(self.name, receipt_date, custody_location or None, custodian or None, remarks)

	@frappe.whitelist(methods=["POST"])
	def store(self, store_date=None, custody_location=None, custodian=None, remarks=None):
		return lifecycle.store(self.name, store_date, custody_location or None, custodian or None, remarks)

	@frappe.whitelist(methods=["POST"])
	def transfer_custody(self, transfer_date=None, custody_location=None, custodian=None, remarks=None):
		return lifecycle.transfer_custody(
			self.name, transfer_date, custody_location or None, custodian or None, remarks
		)

	@frappe.whitelist(methods=["POST"])
	def issue(self, handover_date=None, remarks=None):
		return lifecycle.issue(self.name, handover_date, remarks)

	@frappe.whitelist(methods=["POST"])
	def return_cheque(self, return_date=None, person=None, reason=None):
		return lifecycle.return_cheque(self.name, return_date, person, reason)

	@frappe.whitelist(methods=["POST"])
	def mark_lost(self, lost_date=None, reason=None):
		return lifecycle.mark_lost(self.name, lost_date, reason)

	@frappe.whitelist(methods=["POST"])
	def cancel_cheque(self, reason=None):
		return lifecycle.cancel_cheque(self.name, reason)

	@frappe.whitelist(methods=["POST"])
	def encash(
		self,
		encashment_date=None,
		amount=None,
		due_date=None,
		bank_account=None,
		references=None,
		remarks=None,
	):
		return lifecycle.encash(
			self.name,
			encashment_date,
			None if amount in (None, "") else flt(amount),
			due_date or None,
			bank_account or None,
			references,
			remarks,
		)


def get_reference_doctypes() -> list[str]:
	return [*REFERENCE_DOCTYPES, *(d for d in OPTIONAL_REFERENCE_DOCTYPES if frappe.db.exists("DocType", d))]


@frappe.whitelist()
def reference_doctypes() -> list[str]:
	"""The document types a security cheque may reference (for the form's Reference Type filter)."""
	frappe.has_permission("Security Cheque", "read", throw=True)
	return get_reference_doctypes()


def on_doctype_update():
	"""Index for the reports, number cards and the expiry alerts, which all filter on these columns."""
	frappe.db.add_index("Security Cheque", ["company", "direction", "status", "expiry_date"])
