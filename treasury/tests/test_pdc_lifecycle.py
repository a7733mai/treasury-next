"""End-to-end PDC Cheque scenarios under the different Treasury Company Settings (PLAN.md §4, §12)."""

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import flt, getdate

from treasury.pdc import lifecycle
from treasury.tests.utils import (
	COMPANY,
	CUSTOMER,
	DEFAULT_PDC_SETTINGS,
	SUPPLIER,
	TreasuryTestMixin,
	assert_gl_entries,
	day,
	ensure_user,
	get_account_balance,
	get_party_balance,
	get_party_outstanding,
	get_voucher_count,
	make_pdc_cheque,
	make_purchase_invoice,
	make_sales_invoice,
	unique_cheque_no,
)

DOCTYPE = "PDC Cheque"


class TestPDCLifecycle(TreasuryTestMixin, IntegrationTestCase):
	def setUp(self):
		super().setUp()
		self.use_company_settings(**DEFAULT_PDC_SETTINGS)

	# ------------------------------------------------------------------ helpers

	def assert_status(self, doc, operational: str, accounting: str):
		doc.reload()
		self.assertEqual((doc.operational_status, doc.accounting_status), (operational, accounting))

	def events(self, doc) -> list[str]:
		doc.reload()
		return [row.event for row in doc.status_log]

	def vouchers(self, doc) -> int:
		return get_voucher_count(DOCTYPE, doc.name)

	def posting_date(self, doctype: str, name: str):
		return getdate(frappe.db.get_value(doctype, name, "posting_date"))

	def delivered_outgoing(self, amount=1000, issue=-60, due=-30, handover=-59, invoice=True):
		pi = make_purchase_invoice(amount, posting_date=day(issue)) if invoice else None
		cheque = make_pdc_cheque(amount=amount, issue_date=day(issue), due_date=day(due), invoice=pi)
		cheque.deliver(handover_date=day(handover))
		cheque.reload()
		return cheque, pi

	def received_incoming(self, amount=2000, issue=-50, due=-20, receipt=-49):
		si = make_sales_invoice(amount, posting_date=day(issue))
		cheque = make_pdc_cheque(
			"Incoming", amount=amount, issue_date=day(issue), due_date=day(due), invoice=si
		)
		cheque.receive(receipt_date=day(receipt))
		cheque.reload()
		return cheque, si

	# ------------------------------------------------------------------ 1. outgoing On Handover

	def test_outgoing_on_handover_and_actual_clearance(self):
		egp = self.data.egp
		bank_before = get_account_balance(egp.bank_gl)
		pdc_before = get_account_balance(egp.pdc_payable)

		cheque, pi = self.delivered_outgoing()
		self.assert_status(cheque, "Delivered", "Party Posted")
		self.assertEqual(getdate(cheque.handover_date), day(-59))
		self.assertEqual(getdate(cheque.party_posting_date), day(-59))
		assert_gl_entries(
			self,
			"Payment Entry",
			cheque.party_payment_entry,
			[
				(self.data.payable, 1000, 0, SUPPLIER, day(-59)),
				(egp.pdc_payable, 0, 1000, None, day(-59)),
			],
		)
		self.assertEqual(get_party_outstanding("Purchase Invoice", pi.name), 0)
		self.assertEqual(get_account_balance(egp.bank_gl), bank_before)

		cheque.clear(clearance_date=day(-25))
		self.assert_status(cheque, "Cleared", "Bank Posted")
		self.assertEqual(getdate(cheque.actual_clearance_date), day(-25))
		self.assertEqual(getdate(cheque.bank_posting_date), day(-25))
		assert_gl_entries(
			self,
			"Journal Entry",
			cheque.bank_journal,
			[(egp.pdc_payable, 1000, 0, None, day(-25)), (egp.bank_gl, 0, 1000, None, day(-25))],
		)
		self.assertEqual(
			getdate(frappe.db.get_value("Journal Entry", cheque.bank_journal, "clearance_date")), day(-25)
		)
		self.assertEqual(get_account_balance(egp.bank_gl), flt(bank_before - 1000))
		self.assertEqual(get_account_balance(egp.pdc_payable), pdc_before)
		self.assertEqual(get_party_outstanding("Purchase Invoice", pi.name), 0)
		self.assertEqual(
			self.events(cheque), ["Created", "Delivered", "Party Posted", "Cleared", "Bank Posted"]
		)
		log = cheque.status_log[2]
		self.assertEqual((log.voucher_type, log.voucher_no), ("Payment Entry", cheque.party_payment_entry))
		self.assertEqual((log.from_accounting, log.to_accounting), ("Not Posted", "Party Posted"))

	# ------------------------------------------------------------------ 2. outgoing On Cheque Date

	def test_outgoing_on_cheque_date_follows_effective_due_date(self):
		self.use_company_settings(outgoing_party_posting="On Cheque Date")
		cheque = make_pdc_cheque(issue_date=day(-10), due_date=day(20))
		cheque.deliver(handover_date=day(-10))
		self.assert_status(cheque, "Delivered", "Not Posted")
		self.assertEqual(self.vouchers(cheque), 0)

		# postponed before posting: the party posting moves with the due date
		cheque.postpone(new_due_date=day(40), reason="Supplier agreed")
		cheque.reload()
		self.assertEqual(getdate(cheque.effective_due_date), day(40))
		self.assertEqual(getdate(cheque.original_due_date), day(20))
		cheque.post_party()
		cheque.reload()
		self.assertEqual(self.posting_date("Payment Entry", cheque.party_payment_entry), day(40))
		self.assertEqual(getdate(cheque.party_posting_date), day(40))
		self.assert_status(cheque, "Delivered", "Party Posted")

		# postponed after posting: no new entry, the party entry stays as it is
		cheque.postpone(new_due_date=day(50), reason="Again")
		cheque.reload()
		self.assertEqual(self.vouchers(cheque), 1)
		self.assertEqual(self.posting_date("Payment Entry", cheque.party_payment_entry), day(40))
		self.assertEqual(getdate(cheque.effective_due_date), day(50))
		self.assertEqual((cheque.is_postponed, cheque.postponement_count), (1, 2))
		self.assertEqual(
			[(getdate(p.old_due_date), getdate(p.new_due_date)) for p in cheque.postponements],
			[(day(20), day(40)), (day(40), day(50))],
		)
		self.assertEqual(cheque.accounting_status, "Party Posted")

	# ------------------------------------------------------------------ 3. cleared before party posting

	def test_outgoing_cleared_early_posts_party_first(self):
		self.use_company_settings(outgoing_party_posting="On Cheque Date")
		pi = make_purchase_invoice(700, posting_date=day(-40))
		cheque = make_pdc_cheque(amount=700, issue_date=day(-40), due_date=day(20), invoice=pi)
		cheque.deliver(handover_date=day(-40))
		self.assertEqual(self.vouchers(cheque), 0)

		cheque.clear(clearance_date=day(-5))
		self.assert_status(cheque, "Cleared", "Bank Posted")
		# party posted on the clearance date because the cheque date is later
		self.assertEqual(self.posting_date("Payment Entry", cheque.party_payment_entry), day(-5))
		self.assertEqual(self.posting_date("Journal Entry", cheque.bank_journal), day(-5))
		self.assertEqual(get_party_outstanding("Purchase Invoice", pi.name), 0)
		self.assertEqual(self.events(cheque)[-3:], ["Cleared", "Party Posted", "Bank Posted"])

	# ------------------------------------------------------------------ 4. outgoing On Creation

	def test_outgoing_on_creation(self):
		self.use_company_settings(outgoing_party_posting="On Creation")
		cheque = make_pdc_cheque(issue_date=day(-3), due_date=day(30))
		self.assert_status(cheque, "Created", "Party Posted")
		self.assertEqual(self.posting_date("Payment Entry", cheque.party_payment_entry), day(-3))
		self.assertEqual(self.events(cheque), ["Created", "Party Posted"])

		cheque.deliver(handover_date=day(-1))
		self.assert_status(cheque, "Delivered", "Party Posted")
		self.assertEqual(self.vouchers(cheque), 1)

	# ------------------------------------------------------------------ 5. incoming

	def test_incoming_receipt_under_collection_and_clearance(self):
		egp = self.data.egp
		before = {a: get_account_balance(a) for a in (egp.bank_gl, egp.pdc_receivable, egp.under_collection)}

		cheque, si = self.received_incoming()
		self.assert_status(cheque, "Received", "Party Posted")
		assert_gl_entries(
			self,
			"Payment Entry",
			cheque.party_payment_entry,
			[
				(egp.pdc_receivable, 2000, 0, None, day(-49)),
				(self.data.receivable, 0, 2000, CUSTOMER, day(-49)),
			],
		)
		self.assertEqual(get_party_outstanding("Sales Invoice", si.name), 0)

		cheque.deposit(deposit_date=day(-20))
		self.assert_status(cheque, "Deposited", "Under Collection Posted")
		self.assertEqual(getdate(cheque.deposit_date), day(-20))
		assert_gl_entries(
			self,
			"Journal Entry",
			cheque.under_collection_journal,
			[(egp.under_collection, 2000, 0, None, day(-20)), (egp.pdc_receivable, 0, 2000, None, day(-20))],
		)

		cheque.clear(clearance_date=day(-18))
		self.assert_status(cheque, "Cleared", "Bank Posted")
		assert_gl_entries(
			self,
			"Journal Entry",
			cheque.bank_journal,
			[(egp.bank_gl, 2000, 0, None, day(-18)), (egp.under_collection, 0, 2000, None, day(-18))],
		)
		self.assertEqual(self.vouchers(cheque), 3)
		self.assertEqual(get_account_balance(egp.bank_gl), flt(before[egp.bank_gl] + 2000))
		self.assertEqual(get_account_balance(egp.pdc_receivable), before[egp.pdc_receivable])
		self.assertEqual(get_account_balance(egp.under_collection), before[egp.under_collection])

	def test_incoming_without_under_collection_stage(self):
		self.use_company_settings(incoming_under_collection="Disabled")
		egp = self.data.egp
		cheque, _si = self.received_incoming()
		cheque.deposit(deposit_date=day(-20))
		self.assert_status(cheque, "Deposited", "Party Posted")
		self.assertEqual(self.vouchers(cheque), 1)

		cheque.clear(clearance_date=day(-18))
		self.assert_status(cheque, "Cleared", "Bank Posted")
		assert_gl_entries(
			self,
			"Journal Entry",
			cheque.bank_journal,
			[(egp.bank_gl, 2000, 0, None, day(-18)), (egp.pdc_receivable, 0, 2000, None, day(-18))],
		)

	def test_incoming_deposit_posts_party_first_when_needed(self):
		self.use_company_settings(incoming_party_posting="On Cheque Date")
		cheque, si = self.received_incoming(due=10)
		self.assert_status(cheque, "Received", "Not Posted")
		self.assertEqual(get_party_outstanding("Sales Invoice", si.name), 2000)

		cheque.deposit(deposit_date=day(-5))
		self.assert_status(cheque, "Deposited", "Under Collection Posted")
		self.assertEqual(self.posting_date("Payment Entry", cheque.party_payment_entry), day(-5))
		self.assertEqual(self.posting_date("Journal Entry", cheque.under_collection_journal), day(-5))

	# ------------------------------------------------------------------ 6. bank posting modes

	def test_manual_bank_posting(self):
		self.use_company_settings(outgoing_bank_posting="Manual")
		egp = self.data.egp
		cheque, _pi = self.delivered_outgoing()
		bank_before = get_account_balance(egp.bank_gl)

		cheque.clear(clearance_date=day(-5))
		self.assert_status(cheque, "Cleared", "Party Posted")
		self.assertFalse(cheque.bank_journal)
		self.assertEqual(get_account_balance(egp.bank_gl), bank_before)

		cheque.post_bank()
		self.assert_status(cheque, "Cleared", "Bank Posted")
		assert_gl_entries(
			self,
			"Journal Entry",
			cheque.bank_journal,
			[(egp.pdc_payable, 1000, 0, None, day(-5)), (egp.bank_gl, 0, 1000, None, day(-5))],
		)
		self.assertEqual(get_account_balance(egp.bank_gl), flt(bank_before - 1000))
		self.assertRaises(frappe.ValidationError, cheque.post_bank)
		self.assertEqual(self.vouchers(cheque), 2)

	def test_bank_posting_on_bank_transaction_date(self):
		self.use_company_settings(outgoing_bank_posting="On Bank Transaction Date")
		egp = self.data.egp
		cheque, _pi = self.delivered_outgoing()

		self.assertRaises(frappe.ValidationError, cheque.clear, clearance_date=day(-6))
		self.assert_status(cheque, "Delivered", "Party Posted")

		cheque.clear(clearance_date=day(-6), bank_transaction_date=day(-4))
		self.assert_status(cheque, "Cleared", "Bank Posted")
		self.assertEqual(getdate(cheque.bank_transaction_date), day(-4))
		assert_gl_entries(
			self,
			"Journal Entry",
			cheque.bank_journal,
			[(egp.pdc_payable, 1000, 0, None, day(-4)), (egp.bank_gl, 0, 1000, None, day(-4))],
		)
		self.assertEqual(
			getdate(frappe.db.get_value("Journal Entry", cheque.bank_journal, "clearance_date")), day(-4)
		)

	def test_bank_charges_on_clearance(self):
		egp = self.data.egp
		cheque, _pi = self.delivered_outgoing()
		cheque.clear(clearance_date=day(-5), bank_charges=15)
		cheque.reload()
		self.assertEqual(flt(cheque.bank_charges), 15)
		assert_gl_entries(
			self,
			"Journal Entry",
			cheque.bank_charges_journal,
			[(egp.bank_charges, 15, 0, None, day(-5)), (egp.bank_gl, 0, 15, None, day(-5))],
		)
		self.assertEqual(self.events(cheque)[-1], "Bank Charges Posted")

	def test_future_clearance_date_is_rejected(self):
		cheque, _pi = self.delivered_outgoing()
		self.assertRaises(frappe.ValidationError, cheque.clear, clearance_date=day(5))
		self.assert_status(cheque, "Delivered", "Party Posted")

	# ------------------------------------------------------------------ 7. bounce

	def test_bounce_before_posting_creates_no_entries(self):
		self.use_company_settings(outgoing_party_posting="On Cheque Date")
		cheque = make_pdc_cheque(issue_date=day(-10), due_date=day(10))
		cheque.deliver(handover_date=day(-10))
		cheque.bounce(bounce_date=day(-1), reason="Stopped")
		self.assert_status(cheque, "Bounced", "Not Posted")
		self.assertEqual(self.vouchers(cheque), 0)

	def test_bounce_after_party_posting(self):
		for mode in ("Reverse by Journal Entry", "Cancel Payment Entry"):
			with self.subTest(on_bounce=mode):
				self.use_company_settings(on_bounce=mode)
				egp = self.data.egp
				pdc_before = get_account_balance(egp.pdc_payable)
				pi = make_purchase_invoice(1000, posting_date=day(-60))
				party_before = get_party_balance("Supplier", SUPPLIER)

				cheque = make_pdc_cheque(amount=1000, issue_date=day(-60), due_date=day(-30), invoice=pi)
				cheque.deliver(handover_date=day(-59))
				cheque.reload()
				self.assertEqual(get_party_outstanding("Purchase Invoice", pi.name), 0)
				self.assertNotEqual(get_party_balance("Supplier", SUPPLIER), party_before)
				payment_entry = cheque.party_payment_entry

				cheque.bounce(bounce_date=day(-2), reason="Insufficient funds")
				self.assert_status(cheque, "Bounced", "Reversed")
				self.assertEqual(get_party_outstanding("Purchase Invoice", pi.name), 1000)
				self.assertEqual(get_account_balance(egp.pdc_payable), pdc_before)
				# the supplier is back to owing the invoice only
				self.assertEqual(get_party_balance("Supplier", SUPPLIER), party_before)

				if mode == "Cancel Payment Entry":
					self.assertEqual(frappe.db.get_value("Payment Entry", payment_entry, "docstatus"), 2)
					self.assertFalse(cheque.reversal_journal)
				else:
					self.assertEqual(frappe.db.get_value("Payment Entry", payment_entry, "docstatus"), 1)
					assert_gl_entries(
						self,
						"Journal Entry",
						cheque.reversal_journal,
						[
							(self.data.payable, 0, 1000, SUPPLIER, day(-2)),
							(egp.pdc_payable, 1000, 0, None, day(-2)),
						],
					)

	def test_bounce_after_deposit_reverses_both(self):
		for mode in ("Reverse by Journal Entry", "Cancel Payment Entry"):
			with self.subTest(on_bounce=mode):
				self.use_company_settings(on_bounce=mode)
				egp = self.data.egp
				before = {a: get_account_balance(a) for a in (egp.pdc_receivable, egp.under_collection)}

				cheque, si = self.received_incoming()
				cheque.deposit(deposit_date=day(-20))
				cheque.reload()
				payment_entry, deposit_journal = cheque.party_payment_entry, cheque.under_collection_journal

				cheque.bounce(bounce_date=day(-15), reason="Account closed")
				self.assert_status(cheque, "Bounced", "Reversed")
				self.assertEqual(get_party_outstanding("Sales Invoice", si.name), 2000)
				for account, balance in before.items():
					self.assertEqual(get_account_balance(account), balance, account)

				if mode == "Cancel Payment Entry":
					self.assertEqual(frappe.db.get_value("Payment Entry", payment_entry, "docstatus"), 2)
					self.assertEqual(frappe.db.get_value("Journal Entry", deposit_journal, "docstatus"), 2)
				else:
					reversals = frappe.get_all(
						"Journal Entry",
						filters={"treasury_source_name": cheque.name, "treasury_event": ["like", "%_reversal"]},
						pluck="treasury_event",
					)
					self.assertCountEqual(
						reversals,
						[
							lifecycle.EVENT_PARTY + lifecycle.REVERSAL_SUFFIX,
							lifecycle.EVENT_UNDER_COLLECTION + lifecycle.REVERSAL_SUFFIX,
						],
					)

	def test_incoming_bounce_charges(self):
		self.use_company_settings(bounce_charges_account=self.data.egp.bank_charges)
		cheque, _si = self.received_incoming()
		cheque.bounce(bounce_date=day(-10), reason="Insufficient funds", charges=50)
		self.assert_status(cheque, "Bounced", "Reversed")
		assert_gl_entries(
			self,
			"Journal Entry",
			cheque.bounce_charges_journal,
			[
				(self.data.receivable, 50, 0, CUSTOMER, day(-10)),
				(self.data.egp.bank_charges, 0, 50, None, day(-10)),
			],
		)

	def test_outgoing_bounce_charges_are_rejected(self):
		self.use_company_settings(bounce_charges_account=self.data.egp.bank_charges)
		cheque, _pi = self.delivered_outgoing()
		self.assertRaises(frappe.ValidationError, cheque.bounce, day(-2), "Stopped", 50)
		self.assert_status(cheque, "Delivered", "Party Posted")

	# ------------------------------------------------------------------ 8. re-presentation

	def test_represent_after_bounce_starts_a_new_cycle(self):
		cheque, pi = self.delivered_outgoing(issue=-30, due=-10, handover=-30)
		first_payment = cheque.party_payment_entry
		cheque.bounce(bounce_date=day(-8), reason="Insufficient funds")
		self.assertEqual(get_party_outstanding("Purchase Invoice", pi.name), 1000)

		cheque.represent(represent_date=day(-5))
		self.assert_status(cheque, "Delivered", "Party Posted")
		self.assertEqual(cheque.cycle, 2)
		self.assertNotEqual(cheque.party_payment_entry, first_payment)
		self.assertEqual(frappe.db.get_value("Payment Entry", cheque.party_payment_entry, "treasury_cycle"), 2)
		self.assertEqual(self.posting_date("Payment Entry", cheque.party_payment_entry), day(-5))
		self.assertFalse(cheque.reversal_journal)
		self.assertEqual(get_party_outstanding("Purchase Invoice", pi.name), 0)

		cheque.clear(clearance_date=day(-2))
		self.assert_status(cheque, "Cleared", "Bank Posted")
		self.assertEqual(frappe.db.get_value("Journal Entry", cheque.bank_journal, "treasury_cycle"), 2)
		self.assertIn("Re-presented", self.events(cheque))

	def test_represent_with_post_party_creates_new_payment_entry(self):
		self.use_company_settings(outgoing_party_posting="Manual", on_bounce="Cancel Payment Entry")
		cheque = make_pdc_cheque(issue_date=day(-30), due_date=day(-10))
		cheque.deliver(handover_date=day(-30))
		cheque.post_party(posting_date=day(-29))
		cheque.reload()
		first_payment = cheque.party_payment_entry
		cheque.bounce(bounce_date=day(-8), reason="Stopped")
		cheque.represent(represent_date=day(-5), new_due_date=day(15))
		self.assert_status(cheque, "Delivered", "Not Posted")
		self.assertEqual(getdate(cheque.effective_due_date), day(15))

		cheque.post_party()
		cheque.reload()
		self.assertNotEqual(cheque.party_payment_entry, first_payment)
		self.assertEqual(self.posting_date("Payment Entry", cheque.party_payment_entry), day(15))
		self.assertEqual(frappe.db.get_value("Payment Entry", first_payment, "docstatus"), 2)

	# ------------------------------------------------------------------ 9. replacement

	def test_replace_reverses_old_and_links_new(self):
		cheque, pi = self.delivered_outgoing(issue=-30, due=-5, handover=-30)
		new_cheque_no = unique_cheque_no()
		new_name = cheque.replace(
			{"cheque_no": new_cheque_no, "amount": 1000, "original_due_date": day(40), "reason": "Damaged"}
		)

		self.assert_status(cheque, "Replaced", "Reversed")
		self.assertEqual(cheque.replaced_by, new_name)
		self.assertTrue(cheque.reversal_journal)
		self.assertEqual(cheque.status_log[-1].voucher_no, new_name)

		new = frappe.get_doc(DOCTYPE, new_name)
		self.assertEqual(new.replaces, cheque.name)
		self.assertEqual(new.cheque_no, new_cheque_no)
		self.assertEqual((new.party, flt(new.amount)), (SUPPLIER, 1000))
		self.assertEqual(getdate(new.original_due_date), day(40))
		self.assertEqual((new.operational_status, new.accounting_status), ("Delivered", "Party Posted"))
		self.assertNotEqual(new.party_payment_entry, cheque.party_payment_entry)
		self.assertEqual(get_party_outstanding("Purchase Invoice", pi.name), 0)

	# ------------------------------------------------------------------ 10. cancellation

	def test_cancel_from_created(self):
		cheque = make_pdc_cheque()
		cheque.cancel_cheque(reason="Written by mistake")
		self.assert_status(cheque, "Cancelled", "Not Posted")
		self.assertRaises(frappe.ValidationError, cheque.cancel_cheque, "Again")

	def test_cancel_requires_reversal_after_posting(self):
		cheque, _pi = self.delivered_outgoing()
		self.assertRaises(frappe.ValidationError, cheque.cancel_cheque, "No")
		self.assert_status(cheque, "Delivered", "Party Posted")

		cheque.bounce(bounce_date=day(-2), reason="Stopped")
		cheque.cancel_cheque(reason="Will not be re-presented")
		self.assert_status(cheque, "Cancelled", "Reversed")

	def test_return_to_party_reverses_receipt(self):
		cheque, si = self.received_incoming()
		cheque.return_to_party(return_date=day(-30), reason="Customer paid cash")
		self.assert_status(cheque, "Returned", "Reversed")
		self.assertEqual(get_party_outstanding("Sales Invoice", si.name), 2000)

		deposited, _si = self.received_incoming()
		deposited.deposit(deposit_date=day(-20))
		self.assertRaises(frappe.ValidationError, deposited.return_to_party, day(-19), "Too late")

	# ------------------------------------------------------------------ 11. idempotency

	def test_repeated_actions_raise_and_create_nothing(self):
		"""Documented choice: repeating an action raises an "already ..." error; no voucher is duplicated."""
		cheque, _pi = self.delivered_outgoing()
		self.assertRaises(frappe.ValidationError, cheque.deliver, day(-58))
		self.assertRaises(frappe.ValidationError, cheque.post_party)
		self.assertEqual(self.vouchers(cheque), 1)

		cheque.clear(clearance_date=day(-5))
		self.assertRaises(frappe.ValidationError, cheque.clear, day(-4))
		self.assertEqual(self.vouchers(cheque), 2)
		self.assertEqual(self.events(cheque).count("Party Posted"), 1)

		incoming, _si = self.received_incoming()
		incoming.deposit(deposit_date=day(-20))
		self.assertRaises(frappe.ValidationError, incoming.deposit, day(-19))
		self.assertRaises(frappe.ValidationError, incoming.receive, day(-19))
		self.assertEqual(self.vouchers(incoming), 2)

	def test_direction_specific_actions(self):
		outgoing = make_pdc_cheque()
		self.assertRaises(frappe.ValidationError, outgoing.receive, day(0))
		incoming = make_pdc_cheque("Incoming")
		self.assertRaises(frappe.ValidationError, incoming.deliver, day(0))

	# ------------------------------------------------------------------ 12. permissions

	def test_role_restricted_actions(self):
		self.use_company_settings(outgoing_party_posting="On Cheque Date")
		user = ensure_user("treasury.user@example.com", ["Treasury User"])
		approver = ensure_user("treasury.approver@example.com", ["Treasury User", "Cheque Approver"])
		cheque = make_pdc_cheque(issue_date=day(-10), due_date=day(20))

		with self.set_user(user):
			cheque.deliver(handover_date=day(-10))
			self.assertRaises(frappe.PermissionError, cheque.bounce, day(-1), "Stopped")
			self.assertRaises(frappe.PermissionError, cheque.cancel_cheque, "No")
			self.assertRaises(frappe.PermissionError, cheque.replace, {"cheque_no": unique_cheque_no()})
			self.assertRaises(frappe.PermissionError, cheque.postpone, day(30), "Asked")
		self.assert_status(cheque, "Delivered", "Not Posted")
		self.assertFalse(cheque.is_postponed)

		with self.set_user(approver):
			cheque.postpone(new_due_date=day(30), reason="Asked")
		cheque.reload()
		self.assertEqual(getdate(cheque.effective_due_date), day(30))
		self.assertEqual(cheque.postponements[0].approved_by, approver)

	# ------------------------------------------------------------------ 15. multi-currency

	def test_usd_cheque_with_exchange_difference_at_clearance(self):
		usd = self.data.usd
		gain_loss = frappe.get_cached_value("Company", COMPANY, "exchange_gain_loss_account")
		self.assertTrue(gain_loss, "the test company needs an Exchange Gain / Loss account")
		bank_before = get_account_balance(usd.bank_gl)
		pdc_before = get_account_balance(usd.pdc_payable)

		pi = make_purchase_invoice(5000, posting_date=day(-20))
		# allocations are in the party account currency (EGP)
		cheque = make_pdc_cheque(
			amount=100,
			currency="USD",
			issue_date=day(-20),
			due_date=day(-5),
			exchange_rate=50,
			references=[
				{"reference_doctype": "Purchase Invoice", "reference_name": pi.name, "allocated_amount": 5000}
			],
		)
		self.assertEqual(cheque.currency, "USD")

		cheque.deliver(handover_date=day(-20))
		cheque.reload()
		assert_gl_entries(
			self,
			"Payment Entry",
			cheque.party_payment_entry,
			[(self.data.payable, 5000, 0, SUPPLIER), (usd.pdc_payable, 0, 5000, None)],
		)
		self.assertEqual(get_party_outstanding("Purchase Invoice", pi.name), 0)

		cheque.clear(clearance_date=day(-3), exchange_rate=52)
		cheque.reload()
		assert_gl_entries(
			self,
			"Journal Entry",
			cheque.bank_journal,
			[(usd.pdc_payable, 5000, 0), (usd.bank_gl, 0, 5200), (gain_loss, 200, 0)],
		)
		# balances in the account currency (USD)
		self.assertEqual(get_account_balance(usd.bank_gl), flt(bank_before - 100))
		self.assertEqual(get_account_balance(usd.pdc_payable), pdc_before)
		self.assert_status(cheque, "Cleared", "Bank Posted")
