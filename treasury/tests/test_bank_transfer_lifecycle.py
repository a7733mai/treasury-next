"""Phase 7b: Bank Transfer lifecycle — schedule, send, reschedule, complete, return, cancel, amend, fee override,
protected fields and validations (spec 7b §3.2, §3.3, §4 T7-T9, §7 C)."""

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, flt, formatdate, getdate, today

from treasury.bank_transfers import lifecycle
from treasury.tests.utils import (
	BANK,
	COMPANY,
	OTHER_ABBR,
	OTHER_BANK,
	OTHER_COMPANY,
	SUPPLIER,
	TRANSFER_SETTINGS,
	USD_SUPPLIER,
	TreasuryTestMixin,
	acc,
	assert_deductions,
	assert_gl_entries,
	bank_account_name,
	ensure_account,
	ensure_company,
	ensure_party_bank_account,
	ensure_second_egp_bank,
	ensure_supplier,
	ensure_user,
	get_party_outstanding,
	get_pe,
	get_transfer,
	get_voucher_count,
	make_bank_transfer,
	make_iban,
	make_purchase_invoice,
	make_sales_invoice,
	make_usd_purchase_invoice,
	set_fee_rules,
	set_transfer_limits,
	standard_fee_rules,
)

DOCTYPE = "Bank Transfer"
USER = "treasury-transfer-user@example.com"
MANAGER = "treasury-transfer-manager@example.com"
OTHER_SUPPLIER = "_Test Treasury Transfer Other Supplier"


def references(invoice, amount):
	return [{"reference_doctype": invoice.doctype, "reference_name": invoice.name, "allocated_amount": amount}]


class TestBankTransferLifecycle(TreasuryTestMixin, IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_user(USER, ["Treasury User"])
		ensure_user(MANAGER, ["Treasury Manager"])
		ensure_supplier(OTHER_SUPPLIER)
		cls.charges = acc("Treasury Bank Charges")

	def setUp(self):
		super().setUp()
		self.use_company_settings(**TRANSFER_SETTINGS)
		set_fee_rules(self, standard_fee_rules())
		self.today = getdate(today())
		previous = frappe.db.get_single_value("Treasury Settings", "allow_future_clearance_date")
		frappe.db.set_single_value("Treasury Settings", "allow_future_clearance_date", 0)
		self.addCleanup(frappe.db.set_single_value, "Treasury Settings", "allow_future_clearance_date", previous)

	def day(self, offset: int):
		return getdate(add_days(self.today, offset))

	def assert_status(self, transfer, status, accounting):
		transfer.reload()
		self.assertEqual((transfer.status, transfer.accounting_status), (status, accounting))

	def second_bank_elsewhere(self):
		"""The second EGP bank account, at the Other Bank for this test (an internal transfer with a fee)."""
		second = ensure_second_egp_bank()
		frappe.db.set_value("Bank Account", second.bank_account, "bank", OTHER_BANK)
		self.addCleanup(frappe.db.set_value, "Bank Account", second.bank_account, "bank", BANK)
		return second

	# ------------------------------------------------------------------ schedule and send

	def test_scheduled_then_send_now(self):
		transfer = make_bank_transfer(amount=20000, execution_date=self.day(3))
		self.assert_status(transfer, "Scheduled", "Not Posted")
		self.assertEqual(get_voucher_count(DOCTYPE, transfer.name), 0)
		self.assertEqual([r.event for r in transfer.status_log], ["Created", "Scheduled"])

		transfer.send_now()
		self.assert_status(transfer, "Sent", "Posted")
		self.assertEqual(getdate(transfer.execution_date), self.today)
		pe = get_pe(transfer.payment_entry)
		self.assertEqual(getdate(pe.posting_date), self.today)
		self.assertEqual(transfer.status_log[-1].event, "Sent")

		with self.assertRaisesRegex(frappe.ValidationError, "not allowed while"):
			transfer.send_now()
		with self.assertRaisesRegex(frappe.ValidationError, "Reschedule"):
			make_bank_transfer(amount=100, execution_date=self.day(3)).send_now(execution_date=self.day(2))

	def test_reschedule(self):
		transfer = make_bank_transfer(amount=500, execution_date=self.day(3))
		transfer.reschedule(execution_date=self.day(5))
		transfer.reload()
		self.assertEqual(getdate(transfer.execution_date), self.day(5))
		row = transfer.status_log[-1]
		self.assertEqual((row.event, row.to_status), ("Rescheduled", "Scheduled"))
		self.assertIn(formatdate(self.day(3)), row.remarks)
		self.assertIn(formatdate(self.day(5)), row.remarks)
		for date in (self.today, self.day(-1)):
			with self.subTest(date=date):
				self.assertRaises(frappe.ValidationError, transfer.reschedule, execution_date=date)

		# a Fawran daily limit with Block on the new date
		self.use_company_settings(transfer_limit_action="Block")
		limit = {"bank_account": bank_account_name("EGP"), "method": "Fawran", "daily_limit": 1000}
		set_transfer_limits(self, [{**limit, "per_transaction_limit": 0}])
		make_bank_transfer(method="Fawran", amount=800, execution_date=self.day(7))
		fawran = make_bank_transfer(method="Fawran", amount=800, execution_date=self.day(4))
		with self.assertRaisesRegex(frappe.ValidationError, "daily"):
			fawran.reschedule(execution_date=self.day(7))
		self.assertEqual(getdate(get_transfer(fawran.name).execution_date), self.day(4))

	def test_backdated_submit(self):
		transfer = make_bank_transfer(amount=700, execution_date=self.day(-2))
		self.assert_status(transfer, "Sent", "Posted")
		self.assertEqual(getdate(get_pe(transfer.payment_entry).posting_date), self.day(-2))

	def test_idempotent_send(self):
		transfer = make_bank_transfer(amount=900, execution_date=self.day(-1))
		payment_entry = transfer.payment_entry
		doc = get_transfer(transfer.name)
		lifecycle._send(doc, doc.execution_date)
		self.assertEqual(doc.payment_entry, payment_entry)
		self.assertEqual(get_voucher_count(DOCTYPE, transfer.name), 1)

	# ------------------------------------------------------------------ completion

	def test_complete(self):
		transfer = make_bank_transfer(amount=1500, execution_date=self.day(-5))
		transfer.mark_completed(completion_date=self.day(-1), bank_transaction_date=self.day(-2))
		self.assert_status(transfer, "Completed", "Posted")
		self.assertEqual(
			(getdate(transfer.completion_date), getdate(transfer.bank_transaction_date)),
			(self.day(-1), self.day(-2)),
		)
		self.assertEqual(getdate(get_pe(transfer.payment_entry).clearance_date), self.day(-2))
		with self.assertRaisesRegex(frappe.ValidationError, "already Completed"):
			transfer.mark_completed(completion_date=self.day(-1))

		sent = make_bank_transfer(amount=1600, execution_date=self.day(-5))
		self.assertRaises(frappe.ValidationError, sent.mark_completed, completion_date=self.day(2))
		self.assertRaises(frappe.ValidationError, sent.mark_completed, completion_date=self.day(-6))
		self.assert_status(sent, "Sent", "Posted")

	def test_completion_setting(self):
		expected = {
			("On Bank Confirmation", "Fawran"): "Sent",
			("On Bank Confirmation", "Normal"): "Sent",
			("On Send for Fawran", "Fawran"): "Completed",
			("On Send for Fawran", "Normal"): "Sent",
			("On Send", "Fawran"): "Completed",
			("On Send", "Normal"): "Completed",
		}
		for (setting, method), status in expected.items():
			with self.subTest(setting=setting, method=method):
				self.use_company_settings(transfer_completion=setting)
				transfer = make_bank_transfer(method=method, amount=600, execution_date=self.day(-1))
				self.assert_status(transfer, status, "Posted")
				clearance = frappe.db.get_value("Payment Entry", transfer.payment_entry, "clearance_date")
				if status == "Completed":
					self.assertEqual(getdate(clearance), self.day(-1))
					self.assertEqual(getdate(transfer.completion_date), self.day(-1))
					self.assertEqual([r.event for r in transfer.status_log][-2:], ["Sent", "Completed"])
				else:
					self.assertIsNone(clearance)

	# ------------------------------------------------------------------ return

	def test_return_party_payment(self):
		egp = self.data.egp
		for refund in (0, 1):
			with self.subTest(refund=refund):
				self.use_company_settings(refund_fees_on_return=refund)
				pi = make_purchase_invoice(20000, posting_date=self.day(-5))
				transfer = make_bank_transfer(
					amount=20000, execution_date=self.day(-3), references=references(pi, 20000)
				)
				self.assertEqual(get_party_outstanding("Purchase Invoice", pi.name), 0)
				transfer.mark_returned(return_date=self.day(-1), reason="Beneficiary account closed")
				self.assert_status(transfer, "Returned", "Reversed")
				self.assertTrue(transfer.reversal_journal)
				self.assertFalse(transfer.reversal_payment_entry)
				self.assertEqual(transfer.fees_refunded, refund)
				self.assertEqual(getdate(transfer.return_date), self.day(-1))
				rows = [
					(egp.bank_gl, 20030 if refund else 20000, 0, None, self.day(-1)),
					(self.data.payable, 0, 20000, SUPPLIER, self.day(-1)),
				]
				if refund:
					rows.append((self.charges, 0, 30, None, self.day(-1)))
				assert_gl_entries(self, "Journal Entry", transfer.reversal_journal, rows)
				self.assertEqual(get_party_outstanding("Purchase Invoice", pi.name), 20000)
				self.assertEqual(transfer.status_log[-1].voucher_no, transfer.reversal_journal)
				with self.assertRaisesRegex(frappe.ValidationError, "already Returned"):
					transfer.mark_returned(return_date=self.day(-1), reason="Again")

	def test_return_usd_party_payment(self):
		"""Risk 7b §8.6: a foreign-currency payment returned with the fee kept — the Journal Entry row against the
		invoice is booked at the invoice's rate, the difference goes to Exchange Gain / Loss."""
		usd = self.data.usd
		pi = make_usd_purchase_invoice(1000, posting_date=self.day(-5), conversion_rate=50)
		transfer = make_bank_transfer(
			method="SWIFT",
			bank_account=usd.bank_account,
			party=USD_SUPPLIER,
			charge_bearer="OUR",
			beneficiary_swift="DEUTDEFF",
			exchange_rate=52,
			amount=1000,
			execution_date=self.day(-3),
			references=references(pi, 1000),
		)
		transfer.mark_returned(return_date=self.day(-1), reason="Returned by the correspondent bank")
		self.assert_status(transfer, "Returned", "Reversed")
		exchange_account = frappe.get_cached_value("Company", COMPANY, "exchange_gain_loss_account")
		assert_gl_entries(
			self,
			"Journal Entry",
			transfer.reversal_journal,
			[(usd.bank_gl, 52000, 0), (self.data.usd_payable, 0, 50000), (exchange_account, 0, 2000)],
		)
		self.assertEqual(get_party_outstanding("Purchase Invoice", pi.name), 1000)

	def test_return_internal(self):
		egp = self.data.egp
		second = ensure_second_egp_bank()
		same = make_bank_transfer(
			transfer_type="Internal Transfer",
			amount=50000,
			to_bank_account=second.bank_account,
			execution_date=self.day(-3),
		)
		same.mark_returned(return_date=self.day(-1), reason="Wrong account")
		same.reload()
		self.assertTrue(same.reversal_payment_entry)
		self.assertFalse(same.reversal_journal)
		assert_gl_entries(
			self,
			"Payment Entry",
			same.reversal_payment_entry,
			[(egp.bank_gl, 50000, 0), (second.bank_gl, 0, 50000)],
		)

		self.second_bank_elsewhere()
		for refund in (0, 1):
			with self.subTest(refund=refund):
				self.use_company_settings(refund_fees_on_return=refund)
				transfer = make_bank_transfer(
					transfer_type="Internal Transfer",
					amount=50000,
					to_bank_account=second.bank_account,
					execution_date=self.day(-3),
				)
				self.assertEqual(transfer.fee_amount, 50)
				transfer.mark_returned(return_date=self.day(-1), reason="Wrong account")
				transfer.reload()
				rows = [(egp.bank_gl, 50050 if refund else 50000, 0), (second.bank_gl, 0, 50000)]
				if refund:
					rows.append((self.charges, 0, 50))
				assert_gl_entries(self, "Journal Entry", transfer.reversal_journal, rows)

	def test_return_receipt_rejected(self):
		receipt = make_bank_transfer(transfer_type="Party Receipt", amount=3000, execution_date=self.day(-2))
		with self.assertRaisesRegex(frappe.ValidationError, "new Party Payment"):
			receipt.mark_returned(return_date=self.today, reason="Refund")

	def test_return_requires_reason_and_manager(self):
		transfer = make_bank_transfer(amount=3000, execution_date=self.day(-2))
		self.assertRaises(frappe.ValidationError, transfer.mark_returned, return_date=self.today, reason="")
		with self.set_user(USER):
			doc = get_transfer(transfer.name)
			self.assertRaises(frappe.PermissionError, doc.mark_returned, return_date=self.today, reason="X")
		with self.set_user(MANAGER):
			get_transfer(transfer.name).mark_returned(return_date=self.today, reason="Returned")
		self.assert_status(transfer, "Returned", "Reversed")
		self.assertEqual(transfer.status_log[-1].user, MANAGER)

	# ------------------------------------------------------------------ cancel, amend

	def test_cancel(self):
		scheduled = make_bank_transfer(amount=400, execution_date=self.day(3))
		scheduled.cancel()
		self.assert_status(scheduled, "Cancelled", "Not Posted")
		self.assertEqual(get_voucher_count(DOCTYPE, scheduled.name), 0)

		sent = make_bank_transfer(amount=400, execution_date=self.day(-1))
		payment_entry = sent.payment_entry
		with self.set_user(USER):
			self.assertRaises(frappe.PermissionError, get_transfer(sent.name).cancel)
		with self.set_user(MANAGER):
			get_transfer(sent.name).cancel()
		self.assert_status(sent, "Cancelled", "Not Posted")
		self.assertEqual(frappe.db.get_value("Payment Entry", payment_entry, "docstatus"), 2)
		row = sent.status_log[-1]
		self.assertEqual((row.event, row.voucher_no, row.user), ("Cancelled", payment_entry, MANAGER))

		completed = make_bank_transfer(amount=400, execution_date=self.day(-1))
		completed.mark_completed(completion_date=self.today)
		completed.reload()
		with self.assertRaisesRegex(frappe.ValidationError, "Return it instead"):
			completed.cancel()

	def test_voucher_guard(self):
		transfer = make_bank_transfer(amount=450, execution_date=self.day(-1))
		with self.assertRaisesRegex(frappe.ValidationError, "managed by"):
			get_pe(transfer.payment_entry).cancel()

	def test_amend(self):
		transfer = make_bank_transfer(amount=470, execution_date=self.day(-1))
		transfer.cancel()
		amended = frappe.copy_doc(transfer, ignore_no_copy=False)  # like the desk "Amend"
		amended.docstatus = 0  # as the desk "Amend" action does
		amended.amended_from = transfer.name
		amended.insert()
		self.assertEqual((amended.status, amended.accounting_status), ("Draft", "Not Posted"))
		self.assertEqual(amended.amended_from, transfer.name)
		self.assertFalse(amended.payment_entry)
		self.assertEqual([r.event for r in amended.status_log], ["Created"])

	# ------------------------------------------------------------------ fee override

	def test_fee_override(self):
		with self.set_user(USER):
			transfer = make_bank_transfer(
				submit=False,
				amount=20000,
				override_fee=1,
				fee_amount=7,
				fee_override_reason="Negotiated with the bank",
			)
			self.assertEqual((transfer.computed_fee, transfer.fee_amount), (30, 7))
			transfer.submit()
		transfer.reload()
		assert_deductions(self, get_pe(transfer.payment_entry), [(self.charges, 7, 0)])
		self.assertEqual(flt(transfer.bank_amount), 20007)
		override = [r for r in transfer.status_log if r.event == "Fee Overridden"]
		self.assertEqual(len(override), 1)
		self.assertEqual(override[0].user, USER)
		for text in ("30.00", "7.00", "Negotiated with the bank"):
			self.assertIn(text, override[0].remarks)

		self.assertRaisesRegex(
			frappe.ValidationError,
			"reason",
			make_bank_transfer,
			submit=False,
			amount=20000,
			override_fee=1,
			fee_amount=7,
		)
		transfer.fee_amount = 9
		self.assertRaises(frappe.ValidationError, transfer.save)

	def test_protected_fields(self):
		transfer = make_bank_transfer(amount=480, execution_date=self.day(-1))
		for fieldname, value in (("status", "Completed"), ("payment_entry", None), ("fees_refunded", 1)):
			with self.subTest(fieldname=fieldname):
				doc = get_transfer(transfer.name)
				doc.set(fieldname, value)
				with self.assertRaisesRegex(frappe.ValidationError, "can only be changed by the"):
					doc.save()
		# the bank reference may still be entered while Sent
		doc = get_transfer(transfer.name)
		doc.bank_reference = "FT-12345"
		doc.save()
		doc.mark_completed(completion_date=self.today)
		doc = get_transfer(transfer.name)
		doc.bank_reference = "FT-99999"
		self.assertRaisesRegex(frappe.ValidationError, "Bank Reference", doc.save)

		draft = make_bank_transfer(submit=False, amount=490)
		draft.append("status_log", {"event": "Completed", "to_status": "Completed"})
		self.assertRaisesRegex(frappe.ValidationError, "can only be changed by the", draft.save)

	# ------------------------------------------------------------------ validations

	def test_validation_errors(self):
		egp = self.data.egp
		second = ensure_second_egp_bank()
		foreign_pi = make_purchase_invoice(500, supplier=OTHER_SUPPLIER)
		si = make_sales_invoice(500)
		pi = make_purchase_invoice(500)
		other_account = ensure_party_bank_account("Supplier", OTHER_SUPPLIER, iban=make_iban("OTHB"), is_default=0)
		internal = {"transfer_type": "Internal Transfer"}
		cases = {
			"same source and target": {**internal, "to_bank_account": egp.bank_account},
			"target of another company": {**internal, "to_bank_account": self.foreign_bank_account()},
			"cross-currency without received amount": {
				**internal,
				"to_bank_account": self.data.usd.bank_account,
			},
			"receipt fee not below the amount": {"transfer_type": "Party Receipt", "amount": 5},
			"party bank account of another party": {"party_bank_account": other_account},
			"invoice of another supplier": {"references": references(foreign_pi, 500)},
			"allocation above the amount": {"amount": 400, "references": references(pi, 500)},
			"receipt with a purchase invoice": {
				"transfer_type": "Party Receipt",
				"references": references(pi, 500),
			},
			"payment without IBAN or bank account": {"beneficiary_iban": None},
			"SWIFT without SWIFT code": {
				"method": "SWIFT",
				"bank_account": self.data.usd.bank_account,
				"party": USD_SUPPLIER,
			},
			"amount not above zero": {"amount": 0},
			"sales invoice of the customer on a payment": {"references": references(si, 500)},
		}
		for case, values in cases.items():
			with self.subTest(case=case):
				self.assertRaises(frappe.ValidationError, make_bank_transfer, **{"amount": 500, **values})
		# none of them was saved
		self.assertFalse(frappe.db.exists(DOCTYPE, {"to_bank_account": second.bank_account, "amount": 500}))

	@staticmethod
	def foreign_bank_account() -> str:
		"""A company bank account of another company."""
		ensure_company(OTHER_COMPANY, OTHER_ABBR)
		gl = ensure_account(
			"Treasury Bank EGP Co2 BT", "Bank Accounts", "Bank", company=OTHER_COMPANY, abbr=OTHER_ABBR
		)
		name = f"Treasury Co2 BT - {BANK}"
		if not frappe.db.exists("Bank Account", name):
			frappe.get_doc(
				{
					"doctype": "Bank Account",
					"account_name": "Treasury Co2 BT",
					"bank": BANK,
					"account": gl,
					"company": OTHER_COMPANY,
					"is_company_account": 1,
				}
			).insert(ignore_permissions=True)
		return name
