"""Phase 9b: bank transfers paying out the custody of a Custody Request (spec 9 §4 E7-E11, §8 9b).

The custodian's Payment Entry carries the amount only (custody refuses deductions); the fee is posted on its
own voucher, keyed (Bank Transfer, name, bt_fee, 1), as a Bank Charge. A return cancels the custodian's Payment
Entry (custody refuses a reversal Journal Entry). Skipped when the custody app is not installed; custody's
invariants are checked on tearDown (CustodyChequeTestCase).
"""

import frappe
from frappe import _
from frappe.utils import add_days, flt, fmt_money, getdate, today

from treasury.bank_transfers import lifecycle as transfer_lifecycle
from treasury.bank_transfers.report.bank_charges_analysis import bank_charges_analysis
from treasury.bank_transfers.scheduler import post_scheduled_transfers
from treasury.integrations.custody import REQUEST, api
from treasury.tests.custody_utils import (
	ACCOUNTANT,
	as_user,
	assert_request,
	custody_payment_entries,
	make_custodian,
	make_request,
)
from treasury.tests.test_custody_cheques import TREASURY_ONLY, CustodyChequeTestCase
from treasury.tests.utils import (
	BANK_CHARGE_SETTINGS,
	COMPANY,
	SUPPLIER,
	TRANSFER_SETTINGS,
	acc,
	assert_deductions,
	assert_gl_entries,
	bank_account_name,
	ensure_other_bank,
	ensure_user,
	fee_rule,
	get_pe,
	make_bank_transfer,
	make_iban,
	report_total,
	run_report,
	set_bank_suppliers,
	set_fee_rules,
)

DOCTYPE = "Bank Transfer"
FEE = 5


class CustodyTransferTestCase(CustodyChequeTestCase):
	def setUp(self):
		super().setUp()
		self.use_company_settings(**TRANSFER_SETTINGS, **BANK_CHARGE_SETTINGS)
		ensure_other_bank()
		set_bank_suppliers(self, [])
		# Fawran: a fixed fee of 5; Normal: no fee
		set_fee_rules(self, [fee_rule(bank_account_name("EGP"), "Fawran", fixed_fee=FEE)])
		self.fee_account = acc("Treasury Bank Charges")

	# ------------------------------------------------------------------ helpers

	def transfer(self, req, method="Fawran", submit=True, amount=None, **values):
		"""A Bank Transfer of the request through the mapper (the client's path), to an IBAN at the Other Bank."""
		data = api.make_bank_transfer(req.name, method)
		for key in ("name", "__islocal", "__unsaved", "owner", "creation", "modified", "modified_by"):
			data.pop(key, None)
		data["beneficiary_iban"] = data.get("beneficiary_iban") or make_iban("OTHB")
		if amount is not None:
			data["amount"] = amount
		data.update(values)
		doc = frappe.get_doc(data)
		doc.insert()
		if submit:
			doc.submit()
			doc.reload()
		return doc

	def e7(self, req, amount=10000, date=None):
		date = getdate(date or today())
		return [(self.cus, amount, 0, req.custodian, date), (self.data.egp.bank_gl, 0, amount, None, date)]

	def e8(self, date=None):
		date = getdate(date or today())
		return [(self.fee_account, FEE, 0, None, date), (self.data.egp.bank_gl, 0, FEE, None, date)]

	def transfer_fees(self) -> float:
		"""Σ fees of Transfer Fees Analysis today, EGP bank."""
		filters = {"company": COMPANY, "from_date": today(), "to_date": today()}
		data = run_report("Transfer Fees Analysis", {**filters, "bank_account": bank_account_name("EGP")})[1]
		return report_total(data, "fees")

	def charges_of(self, transfer) -> list:
		"""Bank Charges Analysis detail rows of a transfer (source Transfer Fees)."""
		filters = frappe._dict(
			company=COMPANY, from_date=today(), to_date=today(), view="Detail", source="Transfer Fees"
		)
		return [r for r in bank_charges_analysis.execute(filters)[1] if r.document == transfer.name]


class TestCustodyTransfers(CustodyTransferTestCase):
	# ------------------------------------------------------------------ E7, E8: payout with a separate fee

	def test_fawran_fee_separate(self):
		for voucher in ("Payment Entry", "Journal Entry"):
			with self.subTest(bank_charge_voucher=voucher):
				self.use_company_settings(bank_charge_voucher=voucher)
				req = self.request()
				before = self.transfer_fees()
				transfer = self.transfer(req)
				self.assertEqual((transfer.status, transfer.accounting_status), ("Sent", "Posted"))
				self.assertEqual((transfer.source_doctype, transfer.source_name), (REQUEST, req.name))
				self.assertEqual((flt(transfer.fee_amount), flt(transfer.bank_amount)), (FEE, 10000 + FEE))

				pe = get_pe(transfer.payment_entry)
				self.assertEqual((pe.payment_type, pe.party_type, pe.party), ("Pay", "Custodian", req.custodian))
				self.assertEqual((pe.custody_request, pe.custody_purpose), (req.name, "Disbursement"))
				self.assertEqual((pe.paid_from, pe.paid_to), (self.data.egp.bank_gl, self.cus))
				self.assertEqual((flt(pe.paid_amount), flt(pe.received_amount)), (10000, 10000))
				self.assertFalse(pe.deductions)
				assert_gl_entries(self, "Payment Entry", pe.name, self.e7(req))

				self.assertEqual(transfer.fee_voucher_type, voucher)
				self.assertTrue(transfer.fee_voucher)
				assert_gl_entries(self, voucher, transfer.fee_voucher, self.e8())
				fee_voucher = frappe.get_doc(voucher, transfer.fee_voucher)
				self.assertEqual(
					(fee_voucher.treasury_source_name, fee_voucher.treasury_event),
					(transfer.name, transfer_lifecycle.EVENT_FEE),
				)
				if voucher == "Payment Entry":
					self.assertEqual((fee_voucher.party_type, fee_voucher.payment_type), ("Supplier", "Pay"))
					assert_deductions(self, fee_voucher, [(self.fee_account, FEE, 0)])
				else:
					self.assertEqual(fee_voucher.voucher_type, "Bank Entry")
				self.assertEqual(flt(transfer.base_fee_amount), FEE)
				events = [(r.event, r.voucher_no) for r in transfer.status_log]
				self.assertIn(("Sent", pe.name), events)
				self.assertIn(("Fee Posted", transfer.fee_voucher), events)

				assert_request(self, req.name, paid_amount=10000, remaining_to_pay=0, status="Paid")
				# the fee counts once in both fee reports
				self.assertEqual(flt(self.transfer_fees() - before, 2), FEE)
				rows = self.charges_of(transfer)
				self.assertEqual([(r.event, flt(r.amount)) for r in rows], [(transfer_lifecycle.EVENT_FEE, FEE)])

	def test_normal_no_fee(self):
		req = self.request()
		transfer = self.transfer(req, "Normal")
		self.assertEqual(flt(transfer.fee_amount), 0)
		self.assertFalse(transfer.fee_voucher)
		self.assertFalse(transfer.fee_voucher_type)
		self.assertEqual(flt(transfer.base_fee_amount), 0)
		assert_gl_entries(self, "Payment Entry", transfer.payment_entry, self.e7(req))
		assert_request(self, req.name, paid_amount=10000, status="Paid")

	def test_scheduled_then_sent(self):
		req = self.request()
		later = getdate(add_days(today(), 3))
		transfer = self.transfer(req, execution_date=later)
		self.assertEqual(transfer.status, "Scheduled")
		self.assertFalse(transfer.payment_entry or transfer.fee_voucher)
		# a Scheduled transfer reserves the request: nothing is left to pay
		self.assertEqual(api.get_payment_context(req.name)["pay"]["amount"], 0)
		assert_request(self, req.name, paid_amount=0, remaining_to_pay=10000)

		result = post_scheduled_transfers(as_of=later)
		self.assertIn(transfer.name, result["posted"])
		transfer.reload()
		self.assertEqual(transfer.status, "Sent")
		assert_gl_entries(self, "Payment Entry", transfer.payment_entry, self.e7(req, date=later))
		assert_gl_entries(self, transfer.fee_voucher_type, transfer.fee_voucher, self.e8(later))
		assert_request(self, req.name, paid_amount=10000, status="Paid")

		# idempotent rerun: nothing more is posted
		self.assertNotIn(transfer.name, post_scheduled_transfers(as_of=later)["posted"])
		self.assertEqual(custody_payment_entries(req.name), [transfer.payment_entry])
		fee_vouchers = frappe.get_all(
			"Payment Entry",
			filters={
				"treasury_source_doctype": DOCTYPE,
				"treasury_source_name": transfer.name,
				"treasury_event": transfer_lifecycle.EVENT_FEE,
				"docstatus": 1,
			},
			pluck="name",
		)
		self.assertEqual(fee_vouchers, [transfer.fee_voucher])

	def test_completion_clears_both(self):
		for completion in ("On Bank Confirmation", "On Send for Fawran", "On Send"):
			with self.subTest(transfer_completion=completion):
				self.use_company_settings(transfer_completion=completion)
				req = self.request()
				transfer = self.transfer(req)
				if completion == "On Bank Confirmation":
					self.assertEqual(transfer.status, "Sent")
					transfer.mark_completed(completion_date=today())
					transfer.reload()
				self.assertEqual(transfer.status, "Completed")
				for name in (transfer.payment_entry, transfer.fee_voucher):
					cleared = frappe.db.get_value("Payment Entry", name, "clearance_date")
					self.assertEqual(getdate(cleared), getdate(today()), name)

	# ------------------------------------------------------------------ E10, E11: return and cancel

	def test_return_keeps_or_refunds_fee(self):
		for refund in (0, 1):
			with self.subTest(refund_fees_on_return=refund):
				self.use_company_settings(refund_fees_on_return=refund)
				req = self.request()
				transfer = self.transfer(req)
				pe, fee_voucher = transfer.payment_entry, transfer.fee_voucher
				transfer.mark_returned(return_date=today(), reason="Wrong IBAN")
				transfer.reload()
				self.assertEqual((transfer.status, transfer.accounting_status), ("Returned", "Reversed"))
				self.assertEqual(frappe.db.get_value("Payment Entry", pe, "docstatus"), 2)
				self.assertFalse(transfer.reversal_journal)
				self.assertFalse(transfer.reversal_payment_entry)
				self.assertEqual(transfer.payment_entry, pe)
				self.assertEqual(frappe.db.get_value("Payment Entry", fee_voucher, "docstatus"), 1)
				self.assertEqual(transfer.fees_refunded, refund)
				refunds = frappe.get_all(
					"Journal Entry",
					filters={
						"treasury_source_doctype": DOCTYPE,
						"treasury_source_name": transfer.name,
						"treasury_event": transfer_lifecycle.EVENT_FEE_REVERSAL,
						"docstatus": 1,
					},
					pluck="name",
				)
				if refund:
					self.assertEqual(len(refunds), 1)
					assert_gl_entries(
						self,
						"Journal Entry",
						refunds[0],
						[(self.data.egp.bank_gl, FEE, 0, None, today()), (self.fee_account, 0, FEE, None, today())],
					)
				else:
					self.assertEqual(refunds, [])
				events = [(r.event, r.voucher_no) for r in transfer.status_log]
				self.assertIn(("Voucher Cancelled", pe), events)
				self.assertIn(("Returned", pe), events)
				assert_request(self, req.name, paid_amount=0, remaining_to_pay=10000, status="Approved")

	def test_cancel_sent_cancels_both(self):
		req = self.request()
		transfer = self.transfer(req)
		pe, fee_voucher = transfer.payment_entry, transfer.fee_voucher
		transfer.cancel()
		transfer.reload()
		self.assertEqual(transfer.status, "Cancelled")
		self.assertEqual(frappe.db.get_value("Payment Entry", pe, "docstatus"), 2)
		self.assertEqual(frappe.db.get_value("Payment Entry", fee_voucher, "docstatus"), 2)
		events = [(r.event, r.voucher_no) for r in transfer.status_log]
		self.assertIn(("Voucher Cancelled", fee_voucher), events)
		self.assertIn(("Cancelled", pe), events)
		assert_request(self, req.name, paid_amount=0, remaining_to_pay=10000, status="Approved")

	# ------------------------------------------------------------------ refusals

	def test_receipt_and_swift_refused(self):
		req = self.request()
		self.assert_refused(
			lambda: self.transfer(req, submit=False, transfer_type="Party Receipt"),
			_("A custody return by transfer is recorded as a Payment Entry in Custody."),
		)
		self.assert_refused(
			lambda: self.transfer(req, submit=False, method="SWIFT"),
			_("SWIFT transfers to custodians are not supported."),
		)
		self.assert_refused(lambda: api.make_bank_transfer(req.name, "SWIFT"), "SWIFT")
		without_source = _("A cheque or transfer to a custodian must be created from its Custody Request.")
		self.assert_refused(
			lambda: make_bank_transfer(submit=False, party_type="Custodian", party=req.custodian), without_source
		)

	def test_amount_above_available(self):
		req = self.request()
		self.transfer(req, "Normal", submit=False, amount=6000)
		self.assertEqual(flt(api.make_bank_transfer(req.name, "Normal")["amount"]), 4000)
		self.assert_refused(
			lambda: self.transfer(req, "Normal", submit=False, amount=4000.01),
			_("At most {0} can still be paid on Custody Request {1}.").format(
				fmt_money(4000, currency="EGP"), frappe.bold(req.name)
			),
		)

	def test_permissions(self):
		req = self.request()
		treasury_only = ensure_user(TREASURY_ONLY, ["Treasury User"])
		with as_user(treasury_only):
			self.assertRaises(frappe.PermissionError, api.make_bank_transfer, req.name, "Normal")

		custody_user = ensure_user(f"trs-custody-{frappe.generate_hash(length=6)}@treasury.test", [])
		_employee, custodian = make_custodian(user=custody_user)
		own = make_request(custodian, 1000, cost_center=self.cost_center)
		with as_user(custody_user):
			self.assertRaises(frappe.PermissionError, api.make_bank_transfer, own.name, "Normal")

		with as_user(ACCOUNTANT):
			data = api.make_bank_transfer(req.name, "Fawran")
			self.assertEqual((flt(data["amount"]), data["method"]), (10000, "Fawran"))
			self.assertEqual((data["party_type"], data["transfer_type"]), ("Custodian", "Party Payment"))
			self.assertFalse(data.get("references"))
			context = api.get_payment_context(req.name)
			self.assertTrue(context["can_transfer"])
			self.assertEqual(set(context["transfer_banks"]), {"Normal", "Fawran"})

	def test_default_method_and_bank(self):
		self.use_company_settings(
			custody_default_method="Fawran", custody_bank_account=bank_account_name("EGP")
		)
		req = self.request()
		data = api.make_bank_transfer(req.name)
		self.assertEqual((data["method"], data["bank_account"]), ("Fawran", bank_account_name("EGP")))
		self.assertEqual(api.get_payment_context(req.name)["default_method"], "Fawran")

	# ------------------------------------------------------------------ regression

	def test_supplier_transfer_unchanged(self):
		"""A non-custodian keeps the fee as a deduction of its Payment Entry, without a fee voucher."""
		transfer = make_bank_transfer(method="Fawran", amount=10000)
		self.assertFalse(transfer.fee_voucher)
		pe = get_pe(transfer.payment_entry)
		assert_deductions(self, pe, [(self.fee_account, FEE, 0)])
		assert_gl_entries(
			self,
			"Payment Entry",
			pe.name,
			[
				(self.data.payable, 10000, 0, SUPPLIER),
				(self.fee_account, FEE, 0, None),
				(self.data.egp.bank_gl, 0, 10000 + FEE, None),
			],
		)
		self.assertEqual(flt(transfer.base_fee_amount), FEE)
