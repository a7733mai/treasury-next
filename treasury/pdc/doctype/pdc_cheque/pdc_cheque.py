# Copyright (c) 2026, Ahmed Abdelmageed and contributors
# For license information, please see license.txt

import erpnext
import frappe
from erpnext.accounts.doctype.account.account import get_account_currency
from erpnext.accounts.doctype.bank_account.bank_account import get_party_bank_account
from frappe import _
from frappe.contacts.doctype.contact.contact import get_default_contact
from frappe.model.document import Document
from frappe.utils import cint, flt, getdate, money_in_words

from treasury.accounting import dimensions, payment_modes, posting
from treasury.pdc import cheque_printing, lifecycle
from treasury.pdc.doctype.cheque_book.cheque_book import allocate_next_cheque_no, lock_covering_books
from treasury.utils import references, voucher_summary
from treasury.utils.party import get_party_name, validate_party_type
from treasury.utils.protected_fields import changed_fields, changed_tables
from treasury.utils.references import reference_rules

NAMING_SERIES = {"Outgoing": "PDC-OUT-.YYYY.-.#####", "Incoming": "PDC-IN-.YYYY.-.#####"}
# Accounting details copied to every voucher of the cheque (with the accounting dimension fields); defaulted
# while the cheque is not posted, locked once it is.
ACCOUNTING_FIELDS = lifecycle.ACCOUNTING_FIELDS
# Party details defaulted from the party: dropped when the party changes (unless chosen for the new one).
PARTY_DETAIL_FIELDS = ("party_bank_account", "contact_person")
# Saved values the defaults are compared with to find the stale ones (see `_drop_stale_defaults`).
STALE_CHECK_FIELDS = ("bank_account", "party_type", "party", "mode_of_payment", *PARTY_DETAIL_FIELDS)


# Fields that cannot change once the cheque left the Created status (except by lifecycle actions) or is
# posted to accounts (the vouchers were built from them).
LOCKED_FIELDS = ("direction", "company", "party_type", "party", "bank_account", "currency", "cheque_no")
LOCKED_AMOUNT_FIELDS = ("amount", "exchange_rate")
# Dates that cannot change once the cheque left the Created status (the due date then moves only by Postpone).
LOCKED_DATE_FIELDS = ("issue_date", "original_due_date")
# Link to the Cheque Batch that created the cheque: set only on insert by treasury.pdc.batch (flag
# `treasury_cheque_batch`) or carried to a replacement by the lifecycle, never changed afterwards.
BATCH_FIELDS = ("cheque_batch", "cheque_batch_row")
# Back-link to the Security Cheque whose encashment created the cheque: set only on insert by
# treasury.security_cheques.lifecycle (flag `treasury_security_cheque`) or carried to a replacement by the
# lifecycle, never changed afterwards.
SECURITY_FIELDS = ("security_cheque",)
# What a submitted Cheque Batch voucher states about each of its cheques: frozen outside the cheque actions
# while the batch is submitted (replace or cancel the cheque instead), together with the references.
BATCH_LOCKED_FIELDS = (*LOCKED_FIELDS, "cheque_book", "drawer_bank")
BATCH_LOCKED_VALUE_FIELDS = ("amount",)
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
	"discount_date",
	"actual_clearance_date",
	"bank_transaction_date",
	"party_posting_date",
	"bank_posting_date",
	"bank_charges",
	"cheque_discounting",
	"party_payment_entry",
	"under_collection_payment_entry",
	"bank_payment_entry",
	"reversal_payment_entry",
	"under_collection_journal",
	"bank_journal",
	"bank_charges_journal",
	"reversal_journal",
	"bounce_charges_journal",
	"replaced_by",
	"replaces",
	*BATCH_FIELDS,
	*SECURITY_FIELDS,
	# written by lifecycle.record_print; a new cheque (a replacement too) starts unprinted
	"print_count",
	"last_printed_on",
	"last_printed_by",
	"last_print_job",
	"printed_fingerprint",
)
# What the leaf of a printed cheque shows (cheque_printing.PRINTED_FIELDS): a form / REST save cannot change it
# once the cheque was printed; the leaf is then replaced or cancelled (design 7f fix round 2).
PRINTED_LOCKED_FIELDS = cheque_printing.PRINTED_FIELDS
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
		bank_payment_entry: DF.Link | None
		bank_posting_date: DF.Date | None
		bank_transaction_date: DF.Date | None
		bounce_charges_journal: DF.Link | None
		cheque_batch: DF.Link | None
		cheque_batch_row: DF.Data | None
		cheque_book: DF.Link | None
		cheque_discounting: DF.Link | None
		cheque_image: DF.AttachImage | None
		cheque_no: DF.Data | None
		company: DF.Link
		contact_person: DF.Link | None
		cost_center: DF.Link | None
		currency: DF.Link | None
		cycle: DF.Int
		deposit_date: DF.Date | None
		direction: DF.Literal["Outgoing", "Incoming"]
		discount_date: DF.Date | None
		drawer_bank: DF.Data | None
		effective_due_date: DF.Date | None
		exchange_rate: DF.Float
		handover_date: DF.Date | None
		is_postponed: DF.Check
		issue_date: DF.Date
		last_print_job: DF.Data | None
		last_printed_by: DF.Link | None
		last_printed_on: DF.Datetime | None
		mode_of_payment: DF.Link | None
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
		party_bank_account: DF.Link | None
		party_name: DF.Data | None
		party_payment_entry: DF.Link | None
		party_posting_date: DF.Date | None
		party_type: DF.Link
		payee_on_cheque: DF.Data | None
		postponement_count: DF.Int
		postponements: DF.Table[PDCPostponement]
		print_count: DF.Int
		printed_fingerprint: DF.Data | None
		project: DF.Link | None
		references: DF.Table[PDCChequeReference]
		remarks: DF.SmallText | None
		replaced_by: DF.Link | None
		replaces: DF.Link | None
		reversal_journal: DF.Link | None
		reversal_payment_entry: DF.Link | None
		security_cheque: DF.Link | None
		status_log: DF.Table[PDCStatusLog]
		under_collection_journal: DF.Link | None
		under_collection_payment_entry: DF.Link | None
		vouchers_html: DF.HTML | None
	# end: auto-generated types

	# ------------------------------------------------------------------ hooks

	def onload(self):
		if not self.is_new():
			cycle = self.cycle or 1
			self.set_onload("voucher_summary", voucher_summary.render(self.doctype, [self.name], cycle=cycle))
			self.set_onload(
				"payment_entries", voucher_summary.payment_entries(self.doctype, [self.name], cycle=cycle)
			)
			layout = (
				cheque_printing.get_layout(self.bank_account) if self.direction == lifecycle.OUTGOING else None
			)
			self.set_onload("cheque_print_format", self.get_cheque_print_format(layout))
			self.set_onload("batch_locked", self.is_batch_locked())
			# {name, default_language, reprint_requires_manager}: the "Print Cheque" dialog (design 7f §6.2)
			self.set_onload("cheque_layout", cheque_printing.layout_info(layout, self.company))

	def get_cheque_print_format(self, layout=None) -> str | None:
		"""The print format of the cheque leaf: an outgoing cheque's bank Cheque Layout ("Treasury Cheque"),
		else the Cheque Print Format of the bank's PDC Account Mapping row, else "PDC Cheque Print".
		`layout`: the bank's layout when the caller already loaded it (one lookup per form load)."""
		if not self.bank_account:
			return None
		if self.direction == lifecycle.OUTGOING:
			return cheque_printing.resolve_print_format(self.company, self.bank_account, layout)
		return (
			cheque_printing.mapping_print_format(self.company, self.bank_account)
			or cheque_printing.LEGACY_FORMAT
		)

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
		self.validate_accounting_details()
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
		"""A new cheque starts clean, whatever the client sent (lifecycle.log_creation sets the statuses).

		Only a cheque inserted by a Cheque Batch keeps its batch link, and only one inserted by a security
		cheque encashment its security cheque link (flags, which a client cannot send)."""
		keep = BATCH_FIELDS if self.flags.treasury_cheque_batch else ()
		if self.flags.treasury_security_cheque:
			keep += SECURITY_FIELDS
		for fieldname in PROTECTED_FIELDS:
			if fieldname not in keep:
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
		lifecycle_save = bool(self.flags.treasury_lifecycle)
		changed = [] if lifecycle_save else changed_fields(self, before, PROTECTED_FIELDS)
		# existing rows never change; only the lifecycle appends new ones
		changed += changed_tables(self, before, PROTECTED_TABLES, lifecycle_save)
		if changed:
			frappe.throw(
				_("{0} can only be changed by the cheque actions.").format(
					", ".join(_(self.meta.get_label(f)) for f in changed)
				),
				title=_("Not Allowed"),
			)

	def validate_party(self):
		"""Any ERPNext Party Type (Customer, Supplier, Employee, Shareholder ...)."""
		validate_party_type(self.party_type)
		if not self.party:
			frappe.throw(_("Party is required."))
		if not frappe.db.exists(self.party_type, self.party):
			frappe.throw(_("{0} {1} does not exist").format(_(self.party_type), self.party))
		self.party_name = get_party_name(self.party_type, self.party)

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
			frappe.throw(_("Bank Account {0} has no GL account.").format(frappe.bold(self.bank_account)))
		# throws when the bank account has no PDC Account Mapping row
		posting.get_account_mapping(self.company, self.bank_account)

	def validate_accounting_details(self, before: dict | None = None):
		"""Default the accounting details of a cheque not posted yet, then check the ones set or changed.

		`before`: the saved values (STALE_CHECK_FIELDS) the defaults may be stale against; read from the
		cheque's own row when not given (a Cheque Batch passes the values of the batch)."""
		self.set_accounting_defaults(before)

		def changed(*fieldnames) -> bool:
			return self.is_new() or any(self.has_value_changed(f) for f in fieldnames)

		if self.cost_center and changed("cost_center", "company"):
			cost_center = frappe.db.get_value(
				"Cost Center", self.cost_center, ["company", "is_group"], as_dict=True
			)
			if not cost_center or cost_center.company != self.company:
				frappe.throw(
					_("Cost Center {0} does not belong to company {1}.").format(
						frappe.bold(self.cost_center), frappe.bold(self.company)
					)
				)
			if cost_center.is_group:
				frappe.throw(_("Cost Center {0} is a group.").format(frappe.bold(self.cost_center)))
		if self.project and changed("project", "company"):
			project_company = frappe.db.get_value("Project", self.project, "company")
			if project_company and project_company != self.company:
				frappe.throw(
					_("Project {0} does not belong to company {1}.").format(
						frappe.bold(self.project), frappe.bold(self.company)
					)
				)
		if self.mode_of_payment and changed("mode_of_payment"):
			if not frappe.db.get_value("Mode of Payment", self.mode_of_payment, "enabled"):
				frappe.throw(_("Mode of Payment {0} is disabled.").format(frappe.bold(self.mode_of_payment)))
		if self.party_bank_account and changed("party_bank_account", "party_type", "party"):
			account = frappe.db.get_value(
				"Bank Account",
				self.party_bank_account,
				["party_type", "party", "is_company_account"],
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
		if self.contact_person and changed("contact_person", "party_type", "party"):
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

	def set_accounting_defaults(self, before: dict | None = None):
		"""Fill the empty accounting details while the cheque is not posted: the company's default cost center
		and dimension defaults, the Mode of Payment mapped to the bank (Treasury Company Settings), the party's
		default bank account and contact. Also called by the lifecycle right before the party posting."""
		if (self.accounting_status or lifecycle.NOT_POSTED) != lifecycle.NOT_POSTED:
			return
		self._drop_stale_defaults(before)
		if not self.cost_center and self.company:
			self.cost_center = erpnext.get_default_cost_center(self.company)
		for fieldname, value in dimensions.default_dimensions(self.company).items():
			if value and self.meta.has_field(fieldname) and not self.get(fieldname):
				self.set(fieldname, value)
		if not self.mode_of_payment:
			self.mode_of_payment = self._mapped_mode(self.bank_account)
		if self.party_type and self.party:
			if not self.party_bank_account:
				self.party_bank_account = get_party_bank_account(self.party_type, self.party)
			if not self.contact_person and self.party_type != "Employee":
				self.contact_person = get_default_contact(self.party_type, self.party)

	def _drop_stale_defaults(self, before: dict | None = None):
		"""A bank or party change drops the Mode of Payment / party details that came with the old ones (a
		value the user chose for the new bank / party is kept)."""
		if before is None:
			if self.is_new():
				return
			before = frappe.db.get_value(self.doctype, self.name, list(STALE_CHECK_FIELDS), as_dict=True)
		if not before:
			return
		before = frappe._dict(before)
		if (
			before.bank_account != self.bank_account
			and self.mode_of_payment
			and self.mode_of_payment == before.mode_of_payment
			and self.mode_of_payment == self._mapped_mode(before.bank_account)
		):
			self.mode_of_payment = None
		if (before.party_type, before.party) != (self.party_type, self.party):
			for fieldname in PARTY_DETAIL_FIELDS:
				if self.get(fieldname) and self.get(fieldname) == before.get(fieldname):
					self.set(fieldname, None)

	def _mapped_mode(self, bank_account: str | None) -> str | None:
		method = payment_modes.CHEQUE_METHOD.get(self.direction)
		if not bank_account or not method:
			return None
		return payment_modes.resolve(self.company, bank_account, method).mode_of_payment

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
		if (
			before
			and before.currency != self.currency
			and flt(before.exchange_rate) == flt(self.exchange_rate)
		):
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
		self.cheque_no = (self.cheque_no or "").strip()
		if not self.cheque_no:
			frappe.throw(_("Cheque No is required."))
		if self.direction != "Outgoing" or self.operational_status in RELEASED_STATUSES:
			return
		if not self.flags.treasury_batch_validation and (
			self.has_value_changed("cheque_no") or self.has_value_changed("bank_account")
		):
			# The books covering the number first (as the allocation does: book, then bank account), so a
			# concurrent Void Leaf / Cancel Leaf of that leaf waits for this save, and the other way round.
			books = lock_covering_books(self.bank_account, self.cheque_no)
			# Serialise concurrent saves of cheque numbers on this bank account until commit, so two
			# transactions cannot both pass the duplicate check below with the same hand-typed number.
			# (Not for the never-inserted cheques a draft Cheque Batch validates its rows with.)
			frappe.db.get_value("Bank Account", self.bank_account, "name", for_update=True)
			if not self.cheque_book:
				self._validate_not_cancelled_leaf(books)
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
		if not duplicate:
			# an outgoing security cheque holds its leaf whatever its status; only the PDC Cheque of its own
			# encashment carries the same number
			duplicate = next(
				(
					name
					for name in frappe.get_all(
						"Security Cheque",
						filters={
							"direction": "Outgoing",
							"bank_account": self.bank_account,
							"cheque_no": self.cheque_no,
						},
						pluck="name",
					)
					if name != self.security_cheque
				),
				None,
			)
		if duplicate:
			frappe.throw(
				_("Cheque No {0} is already used on Bank Account {1} by {2}.").format(
					frappe.bold(self.cheque_no), frappe.bold(self.bank_account), frappe.bold(duplicate)
				)
			)

	def _validate_not_cancelled_leaf(self, books: list[str]):
		"""A hand-typed number (no cheque book) must not be a cancelled / void leaf of a book in use covering it
		(the same rule as `validate_cheque_book` for a cheque of a book)."""
		if not books:
			return
		open_books = frappe.get_all(
			"Cheque Book", filters={"name": ["in", books], "status": ["!=", "Closed"]}, pluck="name"
		)
		if not open_books:
			return
		number = cint(self.cheque_no)
		for row in frappe.get_all(
			"Cheque Book Cancelled Leaf",
			filters={"parenttype": "Cheque Book", "parent": ["in", open_books]},
			fields=["parent", "cheque_no"],
		):
			if str(row.cheque_no or "").strip().isdigit() and cint(row.cheque_no) == number:
				frappe.throw(
					_("Cheque No {0} is a cancelled leaf of Cheque Book {1}.").format(
						frappe.bold(self.cheque_no), frappe.bold(row.parent)
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
		references.validate_references(
			self,
			self.direction,
			_("The total allocated amount cannot be greater than the cheque amount."),
		)

	def validate_locked_fields(self):
		if self.is_new():
			return
		before = self.get_doc_before_save()
		if not before:
			return

		self._validate_printed_fields(before)
		self._validate_batch_locked_fields(before)
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
		if not self.flags.treasury_lifecycle:
			# the vouchers carry them; the lifecycle only fills details a cheque of version 1.0 lacks
			changed += [
				f
				for f in (*ACCOUNTING_FIELDS, *self._dimension_fields())
				if (before.get(f) or None) != (self.get(f) or None)
			]
		if changed:
			frappe.throw(
				_("{0} cannot be changed after the cheque is posted to accounts.").format(
					", ".join(_(self.meta.get_label(f)) for f in changed)
				),
				title=_("Not Allowed"),
			)

	def is_batch_locked(self) -> bool:
		"""True for a cheque of a submitted Cheque Batch (see `_validate_batch_locked_fields`)."""
		return bool(self.cheque_batch) and (
			frappe.db.get_value("Cheque Batch", self.cheque_batch, "docstatus") == 1
		)

	def _validate_batch_locked_fields(self, before):
		"""A cheque of a submitted Cheque Batch is listed on the batch voucher the party signs: its number,
		bank, amount, dates and references change only through the cheque actions (e.g. Replace)."""
		if self.flags.treasury_lifecycle or not self.is_batch_locked():
			return
		changed = [f for f in BATCH_LOCKED_FIELDS if (before.get(f) or None) != (self.get(f) or None)]
		changed += [f for f in BATCH_LOCKED_VALUE_FIELDS if flt(before.get(f), 9) != flt(self.get(f), 9)]
		changed += [f for f in LOCKED_DATE_FIELDS if getdate(before.get(f)) != getdate(self.get(f))]
		if self._references_key(before) != self._references_key(self):
			changed.append("references")
		if changed:
			frappe.throw(
				_(
					"{0} cannot be changed on a cheque of the submitted Cheque Batch {1}. Replace or cancel the cheque instead."
				).format(
					", ".join(_(self.meta.get_label(f)) for f in changed), frappe.bold(self.cheque_batch)
				),
				title=_("Not Allowed"),
			)

	def _validate_printed_fields(self, before):
		"""A printed leaf is what it is: its payee, amount, currency, date, bank and number cannot change by a
		form / REST save, or a later print would show other content than the registered one."""
		if self.flags.treasury_lifecycle or not cint(before.print_count):
			return
		changed = changed_fields(self, before, PRINTED_LOCKED_FIELDS)
		if changed:
			frappe.throw(
				_(
					"{0} cannot be changed after the cheque was printed. Replace or cancel the cheque instead."
				).format(", ".join(_(self.meta.get_label(f)) for f in changed)),
				title=_("Not Allowed"),
			)

	def _dimension_fields(self) -> list[str]:
		return [f for f in dimensions.get_dimension_fieldnames() if self.meta.has_field(f)]

	def _changed_identity_fields(self, before) -> list[str]:
		changed = [f for f in LOCKED_FIELDS if (before.get(f) or None) != (self.get(f) or None)]
		return changed + [f for f in LOCKED_AMOUNT_FIELDS if flt(before.get(f), 9) != flt(self.get(f), 9)]

	@staticmethod
	def _allocation_precision() -> int:
		return references.allocation_precision()

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
	def deliver(self, handover_date=None, remarks=None, allow_low_balance=0):
		return lifecycle.deliver(self.name, handover_date, remarks, cint(allow_low_balance))

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

	@frappe.whitelist(methods=["POST"])
	def settle_discounted(self, settlement_date=None, remarks=None):
		return lifecycle.settle_discounted(self.name, settlement_date, remarks)

	@frappe.whitelist(methods=["POST"])
	def print_cheque(self, language=None, reprint=0, reason=None):
		"""Register the print of the cheque leaf (Cheque Layout of its bank); the form then opens the print."""
		return lifecycle.record_print(self.name, language, cint(reprint), reason)


def on_doctype_update():
	"""Indexes for the PDC reports, number cards and schedulers, which all filter on these columns."""
	frappe.db.add_index("PDC Cheque", ["company", "direction", "operational_status", "effective_due_date"])
