# Copyright (c) 2026, Ahmed Abdelmageed and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint

from treasury.balance_alerts import guard
from treasury.pdc import batch, cheque_printing, lifecycle

# "CHQB" (cheque batch), distinct from the "CHB-" series of Cheque Book
NAMING_SERIES = {"Outgoing": "CHQB-OUT-.YYYY.-.####", "Incoming": "CHQB-IN-.YYYY.-.####"}


class ChequeBatch(Document):
	"""One voucher for many post-dated cheques; see treasury.pdc.batch (it never posts anything itself)."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from treasury.pdc.doctype.cheque_batch_item.cheque_batch_item import ChequeBatchItem

		allocation_method: DF.Literal["Manual", "FIFO Invoices", "Advance (no allocation)"]
		allow_low_balance: DF.Check
		amended_from: DF.Link | None
		bank_account: DF.Link | None
		cheque_book: DF.Link | None
		cheque_count: DF.Int
		cheques: DF.Table[ChequeBatchItem]
		cheques_html: DF.HTML | None
		company: DF.Link
		contact_person: DF.Link | None
		cost_center: DF.Link | None
		currency: DF.Link | None
		direction: DF.Literal["Outgoing", "Incoming"]
		facility_drawdown: DF.Link | None
		gen_amount: DF.Currency
		gen_amount_mode: DF.Literal["Per Cheque", "Split Total"]
		gen_count: DF.Int
		gen_drawer_bank: DF.Data | None
		gen_first_cheque_no: DF.Data | None
		gen_first_due_date: DF.Date | None
		gen_frequency: DF.Literal[
			"Monthly", "Every 2 Months", "Quarterly", "Semi-Annually", "Yearly", "Weekly", "Custom Days"
		]
		gen_interval_days: DF.Int
		gen_month_end: DF.Check
		gen_total: DF.Currency
		handover_date: DF.Date | None
		issue_date: DF.Date
		mode_of_payment: DF.Link | None
		naming_series: DF.Literal["CHQB-OUT-.YYYY.-.####", "CHQB-IN-.YYYY.-.####"]
		party: DF.DynamicLink
		party_bank_account: DF.Link | None
		party_name: DF.Data | None
		party_type: DF.Link
		payment_entries_html: DF.HTML | None
		posting_mode: DF.Literal["", "On Handover", "On Receipt", "On Cheque Date", "On Creation", "Manual"]
		project: DF.Link | None
		purpose: DF.Literal["", "Rent", "Installments", "Supplier Settlement", "Customer Settlement", "Other"]
		remarks: DF.SmallText | None
		source_doctype: DF.Link | None
		source_name: DF.DynamicLink | None
		status: DF.Literal[
			"Draft",
			"Submitted",
			"Partially Delivered",
			"Delivered",
			"Partially Received",
			"Received",
			"Partially Cleared",
			"Cleared",
			"Cancelled",
		]
		total_amount: DF.Currency
	# end: auto-generated types

	# ------------------------------------------------------------------ hooks

	def onload(self):
		if not self.is_new() and self.docstatus > 0:
			self.set_onload("cheques", batch.get_dashboard(self))
			# design 7e P17: "Submitted with a low-balance override by ..." on the form
			self.set_onload("low_balance_override", guard.latest_override(self.doctype, self.name))
			# design 7f P16: printable / printed cheques for the "Print All Cheques" dialog (outgoing only)
			self.set_onload("cheque_print_summary", cheque_printing.batch_print_summary(self))

	def before_insert(self):
		self.naming_series = NAMING_SERIES.get(self.direction, NAMING_SERIES["Outgoing"])
		if self.amended_from:
			batch.clear_cancelled_leaves(self)

	def validate(self):
		guard.reset_override_flag(self)
		batch.validate_batch(self)
		self.status = batch.current_status(self)

	def before_submit(self):
		batch.check_can_create_cheques()
		batch.begin_atomic(self)

	def on_submit(self):
		batch.run_atomic(self, batch.create_cheques)

	def before_cancel(self):
		batch.validate_cancellation(self)
		batch.begin_atomic(self)

	def on_cancel(self):
		batch.run_atomic(self, batch.cancel_cheques)

	# ------------------------------------------------------------------ draft tools
	# POST only: they change data, and Frappe checks the CSRF token only on POST requests.

	@frappe.whitelist(methods=["POST"])
	def generate_rows(self):
		self._check_draft()
		return batch.generate_rows(self)

	@frappe.whitelist(methods=["POST"])
	def allocate_fifo(self):
		self._check_draft()
		return batch.allocate_fifo(self)

	def _check_draft(self):
		self.check_permission("create" if self.is_new() else "write")
		if self.docstatus != 0:
			frappe.throw(_("Only a draft Cheque Batch can be changed."))

	# ------------------------------------------------------------------ actions on the submitted batch
	# Each cheque is acted on through treasury.pdc.lifecycle, which checks the user's permission (and role).

	@frappe.whitelist(methods=["POST"])
	def deliver_all(self, handover_date=None, remarks=None, allow_low_balance=0):
		self.check_permission("read")
		return batch.run_bulk_action(
			self, lifecycle.OUTGOING, handover_date, remarks, allow_low_balance=cint(allow_low_balance)
		)

	@frappe.whitelist(methods=["POST"])
	def receive_all(self, receipt_date=None, remarks=None):
		self.check_permission("read")
		return batch.run_bulk_action(self, lifecycle.INCOMING, receipt_date, remarks)

	@frappe.whitelist()
	def print_all(self):
		self.check_permission("print")
		return batch.get_print_info(self)

	@frappe.whitelist(methods=["POST"])
	def print_cheques(self, language=None, reprint=0, reason=None, only_unprinted=0):
		"""Register the print of the batch's printable cheques (all or nothing), then the form opens the
		"Cheque Batch Leaves" print; without a Cheque Layout, the legacy multi-PDF of the cheques."""
		self.check_permission("print")
		if self.docstatus != 1:
			frappe.throw(_("Submit the Cheque Batch first."))
		if self.direction != lifecycle.OUTGOING:
			frappe.throw(_("This action is only available for {0} cheques.").format(_(lifecycle.OUTGOING)))
		names = [
			c.name
			for c in batch.get_batch_cheques(self.name, as_user=True)
			if c.operational_status in cheque_printing.PRINTABLE_STATUSES
		]
		if not names:
			frappe.throw(_("No cheques to print."))
		layout = cheque_printing.get_layout(self.bank_account)
		if not layout:
			# legacy: no counter without a layout (lifecycle.record_print needs one), the cheques' own format
			return batch.get_print_info(self)
		result = cheque_printing.register_prints(names, language, cint(reprint), reason, cint(only_unprinted))
		return {
			"doctype": self.doctype,
			"name": self.name,
			"print_format": cheque_printing.FORMAT_BATCH,
			"language": cheque_printing.lang_code(language, layout),
			**result,
		}


@frappe.whitelist()
def get_posting_mode(company: str | None = None, direction: str | None = None) -> str | None:
	"""Party posting mode shown on a new batch before it is saved."""
	frappe.has_permission("Cheque Batch", "read", throw=True)
	if not company or not frappe.db.exists("Treasury Company Settings", company):
		return None
	return batch.get_posting_mode(company, direction)
