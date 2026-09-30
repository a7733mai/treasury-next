# Copyright (c) 2026, Ahmed Abdelmageed and contributors
# For license information, please see license.txt

"""PDC Bank Clearing: match unreconciled Bank Transactions with PDC Cheques and Bank Transfers, clear them
and reconcile.

A UI tool (never saved): "Get Suggestions" fills `matches`, "Clear Selected" clears every selected cheque
through the lifecycle on the bank transaction date and reconciles the Bank Transaction against the bank
Journal Entry with ERPNext's own Bank Transaction methods (as `reconcile_vouchers` of the Bank
Reconciliation Tool does). Each row runs in its own savepoint: a failing row is rolled back and reported.

Bank Transfers (Sent): the statement line of the transfer's own bank (its amount with the fee) completes the
transfer and is reconciled against its Payment Entry, which is cleared on the statement date; the deposit on
the target bank of an Internal Transfer is reconciled against the same Payment Entry and logged ("Target
Confirmed"), the transfer's status and clearance date stay as they are.

Bank Charges (spec 7d §3.5): a Posted charge is matched like a transfer (its bank reference, or its posting
date) and reconciled against its voucher. A withdrawal left unmatched whose description / reference contains a
keyword of a Bank Charge Category is proposed as a new charge of that category ("Keyword"): clearing it creates,
submits and reconciles the charge in one step.
"""

import re

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import add_days, cint, date_diff, flt, formatdate, getdate, strip_html

from treasury.accounting import posting
from treasury.bank_transfers import charges
from treasury.bank_transfers import lifecycle as transfer_lifecycle
from treasury.pdc import lifecycle
from treasury.treasury_setup.doctype.bank_charge_category.bank_charge_category import (
	normalise,
	parse_keywords,
)
from treasury.utils.bank_reconciliation import reconcile_voucher  # re-exported (earlier home of the helper)

CHEQUE = "PDC Cheque"
TRANSFER = "Bank Transfer"
BANK_CHARGE = "Bank Charge"
BANK_TRANSACTION = "Bank Transaction"
CHEQUE_ROW, TRANSFER_ROW, CHARGE_ROW = "cheque", "transfer", "charge"
CHARGE_FIELDS = ["name", "entry_type", "amount", "currency", "posting_date", "bank_reference", "category"]
SOURCE_SIDE = "source"
TARGET_SIDE = "target"
TRANSFER_FIELDS = [
	"name",
	"company",
	"transfer_type",
	"status",
	"bank_account",
	"to_bank_account",
	"currency",
	"to_currency",
	"bank_amount",
	"received_amount",
	"execution_date",
	"bank_reference",
	"party_name",
	"payment_entry",
]
ROLES = ("Treasury Manager", "Treasury User", "System Manager")
SAVEPOINT = "treasury_pdc_bank_clearing"
STRONG = "Strong"
WEAK = "Weak"
KEYWORD = "Keyword"
# Cheques that can still be cleared by the bank (Cleared = cleared without bank posting, `Manual` mode).
CLEARABLE = {lifecycle.OUTGOING: ("Delivered",), lifecycle.INCOMING: ("Received", "Deposited")}
OPEN_TRANSACTION_STATUSES = ("Pending", "Unreconciled")
# A bank transaction dated before any of these cannot clear the cheque (lifecycle date order), so it is
# never suggested; the lifecycle still enforces the full rule (voucher dates included) when clearing.
EARLIEST_BANK_DATE_FIELDS = ("issue_date", "handover_date", "deposit_date", "party_posting_date")


class PDCBankClearing(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from treasury.pdc.doctype.pdc_clearing_match.pdc_clearing_match import PDCClearingMatch

		bank_account: DF.Link
		company: DF.Link
		date_tolerance: DF.Int
		from_date: DF.Date | None
		matches: DF.Table[PDCClearingMatch]
		to_date: DF.Date | None
	# end: auto-generated types

	# ------------------------------------------------------------------ suggestions

	@frappe.whitelist()
	def get_suggestions(self):
		"""Fill `matches`: strong (cheque no / transfer or charge reference in the reference / description and
		same amount) first, then weak (same amount, transaction date within `date_tolerance` days of the
		effective due date / execution date / posting date), then the keyword proposals of new bank charges.
		Every bank transaction, cheque, transfer and charge is suggested at most once."""
		self.check_access("read")
		self.validate_filters()
		# Bank Transactions are read without the user's (usual lack of) permission on them, but only those of
		# a bank account the user may read (validate_filters); the cheques, transfers and charges are the ones
		# the user may read.
		transactions = self.get_open_transactions()
		documents = [(CHEQUE, c) for c in self.get_open_cheques()]
		documents += [(TRANSFER, t) for t in self.get_open_transfers()]
		documents += [(BANK_CHARGE, c) for c in self.get_open_bank_charges()]
		tolerance = cint(self.date_tolerance)
		quality_of = {CHEQUE: match_quality, TRANSFER: transfer_match_quality, BANK_CHARGE: charge_match_quality}

		candidates = []
		for bt in transactions:
			for doctype, doc in documents:
				quality = quality_of[doctype](bt, doc, tolerance)
				if quality:
					candidates.append((0 if quality == STRONG else 1, bt, doctype, doc, quality))
		candidates.sort(key=lambda c: (c[0], getdate(c[1].date), c[1].name, c[2], c[3].name))

		self.set("matches", [])
		used_transactions, used_documents = set(), set()
		for _rank, bt, doctype, doc, quality in candidates:
			if bt.name in used_transactions or (doctype, doc.name) in used_documents:
				continue
			used_transactions.add(bt.name)
			used_documents.add((doctype, doc.name))
			row = {
				"selected": 1 if quality == STRONG else 0,
				"match_quality": quality,
				"bank_transaction": bt.name,
				"transaction_date": bt.date,
				"amount": flt(bt.unallocated_amount),
				"currency": bt.currency,
				"reference_doctype": doctype,
			}
			if doctype == CHEQUE:
				row.update(
					cheque=doc.name,
					cheque_no=doc.cheque_no,
					direction=doc.direction,
					party=doc.party_name or doc.party,
					due_date=doc.effective_due_date,
				)
			elif doctype == TRANSFER:
				row.update(
					bank_transfer=doc.name,
					cheque_no=doc.bank_reference,
					direction=doc.direction,
					party=doc.counterparty,
					due_date=doc.execution_date,
				)
			else:
				row.update(
					bank_charge=doc.name,
					bank_charge_category=doc.category,
					cheque_no=doc.bank_reference,
					direction=doc.direction,
					due_date=doc.posting_date,
				)
			self.append("matches", row)

		if frappe.has_permission(BANK_CHARGE, "create"):
			self.add_keyword_proposals([bt for bt in transactions if bt.name not in used_transactions])
		return len(self.matches)

	def add_keyword_proposals(self, transactions: list):
		"""A row "Keyword" (a Bank Charge to create) for each open withdrawal whose description / reference
		contains a keyword of an enabled category: the category with the longest keyword found; a tie between
		categories is proposed but not selected."""
		categories = keyword_categories(self.company)
		if not categories:
			return
		for bt in transactions:
			if flt(bt.withdrawal) <= 0:
				continue
			winners = match_categories(f"{bt.description or ''} {bt.reference_number or ''}", categories)
			if not winners:
				continue
			category = categories[winners[0]]
			ambiguous = len(winners) > 1
			result = None
			if not category.has_account:
				result = _("Set the Expense Account of Bank Charge Category {0} for company {1}.").format(
					winners[0], self.company
				)
			elif ambiguous:
				result = _("Several categories match; choose one.")
			elif not category.auto_select:
				# design 7d P14: e.g. Transfer Fees, whose fee line may already be booked by its transfer
				result = _(
					"Not pre-selected (category setting). Check that the fee is not already booked by its transfer."
				)
			self.append(
				"matches",
				{
					"selected": 1 if not ambiguous and category.auto_select and category.has_account else 0,
					"match_quality": KEYWORD,
					"bank_transaction": bt.name,
					"transaction_date": bt.date,
					"amount": flt(bt.unallocated_amount),
					"currency": bt.currency,
					"reference_doctype": BANK_CHARGE,
					"bank_charge_category": winners[0],
					"direction": lifecycle.OUTGOING,
					"due_date": bt.date,
					"description": bt.description,
					"result": result,
				},
			)

	def validate_filters(self):
		if not self.company or not self.bank_account:
			frappe.throw(_("Company and Bank Account are required."))
		# role permissions and User Permissions of the user (e.g. restricted to another company)
		frappe.has_permission("Company", "read", doc=self.company, throw=True)
		frappe.has_permission("Bank Account", "read", doc=self.bank_account, throw=True)
		company = frappe.db.get_value("Bank Account", self.bank_account, "company")
		if company != self.company:
			frappe.throw(
				_("Bank Account {0} does not belong to company {1}.").format(
					frappe.bold(self.bank_account), frappe.bold(self.company)
				)
			)
		if self.from_date and self.to_date and getdate(self.from_date) > getdate(self.to_date):
			frappe.throw(_("From Date cannot be after To Date."))

	def get_open_transactions(self) -> list[frappe._dict]:
		filters = {
			"docstatus": 1,
			"bank_account": self.bank_account,
			"status": ["in", OPEN_TRANSACTION_STATUSES],
			"unallocated_amount": [">", 0],
		}
		if self.from_date and self.to_date:
			filters["date"] = ["between", [self.from_date, self.to_date]]
		elif self.from_date:
			filters["date"] = [">=", self.from_date]
		elif self.to_date:
			filters["date"] = ["<=", self.to_date]
		return frappe.get_all(
			BANK_TRANSACTION,
			filters=filters,
			fields=[
				"name",
				"date",
				"deposit",
				"withdrawal",
				"unallocated_amount",
				"currency",
				"reference_number",
				"description",
			],
			order_by="date asc, name asc",
		)

	def get_open_cheques(self) -> list[frappe._dict]:
		"""Open cheques of the bank account that the user may read."""
		fields = [
			"name",
			"direction",
			"cheque_no",
			"party",
			"party_name",
			"amount",
			"currency",
			"effective_due_date",
			"operational_status",
			"accounting_status",
			*EARLIEST_BANK_DATE_FIELDS,
		]
		cheques = []
		for direction, statuses in CLEARABLE.items():
			cheques += frappe.get_list(
				CHEQUE,
				filters={
					"company": self.company,
					"bank_account": self.bank_account,
					"direction": direction,
					"operational_status": ["in", statuses],
				},
				fields=fields,
				limit_page_length=0,
			)
		# cleared without bank posting (`Manual` bank posting mode)
		cheques += frappe.get_list(
			CHEQUE,
			filters={
				"company": self.company,
				"bank_account": self.bank_account,
				"operational_status": "Cleared",
				"accounting_status": ["not in", (lifecycle.BANK_POSTED, lifecycle.REVERSED)],
			},
			fields=fields,
			limit_page_length=0,
		)
		return cheques

	def get_open_transfers(self) -> list[frappe._dict]:
		"""Sent transfers of the bank account (their source side), and the Sent / Completed Internal Transfers
		to it whose target side is not confirmed yet, that the user may read. Each gets the `side`, the
		statement `direction` / `expected_amount` / `expected_currency` and the `counterparty`."""
		if not frappe.has_permission(TRANSFER, "read"):
			return []
		base = {"company": self.company, "docstatus": 1}
		transfers = []
		for row in frappe.get_list(
			TRANSFER,
			filters={**base, "status": transfer_lifecycle.SENT, "bank_account": self.bank_account},
			fields=TRANSFER_FIELDS,
			limit_page_length=0,
		):
			transfers.append(transfer_side(row, SOURCE_SIDE))
		targets = frappe.get_list(
			TRANSFER,
			filters={
				**base,
				"transfer_type": transfer_lifecycle.INTERNAL,
				"status": ["in", (transfer_lifecycle.SENT, transfer_lifecycle.COMPLETED)],
				"to_bank_account": self.bank_account,
			},
			fields=TRANSFER_FIELDS,
			limit_page_length=0,
		)
		confirmed = transfer_lifecycle.target_confirmed([row.name for row in targets])
		transfers += [transfer_side(row, TARGET_SIDE) for row in targets if row.name not in confirmed]
		return transfers

	def get_open_bank_charges(self) -> list[frappe._dict]:
		"""Posted (not yet reconciled) charges and refunds of the bank account that the user may read, with the
		statement `direction` (a charge is a withdrawal, a refund a deposit)."""
		if not frappe.has_permission(BANK_CHARGE, "read"):
			return []
		rows = frappe.get_list(
			BANK_CHARGE,
			filters={
				"company": self.company,
				"bank_account": self.bank_account,
				"docstatus": 1,
				"status": charges.POSTED,
			},
			fields=CHARGE_FIELDS,
			limit_page_length=0,
		)
		for row in rows:
			row.direction = lifecycle.OUTGOING if row.entry_type == charges.CHARGE else lifecycle.INCOMING
		return rows

	# ------------------------------------------------------------------ clearing

	@frappe.whitelist(methods=["POST"])
	def clear_selected(self) -> dict:
		"""Clear the selected rows. Returns {"cleared": [cheques, transfers and reconciled charges], "created":
		[bank charges created], "skipped": [{"cheque", "bank_transfer", "bank_charge", "document", "reason"}]}.
		The permissions on Bank Transfer / Bank Charge are checked per row (a row the user may not change is
		skipped)."""
		self.check_access("write")
		selected = [row for row in self.matches if cint(row.selected)]
		if any(row_kind(row) == CHEQUE_ROW for row in selected):
			frappe.has_permission(CHEQUE, "write", throw=True)
		if not set(ROLES) & set(frappe.get_roles()):
			frappe.throw(
				_("This action requires one of the roles: {0}.").format(", ".join(_(r) for r in ROLES)),
				frappe.PermissionError,
				title=_("Not Permitted"),
			)
		self.validate_filters()

		result = {"cleared": [], "created": [], "skipped": []}
		for row in selected:
			kind = row_kind(row)
			creates = kind == CHARGE_ROW and not row.get("bank_charge")
			reason = self.clear_row(row)
			document = {CHEQUE_ROW: row.cheque, TRANSFER_ROW: row.get("bank_transfer")}.get(
				kind, row.get("bank_charge")
			)
			if reason:
				row.result = reason
				result["skipped"].append(
					{
						"cheque": row.cheque,
						"bank_transfer": row.get("bank_transfer"),
						"bank_charge": row.get("bank_charge"),
						"document": document or row.bank_transaction,
						"reason": reason,
					}
				)
			elif creates:
				row.result = _("Bank Charge {0} created and reconciled").format(row.bank_charge)
				row.selected = 0
				result["created"].append(row.bank_charge)
			else:
				row.result = _("Cleared and reconciled")
				row.selected = 0
				result["cleared"].append(document)

		frappe.clear_messages()
		frappe.msgprint(
			self.summary_message(result),
			title=_("PDC Bank Clearing"),
			indicator="green" if not result["skipped"] else "orange",
		)
		return result

	def check_access(self, ptype: str):
		if not frappe.has_permission(self.doctype, ptype):
			frappe.throw(
				_("You are not permitted to use the PDC Bank Clearing tool."), frappe.PermissionError
			)

	def clear_row(self, row) -> str | None:
		"""Clear and reconcile one match in its own savepoint. Returns the reason it was skipped, if so."""
		kind = row_kind(row)
		bank_charge = row.get("bank_charge")
		frappe.db.savepoint(SAVEPOINT)
		try:
			if kind == TRANSFER_ROW:
				self.clear_transfer(row)
			elif kind == CHARGE_ROW:
				self.clear_charge(row)
			else:
				self.validate_row(row)
				self.clear_cheque(row)
				self.reconcile(row)
		except Exception as e:
			frappe.db.rollback(save_point=SAVEPOINT)
			row.bank_charge = bank_charge  # a charge created in the rolled back savepoint no longer exists
			# expected: business rules and the user's permission on the document; anything else is logged
			if not isinstance(e, frappe.ValidationError | frappe.PermissionError):
				if kind == TRANSFER_ROW:
					title = _("PDC Bank Clearing failed for Bank Transfer {0}").format(row.bank_transfer)
				elif kind == CHARGE_ROW:
					title = _("PDC Bank Clearing failed for Bank Transaction {0}").format(row.bank_transaction)
				else:
					title = _("PDC Bank Clearing failed for cheque {0}").format(row.cheque)
				frappe.log_error(title=title)
			return strip_html(str(e)) or _("Could not be cleared.")
		frappe.db.release_savepoint(SAVEPOINT)
		return None

	def clear_charge(self, row):
		"""An existing (Posted) charge is reconciled with the statement line; a keyword proposal creates,
		submits and reconciles a charge of the chosen category. The user's own Bank Charge permissions apply."""
		if not row.bank_transaction:
			frappe.throw(_("The bank transaction no longer exists."))
		if row.get("bank_charge"):
			charge = frappe.db.get_value(BANK_CHARGE, row.bank_charge, ["company", "bank_account"], as_dict=True)
			if not charge or (charge.company, charge.bank_account) != (self.company, self.bank_account):
				frappe.throw(
					_("Bank Charge {0} does not belong to Bank Account {1}.").format(
						row.bank_charge, self.bank_account
					)
				)
			charges.reconcile(row.bank_charge, row.bank_transaction)
		else:
			if not row.get("bank_charge_category"):
				frappe.throw(_("Choose a Charge Category."))
			row.bank_charge = charges.create_from_bank_transaction(
				row.bank_transaction, row.bank_charge_category, self.company, self.bank_account
			)
		row.transaction_date = frappe.db.get_value(BANK_TRANSACTION, row.bank_transaction, "date")

	def validate_row(self, row):
		if not row.cheque or not row.bank_transaction:
			frappe.throw(_("The cheque or the bank transaction no longer exists."))
		cheque_fields = [
			"company",
			"bank_account",
			"currency",
			"amount",
			"direction",
			"bank_transaction_date",
		]
		cheque = frappe.db.get_value(
			CHEQUE, row.cheque, [*cheque_fields, "operational_status", "accounting_status"], as_dict=True
		)
		bt_fields = ["docstatus", "bank_account", "currency", "unallocated_amount", "deposit", "withdrawal"]
		bt = frappe.db.get_value(BANK_TRANSACTION, row.bank_transaction, [*bt_fields, "date"], as_dict=True)
		if not cheque or not bt:
			frappe.throw(_("The cheque or the bank transaction no longer exists."))
		open_statuses = (*CLEARABLE[cheque.direction], "Cleared")
		if (
			cheque.accounting_status == lifecycle.BANK_POSTED
			or cheque.operational_status not in open_statuses
		):
			frappe.throw(_("Cheque {0} is already cleared or no longer open.").format(row.cheque))
		if cheque.company != self.company or cheque.bank_account != self.bank_account:
			frappe.throw(
				_("Cheque {0} does not belong to Bank Account {1}.").format(row.cheque, self.bank_account)
			)
		if bt.docstatus != 1 or bt.bank_account != self.bank_account or flt(bt.unallocated_amount) <= 0:
			frappe.throw(
				_("Bank Transaction {0} is not an open transaction of this bank account.").format(
					row.bank_transaction
				)
			)
		if bt.currency and bt.currency != cheque.currency:
			frappe.throw(
				_("Bank Transaction {0} is in {1} but cheque {2} is in {3}.").format(
					row.bank_transaction, bt.currency, row.cheque, cheque.currency
				)
			)
		expected_side = "withdrawal" if cheque.direction == lifecycle.OUTGOING else "deposit"
		precision = frappe.get_precision(BANK_TRANSACTION, "unallocated_amount")
		if flt(bt.get(expected_side), precision) <= 0 or flt(bt.unallocated_amount, precision) != flt(
			cheque.amount, precision
		):
			frappe.throw(
				_("The open amount of Bank Transaction {0} does not match cheque {1}.").format(
					row.bank_transaction, row.cheque
				)
			)
		if cheque.bank_transaction_date and getdate(cheque.bank_transaction_date) != getdate(bt.date):
			# a cheque cleared (Manual mode) with its own bank transaction date: the bank JE, the cheque and
			# the reconciled clearance date would disagree
			frappe.throw(
				_(
					"Cheque {0} was cleared with bank transaction date {1}, but Bank Transaction {2} is dated {3}."
				).format(
					row.cheque,
					formatdate(cheque.bank_transaction_date),
					row.bank_transaction,
					formatdate(bt.date),
				)
			)
		row.transaction_date = bt.date

	def clear_cheque(self, row):
		"""Clear the cheque on the bank transaction date; the lifecycle dates the bank JE by the settings.

		A cheque already cleared without a bank posting (`Manual` mode) — or cleared now in that mode — is
		posted to the bank here: confirming the match in this tool is the user's explicit bank posting."""
		date = getdate(row.transaction_date)
		status = frappe.db.get_value(CHEQUE, row.cheque, "operational_status")
		if status != "Cleared":
			lifecycle.clear(row.cheque, clearance_date=date, bank_transaction_date=date)
		if frappe.db.get_value(CHEQUE, row.cheque, "accounting_status") != lifecycle.BANK_POSTED:
			lifecycle.post_bank(row.cheque, posting_date=date, bank_transaction_date=date)

	def reconcile(self, row):
		"""Reconcile the Bank Transaction against the bank voucher of the cheque — its bank Payment Entry, or
		its bank Journal Entry (Journal Entry mode / version 1.0) — with ERPNext's BankTransaction methods, in
		the order `bank_reconciliation_tool.reconcile_vouchers` calls them."""
		vouchers = frappe.db.get_value(
			CHEQUE, row.cheque, ["bank_payment_entry", "bank_journal"], as_dict=True
		)
		if vouchers and vouchers.bank_payment_entry:
			voucher = ("Payment Entry", vouchers.bank_payment_entry)
		elif vouchers and vouchers.bank_journal:
			voucher = ("Journal Entry", vouchers.bank_journal)
		else:
			frappe.throw(_("Cheque {0} has no bank voucher to reconcile.").format(row.cheque))
		reconcile_voucher(row.bank_transaction, *voucher)

	# ------------------------------------------------------------------ bank transfers

	def clear_transfer(self, row):
		"""Source side: complete the transfer on the statement date, reconcile the statement line against its
		Payment Entry and clear the Payment Entry on that date (ERPNext does not clear a voucher with a second
		bank row, e.g. an Internal Transfer). Target side: reconcile and log; the clearance date stays."""
		if not frappe.has_permission(TRANSFER, "write"):
			frappe.throw(_("You are not permitted to change Bank Transfers."), frappe.PermissionError)
		transfer, bt = self.validate_transfer_row(row)
		date = getdate(bt.date)
		if transfer.side == SOURCE_SIDE:
			transfer_lifecycle.complete(transfer.name, completion_date=date, bank_transaction_date=date)
			reconcile_voucher(row.bank_transaction, "Payment Entry", transfer.payment_entry)
			posting.set_clearance_date("Payment Entry", transfer.payment_entry, date)
			return
		# the target side must not change the date the transfer was cleared on (set by its source side)
		clearance_date = frappe.db.get_value("Payment Entry", transfer.payment_entry, "clearance_date")
		reconcile_voucher(row.bank_transaction, "Payment Entry", transfer.payment_entry)
		if frappe.db.get_value("Payment Entry", transfer.payment_entry, "clearance_date") != clearance_date:
			posting.set_clearance_date("Payment Entry", transfer.payment_entry, clearance_date)
		transfer_lifecycle.confirm_target(transfer.name, date)

	def validate_transfer_row(self, row) -> tuple[frappe._dict, frappe._dict]:
		"""The transfer (with its side on this bank account) and the Bank Transaction of a row, still open and
		matching: bank, currency, direction and open amount, not dated before the execution date."""
		transfer = row.bank_transfer and frappe.db.get_value(
			TRANSFER, row.bank_transfer, TRANSFER_FIELDS, as_dict=True
		)
		bt_fields = ["docstatus", "bank_account", "currency", "unallocated_amount", "deposit", "withdrawal"]
		bt = row.bank_transaction and frappe.db.get_value(
			BANK_TRANSACTION, row.bank_transaction, [*bt_fields, "date"], as_dict=True
		)
		if not transfer or not bt:
			frappe.throw(_("The bank transfer or the bank transaction no longer exists."))
		side = None
		if transfer.company == self.company and transfer.bank_account == self.bank_account:
			side = SOURCE_SIDE
		elif (
			transfer.company == self.company
			and transfer.transfer_type == transfer_lifecycle.INTERNAL
			and transfer.to_bank_account == self.bank_account
		):
			side = TARGET_SIDE
		if not side:
			frappe.throw(
				_("Bank Transfer {0} does not belong to Bank Account {1}.").format(
					row.bank_transfer, self.bank_account
				)
			)
		transfer = transfer_side(transfer, side)
		if side == SOURCE_SIDE:
			is_open = transfer.status == transfer_lifecycle.SENT
		else:
			is_open = transfer.status in (
				transfer_lifecycle.SENT,
				transfer_lifecycle.COMPLETED,
			) and not transfer_lifecycle.target_confirmed([transfer.name])
		if not is_open or not transfer.payment_entry:
			frappe.throw(
				_("Bank Transfer {0} is already completed or no longer open.").format(row.bank_transfer)
			)
		if bt.docstatus != 1 or bt.bank_account != self.bank_account or flt(bt.unallocated_amount) <= 0:
			frappe.throw(
				_("Bank Transaction {0} is not an open transaction of this bank account.").format(
					row.bank_transaction
				)
			)
		if bt.currency and bt.currency != transfer.expected_currency:
			frappe.throw(
				_("Bank Transaction {0} is in {1} but Bank Transfer {2} is in {3}.").format(
					row.bank_transaction, bt.currency, row.bank_transfer, transfer.expected_currency
				)
			)
		precision = frappe.get_precision(BANK_TRANSACTION, "unallocated_amount")
		side = "withdrawal" if transfer.direction == lifecycle.OUTGOING else "deposit"
		if flt(bt.get(side), precision) <= 0 or flt(bt.unallocated_amount, precision) != flt(
			transfer.expected_amount, precision
		):
			frappe.throw(
				_("The open amount of Bank Transaction {0} does not match Bank Transfer {1}.").format(
					row.bank_transaction, row.bank_transfer
				)
			)
		if getdate(bt.date) < getdate(transfer.execution_date):
			frappe.throw(
				_("Bank Transaction {0} is dated before the execution date of Bank Transfer {1}.").format(
					row.bank_transaction, row.bank_transfer
				)
			)
		row.transaction_date = bt.date
		return transfer, bt

	@staticmethod
	def summary_message(result: dict) -> str:
		lines = [_("{0} document(s) cleared and reconciled.").format(len(result["cleared"]))]
		if result.get("created"):
			lines.append(_("{0} bank charge(s) created.").format(len(result["created"])))
		if result["skipped"]:
			lines.append(_("{0} row(s) skipped:").format(len(result["skipped"])))
			lines += [
				f"{frappe.utils.escape_html(d.get('document') or d.get('cheque') or '')}: "
				f"{frappe.utils.escape_html(d['reason'])}"
				for d in result["skipped"]
			]
		return "<br>".join(lines)


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


def row_kind(row) -> str:
	""""cheque", "transfer" or "charge" from the row's document type (rows without one: by their links)."""
	doctype = row.get("reference_doctype")
	if doctype == TRANSFER or (doctype != BANK_CHARGE and row.get("bank_transfer") and not row.get("cheque")):
		return TRANSFER_ROW
	if doctype == BANK_CHARGE or (row.get("bank_charge") and not row.get("cheque")):
		return CHARGE_ROW
	return CHEQUE_ROW


def charge_match_quality(bt, charge, tolerance_days: int) -> str | None:
	"""Strong: the open amount on the right side (a charge is a withdrawal, a refund a deposit) and the charge's
	bank reference (or name) in the reference number / description. Weak: same amount, within
	`tolerance_days` of the posting date."""
	side = flt(bt.withdrawal) if charge.entry_type == charges.CHARGE else flt(bt.deposit)
	if side <= 0 or (bt.currency and charge.currency and bt.currency != charge.currency):
		return None
	precision = frappe.get_precision(BANK_TRANSACTION, "unallocated_amount")
	if flt(bt.unallocated_amount, precision) != flt(charge.amount, precision):
		return None
	reference = charge.bank_reference or charge.name
	if cheque_no_in(reference, bt.reference_number) or cheque_no_in(reference, bt.description):
		return STRONG
	if abs(date_diff(bt.date, charge.posting_date)) <= tolerance_days:
		return WEAK
	return None


def keyword_categories(company: str) -> dict[str, frappe._dict]:
	"""{category: {keywords, auto_select, has_account}} of the enabled categories that have keywords."""
	rows = frappe.get_all(
		"Bank Charge Category",
		filters={"disabled": 0, "keywords": ["is", "set"]},
		fields=["name", "keywords", "auto_select_proposals"],
	)
	if not rows:
		return {}
	with_account = set(
		frappe.get_all(
			"Bank Charge Category Account",
			filters={"parenttype": "Bank Charge Category", "company": company},
			pluck="parent",
		)
	)
	categories = {}
	for row in rows:
		keywords = parse_keywords(row.keywords)
		if keywords:
			categories[row.name] = frappe._dict(
				keywords=keywords,
				auto_select=cint(row.auto_select_proposals),
				has_account=row.name in with_account,
			)
	return categories


def match_categories(text: str, categories: dict) -> list[str]:
	"""The categories whose longest keyword found in `text` is the longest of all (several: a tie), sorted."""
	text = normalise(text)
	best, winners = 0, []
	for name, category in categories.items():
		length = max((len(k) for k in category.keywords if k in text), default=0)
		if not length or length < best:
			continue
		if length > best:
			best, winners = length, []
		winners.append(name)
	return sorted(winners)


def match_quality(bt, cheque, tolerance_days: int) -> str | None:
	"""Strong: same amount and the cheque number appears in the reference number / description.
	Weak: same amount, on the right side (withdrawal for outgoing), within `tolerance_days` of the due date.
	Never a transaction dated before the cheque's issue / handover / deposit / party posting date."""
	side = flt(bt.withdrawal) if cheque.direction == lifecycle.OUTGOING else flt(bt.deposit)
	if side <= 0 or (bt.currency and cheque.currency and bt.currency != cheque.currency):
		return None
	earliest = [getdate(cheque.get(f)) for f in EARLIEST_BANK_DATE_FIELDS if cheque.get(f)]
	if earliest and getdate(bt.date) < max(earliest):
		return None
	precision = frappe.get_precision(BANK_TRANSACTION, "unallocated_amount")
	if flt(bt.unallocated_amount, precision) != flt(cheque.amount, precision):
		return None
	if cheque_no_in(cheque.cheque_no, bt.reference_number) or cheque_no_in(cheque.cheque_no, bt.description):
		return STRONG
	due = getdate(cheque.effective_due_date)
	if add_days(due, -tolerance_days) <= getdate(bt.date) <= add_days(due, tolerance_days):
		return WEAK
	return None


def is_transfer_row(row) -> bool:
	return row_kind(row) == TRANSFER_ROW


def transfer_side(transfer, side: str) -> frappe._dict:
	"""The statement line a transfer leaves on one of its banks: the source bank pays the amount with the fee
	(a receipt: receives the amount less the fee), the target bank of an Internal Transfer receives the
	received amount."""
	transfer = frappe._dict(transfer)
	transfer.side = side
	if side == SOURCE_SIDE:
		incoming = transfer.transfer_type == transfer_lifecycle.PARTY_RECEIPT
		transfer.direction = lifecycle.INCOMING if incoming else lifecycle.OUTGOING
		transfer.expected_amount = flt(transfer.bank_amount)
		transfer.expected_currency = transfer.currency
		transfer.counterparty = transfer.party_name or transfer.to_bank_account
	else:
		transfer.direction = lifecycle.INCOMING
		transfer.expected_amount = flt(transfer.received_amount)
		transfer.expected_currency = transfer.to_currency
		transfer.counterparty = transfer.bank_account
	return transfer


def transfer_match_quality(bt, transfer, tolerance_days: int) -> str | None:
	"""Strong: the statement amount on the right side and the transfer's bank reference (or name) in the
	reference number / description. Weak: same amount, within `tolerance_days` of the execution date. Never a
	transaction dated before the execution date."""
	side = flt(bt.withdrawal) if transfer.direction == lifecycle.OUTGOING else flt(bt.deposit)
	if side <= 0:
		return None
	if bt.currency and transfer.expected_currency and bt.currency != transfer.expected_currency:
		return None
	execution = getdate(transfer.execution_date)
	if getdate(bt.date) < execution:
		return None
	precision = frappe.get_precision(BANK_TRANSACTION, "unallocated_amount")
	if flt(bt.unallocated_amount, precision) != flt(transfer.expected_amount, precision):
		return None
	for reference in (transfer.bank_reference, transfer.name):
		if cheque_no_in(reference, bt.reference_number) or cheque_no_in(reference, bt.description):
			return STRONG
	if getdate(bt.date) <= add_days(execution, tolerance_days):
		return WEAK
	return None


def cheque_no_in(cheque_no: str | None, text: str | None) -> bool:
	"""The cheque number appears in `text` as a whole token (not inside a longer number)."""
	if not cheque_no or not text:
		return False
	pattern = r"(?<![0-9A-Za-z])" + re.escape(cheque_no.strip()) + r"(?![0-9A-Za-z])"
	return bool(re.search(pattern, text, flags=re.IGNORECASE))
