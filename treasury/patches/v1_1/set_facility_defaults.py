"""Phase 7g: the defaults of the new Bank Facilities fields on the documents saved before them.

Idempotent: only empty values are set.
- Bank Facility: lender type Bank, late payment penalties paid to the lender, the lender's name (the Bank's
  name), Islamic when a Murabaha / Tawarruq sub-limit exists;
- Facility Drawdown: repayment mode Manual, disbursed to the bank account, the lender type / name and the
  Islamic flag fetched from its facility;
- Facility Repayment: settled by the bank;
- Facility Repayment Schedule: the payment voucher is a Journal Entry (`payment_journal` became a Dynamic
  Link on `payment_voucher_type`; the column keeps its values);
- Treasury Company Settings: facility vouchers are Payment Entries, auto debits posted on the due date.
"""

import frappe
from frappe.query_builder.functions import IfNull


def _fill(doctype: str, values: dict):
	table = frappe.qb.DocType(doctype)
	for fieldname, value in values.items():
		field = getattr(table, fieldname)
		frappe.qb.update(table).set(field, value).where(IfNull(field, "") == "").run()


def _fetch_drawdown_lender():
	"""The facility values a drawdown fetches (lender type and name, Islamic), on the drawdowns saved before
	them: the Profit wording and the Lender columns of the reports read them from the drawdown."""
	drawdown = frappe.qb.DocType("Facility Drawdown")
	facility = frappe.qb.DocType("Bank Facility")
	for row in (
		frappe.qb.from_(drawdown)
		.join(facility)
		.on(facility.name == drawdown.facility)
		.select(drawdown.name, facility.lender_type, facility.lender_name, facility.is_islamic)
		.where(IfNull(drawdown.lender_type, "") == "")
	).run(as_dict=True):
		frappe.db.set_value(
			"Facility Drawdown",
			row.name,
			{
				"lender_type": row.lender_type,
				"lender_name": row.lender_name,
				"is_islamic": row.is_islamic or 0,
			},
			update_modified=False,
		)


def execute():
	_fill("Bank Facility", {"lender_type": "Bank", "late_payment_mode": "Paid to Lender"})

	facility = frappe.qb.DocType("Bank Facility")
	limit = frappe.qb.DocType("Bank Facility Limit")
	islamic = (
		frappe.qb.from_(limit)
		.select(limit.parent)
		.where((limit.parenttype == "Bank Facility") & (limit.limit_type.isin(["Murabaha", "Tawarruq"])))
	)
	frappe.qb.update(facility).set(facility.is_islamic, 1).where(facility.name.isin(islamic)).run()

	bank = frappe.qb.DocType("Bank")
	for row in (
		frappe.qb.from_(facility)
		.join(bank)
		.on(bank.name == facility.bank)
		.select(facility.name, bank.name.as_("bank"), bank.bank_name)
		.where(IfNull(facility.lender_name, "") == "")
	).run(as_dict=True):
		frappe.db.set_value(
			"Bank Facility", row.name, "lender_name", row.bank_name or row.bank, update_modified=False
		)

	_fill("Facility Drawdown", {"repayment_mode": "Manual", "disburse_to": "Bank Account"})
	_fetch_drawdown_lender()
	_fill("Facility Repayment", {"settlement": "Bank"})

	schedule = frappe.qb.DocType("Facility Repayment Schedule")
	(
		frappe.qb.update(schedule)
		.set(schedule.payment_voucher_type, "Journal Entry")
		.where(
			(IfNull(schedule.payment_journal, "") != "") & (IfNull(schedule.payment_voucher_type, "") == "")
		)
		.run()
	)

	_fill(
		"Treasury Company Settings",
		{"facility_voucher": "Payment Entry", "auto_debit_posting": "On Due Date"},
	)
