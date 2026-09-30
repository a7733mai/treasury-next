"""Reconcile a Bank Transaction against a Treasury voucher (shared by PDC Bank Clearing and Bank Charge)."""

import frappe

from treasury.utils.context import elevated

BANK_TRANSACTION = "Bank Transaction"


def reconcile_voucher(bank_transaction: str, voucher_doctype: str, voucher_name: str):
	"""Reconcile a Bank Transaction against a voucher with ERPNext's BankTransaction methods, in the order
	`bank_reconciliation_tool.reconcile_vouchers` calls them. The user's right to clear the Treasury document
	was checked by its lifecycle just before; Treasury users need not hold Bank Transaction / voucher
	permissions (like the vouchers of treasury.accounting.posting)."""
	with elevated():
		transaction = frappe.get_doc(BANK_TRANSACTION, bank_transaction)
		transaction.add_payment_entries([{"payment_doctype": voucher_doctype, "payment_name": voucher_name}])
		transaction.validate_duplicate_references()
		transaction.allocate_payment_entries()
		transaction.update_allocated_amount()
		transaction.set_status()
		transaction.flags.ignore_permissions = True
		transaction.save()
