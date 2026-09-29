# Copyright (c) 2026, Ahmed Abdelmageed and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model import no_value_fields
from frappe.model.document import Document
from frappe.utils import cint, flt, get_datetime, getdate, money_in_words

import erpnext
from erpnext.accounts.doctype.account.account import get_account_currency
from treasury.accounting import posting
from treasury.pdc import lifecycle
from treasury.pdc.doctype.cheque_book.cheque_book import allocate_next_cheque_no

PARTY_TYPES = ("Customer", "Supplier")
NAMING_SERIES = {"Outgoing": "PDC-OUT-.YYYY.-.#####", "Incoming": "PDC-IN-.YYYY.-.#####"}
# Reference types allowed per direction, and the field holding their party.
REFERENCE_PARTY_FIELD = {
	"Outgoing": {"Purchase Invoice": "supplier", "Purchase Order": "supplier", "Journal Entry": None},
	"Incoming": {"Sales Invoice": "customer", "Sales Order": "customer", "Journal Entry": None},
}
# Fields that cannot change once the cheque left the Created status (except by lifecycle actions) or is
# posted to accounts (the vouchers were built from them).
LOCKED_FIELDS = ("direction", "company", "party_type", "party", "bank_account", "currency", "cheque_no")
LOCKED_AMOUNT_FIELDS = ("amount", "exchange_rate")
# Dates that cannot change once the cheque left the Created status (the due date then moves only by Postpone).
LOCKED_DATE_FIELDS = ("issue_date", "original_due_date")
# Written only by treasury.pdc.lifecycle, which saves with `flags.treasury_lifecycle`. `read_only` in the
# DocType only affects the form, so a REST / client save must be rejected here (see validate_protected_fields).
PROTECTED_FIELDS = (
	"operational_status",
	"accounting_status",
	"cycle",
	"is_postponed",
	"postponement_count",
	"effective_due_date",
	"handover_date",
	"deposit_date",
	"actual_clearance_date",
	"bank_transaction_date",
	"party_posting_date",
	"bank_posting_date",
	"bank_charges",
	"party_payment_entry",
	"under_collection_journal",
	"bank_journal",
	"bank_charges_journal",
	"reversal_journal",
	"bounce_charges_journal",
	"replaced_by",
	"replaces",
)
# Append-only history tables, appended to only by the lifecycle.
PROTECTED_TABLES = ("status_log", "postponements")
# A cheque number may be reused on a bank account only by cheques in these statuses.
RELEASED_STATUSES = ("Cancelled", "Replaced")


class PDCCheque(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from treasury.pdc.doctype.pdc_cheque_reference.pdc_cheque_reference import PDCChequeReference
		from treasury.pdc.doctype.pdc_postponement.pdc_postponement import PDCPostponement
		from treasury.pdc.doctype.pdc_status_log.pdc_status_log import PDCStatusLog

		accounting_status: DF.Literal[
			"Not Posted", "Party Posted", "Under Collection Posted", "Bank Posted", "Reversed"
		]
		actual_clearance_date: DF.Date | None
		amount: DF.Currency
		amount_in_words: DF.SmallText | None
		bank_account: DF.Link | None
		bank_charges: DF.Currency
		bank_charges_journal: DF.Link | None
		bank_journal: DF.Link | None
		bank_posting_date: DF.Date | None
		bank_transaction_date: DF.Date | None
		bounce_charges_journal: DF.Link | None
		cheque_book: DF.Link | None
		cheque_image: DF.AttachImage | None
		cheque_no: DF.Data | None
		company: DF.Link
		currency: DF.Link | None
		cycle: DF.Int
		deposit_date: DF.Date | None
		direction: DF.Literal["Outgoing", "Incoming"]
		drawer_bank: DF.Data | None
		effective_due_date: DF.Date | None
		exchange_rate: DF.Float
		handover_date: DF.Date | None
		is_postponed: DF.Check
		issue_date: DF.Date
		naming_series: DF.Literal["PDC-OUT-.YYYY.-.#####", "PDC-IN-.YYYY.-.#####"]
		operational_status: DF.Literal[
			"Created",
			"Delivered",
			"Received",
			"Deposited",
			"Cleared",
			"Bounced",
			"Returned",
			"Endorsed",
			"Discounted",
			"Replaced",
			"Cancelled",
		]
		original_due_date: DF.Date
		party: DF.DynamicLink
		party_name: DF.Data | None
		party_payment_entry: DF.Link | None
		party_posting_date: DF.Date | None
		party_type: DF.Link
		postponement_count: DF.Int
		postponements: DF.Table[PDCPostponement]
		references: DF.Table[PDCChequeReference]
		remarks: DF.SmallText | None
		replaced_by: DF.Link | None
		replaces: DF.Link | None
		reversal_journal: DF.Link | None
		status_log: DF.Table[PDCStatusLog]
		under_collection_journal: DF.Link | None
		vouchers_html: DF.HTML | None
	# end: auto-generated types

	# ------------------------------------------------------------------ hooks

	def onload(self):
		if not self.is_new():
			self.set_onload("vouchers", posting.get_linked_vouchers(self.doctype, self.name))
			self.set_onload("cheque_print_format", self.get_cheque_print_format())

	def get_cheque_print_format(self) -> str | None:
		"""The Cheque Print Format hinted by the PDC Account Mapping row of the bank account, if any."""
		if not self.bank_account:
			return None
		formats = frappe.get_all(
			"PDC Account Mapping",
			filters={
				"parenttype": "Treasury Company Settings",
				"parent": self.company,
				"bank_account": self.bank_account,
			},
			pluck="cheque_print_format",
			limit=1,
		)
		return formats[0] if formats else None

	def insert(self, *args, **kwargs):
		# Frappe validates links before `before_insert`, so a client-sent voucher link would fail link
		# validation (or pass it, if real) before it could be discarded: reset first.
		if not self.flags.treasury_lifecycle:
			self.reset_protected_fields()
		return super().insert(*args, **kwargs)

	def before_insert(self):
		self.naming_series = NAMING_SERIES.get(self.direction, NAMING_SERIES["Outgoing"])
		self.effective_due_date = self.original_due_date
		if self.direction == "Outgoing" and self.cheque_book and not self.cheque_no:
			self.validate_cheque_book()
			self.cheque_no = allocate_next_cheque_no(self.cheque_book)
			# the book may have become Exhausted by this very allocation
			self.flags.cheque_no_allocated = True
		lifecycle.log_creation(self)

	def after_insert(self):
		lifecycle.after_insert(self)

	def validate(self):
		# first: later steps (validate_dates) legitimately derive effective_due_date
		self.validate_protected_fields()
		self.validate_party()
		self.validate_bank_account()
		self.set_currency_and_rate()
		self.validate_amount()
		self.validate_cheque_book()
		self.validate_cheque_no()
		self.validate_dates()
		self.validate_references()
		self.validate_locked_fields()
		self.amount_in_words = money_in_words(self.amount, self.currency)

	def on_trash(self):
		if self.accounting_status != lifecycle.NOT_POSTED or any(
			v.docstatus == 1 for v in posting.get_linked_vouchers(self.doctype, self.name)
		):
			frappe.throw(
				_("Cheque {0} has accounting entries and cannot be deleted. Cancel it instead.").format(
					frappe.bold(self.name)
				)
			)

	# ------------------------------------------------------------------ validation

	def reset_protected_fields(self):
		"""A new cheque starts clean, whatever the client sent (lifecycle.log_creation sets the statuses)."""
		for fieldname in PROTECTED_FIELDS:
			self.set(fieldname, None)
		self.bank_charges = 0
		for table in PROTECTED_TABLES:
			self.set(table, [])

	def validate_protected_fields(self):
		"""Statuses, lifecycle dates, voucher links and the history tables change only through the lifecycle.

		Otherwise a client could e.g. reset `accounting_status` / bump `cycle` and have the same cheque posted
		twice, mark it Cleared without accounting, or move the due date without the approver role.
		"""
		if self.is_new():
			return
		before = self.get_doc_before_save()
		if not before:
			return
		lifecycle_save = self.flags.treasury_lifecycle

		changed = []
		if not lifecycle_save:
			changed = [f for f in PROTECTED_FIELDS if self._normalized(before, f) != self._normalized(self, f)]
		for table in PROTECTED_TABLES:
			old_rows, new_rows = self._table_key(before, table), self._table_key(self, table)
			# existing rows never change; only the lifecycle appends new ones
			if new_rows[: len(old_rows)] != old_rows or (len(new_rows) != len(old_rows) and not lifecycle_save):
				changed.append(table)
		if changed:
			frappe.throw(
				_("{0} can only be changed by the cheque actions.").format(
					", ".join(_(self.meta.get_label(f)) for f in changed)
				),
				title=_("Not Allowed"),
			)

	@staticmethod
	def _normalize(value, fieldtype: str):
		if fieldtype == "Date":
			return getdate(value) if value else None
		if fieldtype == "Datetime":
			return get_datetime(value) if value else None
		if fieldtype in ("Int", "Check"):
			return cint(value)
		if fieldtype in ("Currency", "Float", "Percent"):
			return flt(value, 9)
		return value or None

	def _normalized(self, doc, fieldname: str):
		return self._normalize(doc.get(fieldname), self.meta.get_field(fieldname).fieldtype)

	def _table_key(self, doc, table: str) -> list[tuple]:
		fields = [
			df
			for df in frappe.get_meta(self.meta.get_field(table).options).fields
			if df.fieldtype not in no_value_fields
		]
		return [
			tuple(self._normalize(row.get(df.fieldname), df.fieldtype) for df in fields)
			for row in doc.get(table)
		]

	def validate_party(self):
		if self.party_type not in PARTY_TYPES:
			frappe.throw(
				_("Party Type must be one of: {0}.").format(", ".join(_(d) for d in PARTY_TYPES))
			)
		if not self.party:
			frappe.throw(_("Party is required."))
		name_field = "customer_name" if self.party_type == "Customer" else "supplier_name"
		party_name = frappe.db.get_value(self.party_type, self.party, name_field)
		if party_name is None and not frappe.db.exists(self.party_type, self.party):
			frappe.throw(_("{0} {1} does not exist").format(_(self.party_type), self.party))
		self.party_name = party_name or self.party

	def validate_bank_account(self):
		if not self.bank_account:
			if self.direction == "Outgoing":
				frappe.throw(_("Bank Account is required for outgoing cheques."))
			return
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
			frappe.throw(
				_("Bank Account {0} has no GL account.").format(frappe.bold(self.bank_account))
			)
		# throws when the bank account has no PDC Account Mapping row
		posting.get_account_mapping(self.company, self.bank_account)

	def set_currency_and_rate(self):
		"""The cheque currency is the currency of the bank GL account (company currency until a bank is set).

		`currency` is read-only: whatever it holds without a bank account (e.g. the user/site default currency
		Frappe fills into Link Currency fields on a new doc) is not a user choice and is overwritten.
		"""
		company_currency = erpnext.get_company_currency(self.company)
		if self.bank_account:
			bank_gl = frappe.db.get_value("Bank Account", self.bank_account, "account")
			self.currency = get_account_currency(bank_gl)
		else:
			self.currency = company_currency
		if self.currency == company_currency:
			self.exchange_rate = 1
			return
		if flt(self.exchange_rate) < 0:
			frappe.throw(_("Exchange Rate cannot be negative."))
		before = None if self.is_new() else self.get_doc_before_save()
		if before and before.currency != self.currency and flt(before.exchange_rate) == flt(self.exchange_rate):
			# the rate of the previous currency (e.g. 1 for the company currency) is meaningless now;
			# it is fetched again at the party posting date
			self.exchange_rate = 0

	def validate_amount(self):
		self.amount = flt(self.amount, self.precision("amount"))
		if self.amount <= 0:
			frappe.throw(_("Amount must be greater than zero."))

	def validate_cheque_book(self):
		if not self.cheque_book:
			return
		if self.direction != "Outgoing":
			frappe.throw(_("A cheque book can only be used on outgoing cheques."))
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
			frappe.throw(
				_("Cheque Book {0} is {1}.").format(frappe.bold(self.cheque_book), _(book.status))
			)
		if self.cheque_no and self.has_value_changed("cheque_no"):
			cheque_book = frappe.get_doc("Cheque Book", self.cheque_book)
			if cheque_book.parse_leaf(self.cheque_no) in cheque_book.cancelled_numbers():
				frappe.throw(
					_("Cheque No {0} is a cancelled leaf of Cheque Book {1}.").format(
						frappe.bold(self.cheque_no), frappe.bold(self.cheque_book)
					)
				)

	def validate_cheque_no(self):
		self.cheque_no = (self.cheque_no or "").strip()
		if not self.cheque_no:
			frappe.throw(_("Cheque No is required."))
		if self.direction != "Outgoing" or self.operational_status in RELEASED_STATUSES:
			return
		if self.has_value_changed("cheque_no") or self.has_value_changed("bank_account"):
			# Serialise concurrent saves of cheque numbers on this bank account until commit, so two
			# transactions cannot both pass the duplicate check below with the same hand-typed number.
			frappe.db.get_value("Bank Account", self.bank_account, "name", for_update=True)
		duplicate = frappe.db.exists(
			"PDC Cheque",
			{
				"name": ["!=", self.name],
				"direction": "Outgoing",
				"bank_account": self.bank_account,
				"cheque_no": self.cheque_no,
				"operational_status": ["not in", RELEASED_STATUSES],
			},
		)
		if duplicate:
			frappe.throw(
				_("Cheque No {0} is already used on Bank Account {1} by {2}.").format(
					frappe.bold(self.cheque_no), frappe.bold(self.bank_account), frappe.bold(duplicate)
				)
			)

	def validate_dates(self):
		if getdate(self.original_due_date) < getdate(self.issue_date):
			frappe.throw(_("The Original Due Date cannot be before the Issue Date."))
		if not self.is_postponed and self.accounting_status == lifecycle.NOT_POSTED:
			self.effective_due_date = self.original_due_date
		elif not self.effective_due_date:
			self.effective_due_date = self.original_due_date

	def validate_references(self):
		allowed = REFERENCE_PARTY_FIELD.get(self.direction, {})
		precision = self._allocation_precision()
		total = 0
		for row in self.references:
			if row.reference_doctype not in allowed:
				frappe.throw(
					_("Row #{0}: Reference Type must be one of: {1}.").format(
						row.idx, ", ".join(_(d) for d in allowed)
					)
				)
			if flt(row.allocated_amount) <= 0:
				frappe.throw(_("Row #{0}: Allocated Amount must be greater than zero.").format(row.idx))
			reference = frappe.db.get_value(
				row.reference_doctype, row.reference_name, ["company", "docstatus"], as_dict=True
			)
			if not reference or reference.docstatus != 1 or reference.company != self.company:
				frappe.throw(
					_("Row #{0}: {1} {2} must be a submitted document of company {3}.").format(
						row.idx, _(row.reference_doctype), frappe.bold(row.reference_name), self.company
					)
				)
			party_field = allowed[row.reference_doctype]
			reference_party = party_field and frappe.db.get_value(
				row.reference_doctype, row.reference_name, party_field
			)
			if party_field and reference_party != self.party:
				frappe.throw(
					_("Row #{0}: {1} {2} does not belong to {3}.").format(
						row.idx,
						_(row.reference_doctype),
						frappe.bold(row.reference_name),
						frappe.bold(self.party),
					)
				)
			total += flt(row.allocated_amount, precision)

		# Allocations are in the party account currency; compare only when that is the cheque currency.
		company_currency = erpnext.get_company_currency(self.company)
		if self.currency == company_currency and flt(total, precision) > flt(self.amount, precision):
			frappe.throw(_("The total allocated amount cannot be greater than the cheque amount."))

	def validate_locked_fields(self):
		if self.is_new():
			return
		before = self.get_doc_before_save()
		if not before:
			return

		if before.operational_status != "Created":
			changed = [f for f in LOCKED_DATE_FIELDS if getdate(before.get(f)) != getdate(self.get(f))]
			if changed:
				frappe.throw(
					_(
						"{0} cannot be changed after the cheque is handed over or received. Use Postpone to move the due date."
					).format(", ".join(_(self.meta.get_label(f)) for f in changed)),
					title=_("Not Allowed"),
				)
			# The cheque in the party's hands is what it is. Lifecycle actions may still change these
			# (e.g. `deposit` into another bank account, the exchange rate set at the party posting).
			changed = [] if self.flags.treasury_lifecycle else self._changed_identity_fields(before)
			if changed:
				frappe.throw(
					_("{0} cannot be changed after the cheque is handed over or received.").format(
						", ".join(_(self.meta.get_label(f)) for f in changed)
					),
					title=_("Not Allowed"),
				)

		if before.accounting_status == lifecycle.NOT_POSTED:
			return
		changed = self._changed_identity_fields(before)
		changed += [f for f in LOCKED_DATE_FIELDS if getdate(before.get(f)) != getdate(self.get(f))]
		if self._references_key(before) != self._references_key(self):
			changed.append("references")
		if changed:
			frappe.throw(
				_("{0} cannot be changed after the cheque is posted to accounts.").format(
					", ".join(_(self.meta.get_label(f)) for f in changed)
				),
				title=_("Not Allowed"),
			)

	def _changed_identity_fields(self, before) -> list[str]:
		changed = [f for f in LOCKED_FIELDS if (before.get(f) or None) != (self.get(f) or None)]
		return changed + [f for f in LOCKED_AMOUNT_FIELDS if flt(before.get(f), 9) != flt(self.get(f), 9)]

	@staticmethod
	def _allocation_precision() -> int:
		return frappe.get_precision("PDC Cheque Reference", "allocated_amount") or 2

	@classmethod
	def _references_key(cls, doc) -> list[tuple]:
		precision = cls._allocation_precision()
		return sorted(
			(r.reference_doctype, r.reference_name, flt(r.allocated_amount, precision))
			for r in doc.get("references")
		)

	# ------------------------------------------------------------------ actions (see treasury.pdc.lifecycle)
	# POST only: they change data, and Frappe checks the CSRF token only on POST requests.

	@frappe.whitelist(methods=["POST"])
	def deliver(self, handover_date=None, remarks=None):
		return lifecycle.deliver(self.name, handover_date, remarks)

	@frappe.whitelist(methods=["POST"])
	def receive(self, receipt_date=None, remarks=None):
		return lifecycle.receive(self.name, receipt_date, remarks)

	@frappe.whitelist(methods=["POST"])
	def post_party(self, posting_date=None):
		return lifecycle.post_party(self.name, posting_date)

	@frappe.whitelist(methods=["POST"])
	def deposit(self, deposit_date=None, bank_account=None, remarks=None):
		return lifecycle.deposit(self.name, deposit_date, bank_account, remarks)

	@frappe.whitelist(methods=["POST"])
	def clear(
		self,
		clearance_date=None,
		bank_transaction_date=None,
		bank_charges=0,
		exchange_rate=None,
		remarks=None,
		bank_account=None,
	):
		return lifecycle.clear(
			self.name,
			clearance_date,
			bank_transaction_date,
			flt(bank_charges),
			flt(exchange_rate) or None,
			remarks,
			bank_account or None,
		)

	@frappe.whitelist(methods=["POST"])
	def post_bank(self, posting_date=None, bank_charges=None, exchange_rate=None):
		return lifecycle.post_bank(
			self.name,
			posting_date,
			None if bank_charges in (None, "") else flt(bank_charges),
			flt(exchange_rate) or None,
		)

	@frappe.whitelist(methods=["POST"])
	def postpone(self, new_due_date=None, reason=None, contact_person=None):
		return lifecycle.postpone(self.name, new_due_date, reason, contact_person)

	@frappe.whitelist(methods=["POST"])
	def bounce(self, bounce_date=None, reason=None, charges=0):
		return lifecycle.bounce(self.name, bounce_date, reason, flt(charges))

	@frappe.whitelist(methods=["POST"])
	def represent(self, represent_date=None, new_due_date=None, remarks=None):
		return lifecycle.represent(self.name, represent_date, new_due_date, remarks)

	@frappe.whitelist(methods=["POST"])
	def replace(self, new_cheque_values=None):
		return lifecycle.replace(self.name, new_cheque_values)

	@frappe.whitelist(methods=["POST"])
	def cancel_cheque(self, reason=None):
		return lifecycle.cancel_cheque(self.name, reason)

	@frappe.whitelist(methods=["POST"])
	def return_to_party(self, return_date=None, reason=None):
		return lifecycle.return_to_party(self.name, return_date, reason)


def on_doctype_update():
	"""Indexes for the PDC reports, number cards and schedulers, which all filter on these columns."""
	frappe.db.add_index("PDC Cheque", ["company", "direction", "operational_status", "effective_due_date"])
