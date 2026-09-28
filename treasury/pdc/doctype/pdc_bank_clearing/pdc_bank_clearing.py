# Copyright (c) 2026, Ahmed Abdelmageed and contributors
# For license information, please see license.txt

"""PDC Bank Clearing: match unreconciled Bank Transactions with PDC Cheques, clear them and reconcile.

A UI tool (never saved): "Get Suggestions" fills `matches`, "Clear Selected" clears every selected cheque
through the lifecycle on the bank transaction date and reconciles the Bank Transaction against the bank
Journal Entry with ERPNext's own Bank Transaction methods (as `reconcile_vouchers` of the Bank
Reconciliation Tool does). Each row runs in its own savepoint: a failing row is rolled back and reported.
"""

import re

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import add_days, cint, flt, getdate, strip_html

from treasury.pdc import lifecycle

CHEQUE = "PDC Cheque"
BANK_TRANSACTION = "Bank Transaction"
ROLES = ("Treasury Manager", "Treasury User", "System Manager")
SAVEPOINT = "treasury_pdc_bank_clearing"
STRONG = "Strong"
WEAK = "Weak"
# Cheques that can still be cleared by the bank (Cleared = cleared without bank posting, `Manual` mode).
CLEARABLE = {lifecycle.OUTGOING: ("Delivered",), lifecycle.INCOMING: ("Received", "Deposited")}
OPEN_TRANSACTION_STATUSES = ("Pending", "Unreconciled")


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
		"""Fill `matches`: strong (cheque no in the reference / description and same amount) first, then weak
		(same amount, transaction date within `date_tolerance` days of the effective due date). Every bank
		transaction and every cheque is suggested at most once."""
		self.check_access("read")
		self.validate_filters()
		transactions = self.get_open_transactions()
		cheques = self.get_open_cheques()

		candidates = []
		for bt in transactions:
			for cheque in cheques:
				quality = match_quality(bt, cheque, cint(self.date_tolerance))
				if quality:
					candidates.append((0 if quality == STRONG else 1, bt, cheque, quality))
		candidates.sort(key=lambda c: (c[0], getdate(c[1].date), c[1].name, c[2].name))

		self.set("matches", [])
		used_transactions, used_cheques = set(), set()
		for _rank, bt, cheque, quality in candidates:
			if bt.name in used_transactions or cheque.name in used_cheques:
				continue
			used_transactions.add(bt.name)
			used_cheques.add(cheque.name)
			self.append(
				"matches",
				{
					"select": 1 if quality == STRONG else 0,
					"match_quality": quality,
					"bank_transaction": bt.name,
					"transaction_date": bt.date,
					"amount": flt(bt.unallocated_amount),
					"currency": bt.currency,
					"cheque": cheque.name,
					"cheque_no": cheque.cheque_no,
					"direction": cheque.direction,
					"party": cheque.party_name or cheque.party,
					"due_date": cheque.effective_due_date,
				},
			)
		return len(self.matches)

	def validate_filters(self):
		if not self.company or not self.bank_account:
			frappe.throw(_("Company and Bank Account are required."))
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
		]
		cheques = []
		for direction, statuses in CLEARABLE.items():
			cheques += frappe.get_all(
				CHEQUE,
				filters={
					"company": self.company,
					"bank_account": self.bank_account,
					"direction": direction,
					"operational_status": ["in", statuses],
				},
				fields=fields,
			)
		# cleared without bank posting (`Manual` bank posting mode)
		cheques += frappe.get_all(
			CHEQUE,
			filters={
				"company": self.company,
				"bank_account": self.bank_account,
				"operational_status": "Cleared",
				"accounting_status": ["not in", (lifecycle.BANK_POSTED, lifecycle.REVERSED)],
			},
			fields=fields,
		)
		return cheques

	# ------------------------------------------------------------------ clearing

	@frappe.whitelist(methods=["POST"])
	def clear_selected(self) -> dict:
		"""Clear the selected rows. Returns {"cleared": [cheques], "skipped": [{"cheque", "reason"}]}."""
		self.check_access("write")
		frappe.has_permission(CHEQUE, "write", throw=True)
		if not set(ROLES) & set(frappe.get_roles()):
			frappe.throw(
				_("This action requires one of the roles: {0}.").format(", ".join(_(r) for r in ROLES)),
				frappe.PermissionError,
				title=_("Not Permitted"),
			)
		self.validate_filters()

		result = {"cleared": [], "skipped": []}
		for row in self.matches:
			if not cint(row.select):
				continue
			reason = self.clear_row(row)
			if reason:
				row.result = reason
				result["skipped"].append({"cheque": row.cheque, "reason": reason})
			else:
				row.result = _("Cleared and reconciled")
				row.select = 0
				result["cleared"].append(row.cheque)

		frappe.clear_messages()
		frappe.msgprint(
			self.summary_message(result),
			title=_("PDC Bank Clearing"),
			indicator="green" if not result["skipped"] else "orange",
		)
		return result

	def check_access(self, ptype: str):
		if not frappe.has_permission(self.doctype, ptype):
			frappe.throw(_("Not permitted"), frappe.PermissionError)

	def clear_row(self, row) -> str | None:
		"""Clear and reconcile one match in its own savepoint. Returns the reason it was skipped, if so."""
		frappe.db.savepoint(SAVEPOINT)
		try:
			self.validate_row(row)
			self.clear_cheque(row)
			self.reconcile(row)
		except Exception as e:
			frappe.db.rollback(save_point=SAVEPOINT)
			if not isinstance(e, frappe.ValidationError):
				frappe.log_error(title=_("PDC Bank Clearing failed for cheque {0}").format(row.cheque))
			return strip_html(str(e)) or _("Could not be cleared.")
		frappe.db.release_savepoint(SAVEPOINT)
		return None

	def validate_row(self, row):
		if not row.cheque or not row.bank_transaction:
			frappe.throw(_("The cheque or the bank transaction no longer exists."))
		cheque_fields = ["company", "bank_account", "currency", "amount", "direction"]
		cheque = frappe.db.get_value(
			CHEQUE, row.cheque, [*cheque_fields, "operational_status", "accounting_status"], as_dict=True
		)
		bt_fields = ["docstatus", "bank_account", "currency", "unallocated_amount", "deposit", "withdrawal"]
		bt = frappe.db.get_value(BANK_TRANSACTION, row.bank_transaction, [*bt_fields, "date"], as_dict=True)
		if not cheque or not bt:
			frappe.throw(_("The cheque or the bank transaction no longer exists."))
		open_statuses = (*CLEARABLE[cheque.direction], "Cleared")
		if cheque.accounting_status == lifecycle.BANK_POSTED or cheque.operational_status not in open_statuses:
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
		"""Reconcile the Bank Transaction against the bank Journal Entry of the cheque (ERPNext's
		BankTransaction methods, in the order `bank_reconciliation_tool.reconcile_vouchers` calls them)."""
		journal = frappe.db.get_value(CHEQUE, row.cheque, "bank_journal")
		if not journal:
			frappe.throw(_("Cheque {0} has no bank Journal Entry to reconcile.").format(row.cheque))
		transaction = frappe.get_doc(BANK_TRANSACTION, row.bank_transaction)
		transaction.add_payment_entries([{"payment_doctype": "Journal Entry", "payment_name": journal}])
		transaction.validate_duplicate_references()
		transaction.allocate_payment_entries()
		transaction.update_allocated_amount()
		transaction.set_status()
		# the user's right to clear the cheque was checked above; Treasury users need not hold Bank
		# Transaction permissions (like the vouchers of treasury.accounting.posting)
		transaction.flags.ignore_permissions = True
		transaction.save()

	@staticmethod
	def summary_message(result: dict) -> str:
		lines = [_("{0} cheque(s) cleared and reconciled.").format(len(result["cleared"]))]
		if result["skipped"]:
			lines.append(_("{0} row(s) skipped:").format(len(result["skipped"])))
			lines += [
				f"{frappe.utils.escape_html(d['cheque'] or '')}: {frappe.utils.escape_html(d['reason'])}"
				for d in result["skipped"]
			]
		return "<br>".join(lines)


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


def match_quality(bt, cheque, tolerance_days: int) -> str | None:
	"""Strong: same amount and the cheque number appears in the reference number / description.
	Weak: same amount, on the right side (withdrawal for outgoing), within `tolerance_days` of the due date."""
	side = flt(bt.withdrawal) if cheque.direction == lifecycle.OUTGOING else flt(bt.deposit)
	if side <= 0 or (bt.currency and cheque.currency and bt.currency != cheque.currency):
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


def cheque_no_in(cheque_no: str | None, text: str | None) -> bool:
	"""The cheque number appears in `text` as a whole token (not inside a longer number)."""
	if not cheque_no or not text:
		return False
	pattern = r"(?<![0-9A-Za-z])" + re.escape(cheque_no.strip()) + r"(?![0-9A-Za-z])"
	return bool(re.search(pattern, text, flags=re.IGNORECASE))
