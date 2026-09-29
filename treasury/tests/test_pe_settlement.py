"""Phase 7a: under collection and bank settlement as Internal Transfer Payment Entries (Bank Settlement
Voucher = Payment Entry), the bank charges inside the bank Payment Entry, reversal by reverse Payment Entries,
and the mixed history of cheques posted in Journal Entry mode (spec 7a §4 E2–E6, §7 A)."""

import frappe
from frappe import _
from frappe.tests import IntegrationTestCase
from frappe.utils import flt, getdate

from treasury.accounting import posting
from treasury.pdc import lifecycle
from treasury.tests.utils import (
	CUSTOMER,
	PE_SETTINGS,
	SUPPLIER,
	TreasuryTestMixin,
	assert_deductions,
	assert_gl_entries,
	day,
	get_account_balance,
	get_party_outstanding,
	get_pe,
	get_voucher_count,
	make_pdc_cheque,
	make_sales_invoice,
)

DOCTYPE = "PDC Cheque"


class PESettlementTestCase(TreasuryTestMixin, IntegrationTestCase):
	def setUp(self):
		super().setUp()
		self.use_company_settings(**PE_SETTINGS)

	# ------------------------------------------------------------------ helpers

	def assert_status(self, doc, operational: str, accounting: str):
		doc.reload()
		self.assertEqual((doc.operational_status, doc.accounting_status), (operational, accounting))

	def delivered_outgoing(self, amount=10000, issue=-60, due=-30, handover=-59):
		cheque = make_pdc_cheque(amount=amount, issue_date=day(issue), due_date=day(due))
		cheque.deliver(handover_date=day(handover))
		cheque.reload()
		return cheque

	def received_incoming(self, amount=20000, issue=-50, due=-20, receipt=-49, invoice=True, **values):
		si = make_sales_invoice(amount, posting_date=day(issue)) if invoice else None
		cheque = make_pdc_cheque(
			"Incoming", amount=amount, issue_date=day(issue), due_date=day(due), invoice=si, **values
		)
		cheque.receive(receipt_date=day(receipt))
		cheque.reload()
		return cheque, si

	def clearance_date(self, name: str):
		value = frappe.db.get_value("Payment Entry", name, "clearance_date")
		return getdate(value) if value else None

	def messages(self) -> str:
		return " ".join(str(m.get("message") if isinstance(m, dict) else m) for m in frappe.local.message_log)


class TestPESettlement(PESettlementTestCase):
	# ------------------------------------------------------------------ outgoing

	def test_outgoing_clear_pe_no_charges(self):
		egp = self.data.egp
		cheque = self.delivered_outgoing()
		cheque.clear(clearance_date=day(10))
		self.assert_status(cheque, "Cleared", "Bank Posted")

		self.assertTrue(cheque.bank_payment_entry)
		self.assertFalse(cheque.bank_journal)
		self.assertFalse(cheque.bank_charges_journal)
		pe = get_pe(cheque.bank_payment_entry)
		self.assertEqual((pe.payment_type, pe.docstatus), ("Internal Transfer", 1))
		self.assertEqual((pe.treasury_event, pe.treasury_cycle), (lifecycle.EVENT_BANK, 1))
		self.assertEqual((pe.reference_no, getdate(pe.reference_date)), (cheque.cheque_no, day(-30)))
		assert_gl_entries(
			self,
			"Payment Entry",
			pe.name,
			[(egp.bank_gl, 0, 10000, None, day(10)), (egp.pdc_payable, 10000, 0, None, day(10))],
		)
		self.assertEqual(self.clearance_date(pe.name), day(10))
		self.assertEqual(getdate(cheque.bank_posting_date), day(10))
		log = cheque.status_log[-1]
		self.assertEqual(
			(log.event, log.voucher_type, log.voucher_no), ("Bank Posted", "Payment Entry", pe.name)
		)

	def test_outgoing_clear_pe_with_charges(self):
		egp = self.data.egp
		cheque = self.delivered_outgoing()
		cheque.clear(clearance_date=day(10), bank_charges=15)
		self.assert_status(cheque, "Cleared", "Bank Posted")

		# one Payment Entry for the settlement and its charges
		self.assertEqual(get_voucher_count(DOCTYPE, cheque.name), 2)
		self.assertFalse(cheque.bank_charges_journal)
		pe = get_pe(cheque.bank_payment_entry)
		self.assertEqual((flt(pe.paid_amount), flt(pe.received_amount)), (10015, 10000))
		assert_deductions(self, pe, [(egp.bank_charges, 15, 1)])
		self.assertEqual(flt(pe.difference_amount), 0)
		assert_gl_entries(
			self,
			"Payment Entry",
			pe.name,
			[
				(egp.bank_gl, 0, 10015, None, day(10)),
				(egp.pdc_payable, 10000, 0, None, day(10)),
				(egp.bank_charges, 15, 0, None, day(10)),
			],
		)
		events = [(row.event, row.voucher_no) for row in cheque.status_log[-2:]]
		self.assertEqual(events, [("Bank Posted", pe.name), ("Bank Charges Posted", pe.name)])

	# ------------------------------------------------------------------ incoming

	def test_incoming_under_collection_pe(self):
		egp = self.data.egp
		cheque, _si = self.received_incoming()
		cheque.deposit(deposit_date=day(-20))
		self.assert_status(cheque, "Deposited", "Under Collection Posted")

		self.assertTrue(cheque.under_collection_payment_entry)
		self.assertFalse(cheque.under_collection_journal)
		pe = get_pe(cheque.under_collection_payment_entry)
		self.assertEqual(
			(pe.payment_type, pe.paid_from, pe.paid_to),
			("Internal Transfer", egp.pdc_receivable, egp.under_collection),
		)
		self.assertEqual(pe.treasury_event, lifecycle.EVENT_UNDER_COLLECTION)
		assert_gl_entries(
			self,
			"Payment Entry",
			pe.name,
			[(egp.under_collection, 20000, 0, None, day(-20)), (egp.pdc_receivable, 0, 20000, None, day(-20))],
		)
		self.assertIsNone(self.clearance_date(pe.name))
		log = cheque.status_log[-1]
		self.assertEqual(
			(log.event, log.voucher_type, log.voucher_no), ("Under Collection Posted", "Payment Entry", pe.name)
		)

	def test_incoming_clear_after_uc_with_charges(self):
		egp = self.data.egp
		before = {a: get_account_balance(a) for a in (egp.pdc_receivable, egp.under_collection)}
		cheque, _si = self.received_incoming()
		cheque.deposit(deposit_date=day(-20))
		cheque.clear(clearance_date=day(-18), bank_charges=25)
		self.assert_status(cheque, "Cleared", "Bank Posted")

		pe = get_pe(cheque.bank_payment_entry)
		self.assertEqual((pe.paid_from, pe.paid_to), (egp.under_collection, egp.bank_gl))
		self.assertEqual((flt(pe.paid_amount), flt(pe.received_amount)), (20000, 19975))
		assert_deductions(self, pe, [(egp.bank_charges, 25, 1)])
		assert_gl_entries(
			self,
			"Payment Entry",
			pe.name,
			[
				(egp.bank_gl, 19975, 0, None, day(-18)),
				(egp.bank_charges, 25, 0, None, day(-18)),
				(egp.under_collection, 0, 20000, None, day(-18)),
			],
		)
		self.assertEqual(self.clearance_date(pe.name), day(-18))
		self.assertEqual(get_voucher_count(DOCTYPE, cheque.name), 3)
		for account, balance in before.items():
			self.assertEqual(get_account_balance(account), balance, account)

	def test_incoming_uc_disabled_clear(self):
		self.use_company_settings(incoming_under_collection="Disabled")
		egp = self.data.egp
		cheque, _si = self.received_incoming()
		cheque.deposit(deposit_date=day(-20))
		self.assert_status(cheque, "Deposited", "Party Posted")
		cheque.clear(clearance_date=day(-18))
		self.assert_status(cheque, "Cleared", "Bank Posted")

		pe = get_pe(cheque.bank_payment_entry)
		self.assertEqual((pe.paid_from, pe.paid_to), (egp.pdc_receivable, egp.bank_gl))
		assert_gl_entries(
			self,
			"Payment Entry",
			pe.name,
			[(egp.bank_gl, 20000, 0, None, day(-18)), (egp.pdc_receivable, 0, 20000, None, day(-18))],
		)
		self.assertEqual(get_voucher_count(DOCTYPE, cheque.name), 2)

	# ------------------------------------------------------------------ bank posting modes × charges

	def test_bank_posting_modes_and_charges(self):
		egp = self.data.egp
		modes = ("On Actual Clearance", "On Bank Transaction Date", "Manual")
		for direction in ("Outgoing", "Incoming"):
			for mode in modes:
				for charges in (0, 10):
					with self.subTest(direction=direction, mode=mode, charges=charges):
						prefix = "outgoing" if direction == "Outgoing" else "incoming"
						self.use_company_settings(**{f"{prefix}_bank_posting": mode})
						if direction == "Outgoing":
							cheque = self.delivered_outgoing(amount=1000)
						else:
							cheque, _si = self.received_incoming(amount=1000, invoice=False)
						transaction_date = day(-8) if mode == "On Bank Transaction Date" else None
						cheque.clear(
							clearance_date=day(-10), bank_transaction_date=transaction_date, bank_charges=charges
						)
						if mode == "Manual":
							self.assert_status(cheque, "Cleared", "Party Posted")
							self.assertFalse(cheque.bank_payment_entry)
							cheque.post_bank()
						self.assert_status(cheque, "Cleared", "Bank Posted")

						posting_date = transaction_date or day(-10)
						pe = get_pe(cheque.bank_payment_entry)
						if direction == "Outgoing":
							expected = [
								(egp.bank_gl, 0, 1000 + charges, None, posting_date),
								(egp.pdc_payable, 1000, 0, None, posting_date),
							]
						else:
							expected = [
								(egp.bank_gl, 1000 - charges, 0, None, posting_date),
								(egp.pdc_receivable, 0, 1000, None, posting_date),
							]
						if charges:
							expected.append((egp.bank_charges, charges, 0, None, posting_date))
						assert_gl_entries(self, "Payment Entry", pe.name, expected)
						assert_deductions(self, pe, [(egp.bank_charges, charges, 1)] if charges else [])
						self.assertEqual(self.clearance_date(pe.name), transaction_date or day(-10))
						self.assertEqual(getdate(cheque.bank_posting_date), posting_date)
						self.assertFalse(cheque.bank_journal or cheque.bank_charges_journal)

	# ------------------------------------------------------------------ foreign currency

	def test_usd_incoming_pe_carrying_rate(self):
		self.use_company_settings(incoming_under_collection="Disabled")
		usd = self.data.usd
		cheque, _si = self.received_incoming(amount=1000, invoice=False, currency="USD", exchange_rate=50)
		self.assertEqual((cheque.currency, flt(cheque.exchange_rate)), ("USD", 50))

		frappe.clear_messages()
		cheque.clear(clearance_date=day(-18), bank_charges=5, exchange_rate=52)
		warning = _(
			"In Payment Entry mode the cheque is settled at its carrying rate {0}; the exchange difference is booked by Exchange Rate Revaluation."
		).format("50")  # formatted: "50", not the raw float "50.0"
		self.assertIn(warning, self.messages())
		self.assert_status(cheque, "Cleared", "Bank Posted")

		pe = get_pe(cheque.bank_payment_entry)
		self.assertEqual((flt(pe.source_exchange_rate), flt(pe.target_exchange_rate)), (50, 50))
		self.assertEqual((flt(pe.paid_amount), flt(pe.received_amount)), (1000, 995))
		assert_deductions(self, pe, [(usd.bank_charges, 250, 1)])
		assert_gl_entries(
			self,
			"Payment Entry",
			pe.name,
			[
				(usd.bank_gl, 49750, 0, None, day(-18)),
				(usd.bank_charges, 250, 0, None, day(-18)),
				(usd.pdc_receivable, 0, 50000, None, day(-18)),
			],
		)

	def test_incoming_charges_not_less_than_amount(self):
		cheque, _si = self.received_incoming(amount=1000, invoice=False)
		self.assertRaises(frappe.ValidationError, cheque.clear, clearance_date=day(-18), bank_charges=1000)
		self.assert_status(cheque, "Received", "Party Posted")
		# Journal Entry mode: the charges are a voucher of their own, any amount is accepted
		self.use_company_settings(bank_settlement_voucher="Journal Entry")
		cheque.clear(clearance_date=day(-18), bank_charges=1000)
		self.assert_status(cheque, "Cleared", "Bank Posted")

	# ------------------------------------------------------------------ bounce / re-present

	def test_bounce_after_deposit_reverse_mode(self):
		egp = self.data.egp
		before = {a: get_account_balance(a) for a in (egp.pdc_receivable, egp.under_collection)}
		cheque, si = self.received_incoming()
		cheque.deposit(deposit_date=day(-20))
		self.assertEqual(get_party_outstanding("Sales Invoice", si.name), 0)

		cheque.bounce(bounce_date=day(-15), reason="Insufficient funds")
		self.assert_status(cheque, "Bounced", "Reversed")

		reverse = get_pe(cheque.reversal_payment_entry)
		self.assertEqual(
			(reverse.payment_type, reverse.paid_from, reverse.paid_to, flt(reverse.paid_amount)),
			("Internal Transfer", egp.under_collection, egp.pdc_receivable, 20000),
		)
		self.assertEqual(reverse.treasury_event, lifecycle.EVENT_UNDER_COLLECTION + lifecycle.REVERSAL_SUFFIX)
		self.assertIsNone(self.clearance_date(reverse.name))
		assert_gl_entries(
			self,
			"Payment Entry",
			reverse.name,
			[(egp.pdc_receivable, 20000, 0, None, day(-15)), (egp.under_collection, 0, 20000, None, day(-15))],
		)
		assert_gl_entries(
			self,
			"Journal Entry",
			cheque.reversal_journal,
			[
				(self.data.receivable, 20000, 0, CUSTOMER, day(-15)),
				(egp.pdc_receivable, 0, 20000, None, day(-15)),
			],
		)
		self.assertEqual(get_party_outstanding("Sales Invoice", si.name), 20000)
		for account, balance in before.items():
			self.assertEqual(get_account_balance(account), balance, account)
		reversed_ = [(r.voucher_type, r.voucher_no) for r in cheque.status_log if r.event == "Voucher Reversed"]
		self.assertEqual(
			reversed_, [("Payment Entry", reverse.name), ("Journal Entry", cheque.reversal_journal)]
		)

	def test_bounce_after_deposit_cancel_mode(self):
		self.use_company_settings(on_bounce="Cancel Payment Entry")
		cheque, si = self.received_incoming()
		cheque.deposit(deposit_date=day(-20))
		cheque.reload()
		party_pe, uc_pe = cheque.party_payment_entry, cheque.under_collection_payment_entry
		self.assertTrue(party_pe and uc_pe)

		cheque.bounce(bounce_date=day(-15), reason="Insufficient funds")
		self.assert_status(cheque, "Bounced", "Reversed")
		for name in (party_pe, uc_pe):
			self.assertEqual(frappe.db.get_value("Payment Entry", name, "docstatus"), 2)
		self.assertEqual(get_voucher_count(DOCTYPE, cheque.name), 2)
		self.assertFalse(cheque.reversal_payment_entry or cheque.reversal_journal)
		self.assertEqual(get_party_outstanding("Sales Invoice", si.name), 20000)

	def test_represent_after_bounce_pe(self):
		cheque, _si = self.received_incoming()
		cheque.deposit(deposit_date=day(-20))
		cheque.reload()
		first = (cheque.party_payment_entry, cheque.under_collection_payment_entry)
		self.assertTrue(all(first))
		cheque.bounce(bounce_date=day(-15), reason="Insufficient funds")
		cheque.reload()
		reverse = cheque.reversal_payment_entry

		cheque.represent(represent_date=day(-10))
		cheque.reload()
		self.assertEqual(cheque.cycle, 2)
		self.assertFalse(cheque.under_collection_payment_entry or cheque.reversal_payment_entry)
		cheque.deposit(deposit_date=day(-8))
		cheque.clear(clearance_date=day(-5))
		self.assert_status(cheque, "Cleared", "Bank Posted")

		second = (cheque.party_payment_entry, cheque.under_collection_payment_entry)
		self.assertTrue(all(second))
		self.assertFalse(set(first) & set(second))
		for name in (*second, cheque.bank_payment_entry):
			self.assertEqual(frappe.db.get_value("Payment Entry", name, "treasury_cycle"), 2)
		# the first cycle's vouchers are kept
		for name in (*first, reverse):
			values = frappe.db.get_value("Payment Entry", name, ["docstatus", "treasury_cycle"])
			self.assertEqual(tuple(values), (1, 1), name)

	# ------------------------------------------------------------------ mixed history (E6)

	def test_mixed_je_uc_then_pe_bank(self):
		egp = self.data.egp
		self.use_company_settings(bank_settlement_voucher="Journal Entry")
		cleared, _si = self.received_incoming()
		cleared.deposit(deposit_date=day(-20))
		bounced, _si = self.received_incoming()
		bounced.deposit(deposit_date=day(-20))
		for cheque in (cleared, bounced):
			cheque.reload()
			self.assertTrue(cheque.under_collection_journal)
			self.assertFalse(cheque.under_collection_payment_entry)

		self.use_company_settings(bank_settlement_voucher="Payment Entry")
		# the bank Payment Entry takes the cheque out of Under Collection, where the Journal Entry left it
		cleared.clear(clearance_date=day(-18))
		self.assert_status(cleared, "Cleared", "Bank Posted")
		pe = get_pe(cleared.bank_payment_entry)
		self.assertEqual((pe.paid_from, pe.paid_to), (egp.under_collection, egp.bank_gl))
		self.assertFalse(cleared.bank_journal)

		# a Journal Entry is reversed by a Journal Entry, whatever the current setting
		bounced.bounce(bounce_date=day(-15), reason="Insufficient funds")
		self.assert_status(bounced, "Bounced", "Reversed")
		self.assertFalse(bounced.reversal_payment_entry)
		reversal = frappe.get_all(
			"Journal Entry",
			filters={
				"treasury_source_name": bounced.name,
				"treasury_event": lifecycle.EVENT_UNDER_COLLECTION + lifecycle.REVERSAL_SUFFIX,
				"docstatus": 1,
			},
			pluck="name",
		)
		self.assertEqual(len(reversal), 1)
		assert_gl_entries(
			self,
			"Journal Entry",
			reversal[0],
			[(egp.pdc_receivable, 20000, 0, None, day(-15)), (egp.under_collection, 0, 20000, None, day(-15))],
		)
		self.assertEqual(frappe.db.count("Payment Entry", {"treasury_source_name": bounced.name}), 1)

	# ------------------------------------------------------------------ idempotency

	def test_idempotency(self):
		egp = self.data.egp
		cheque, _si = self.received_incoming(invoice=False)
		values = {
			"source_doctype": DOCTYPE,
			"source_name": cheque.name,
			"event": "test_internal_transfer",
			"company": cheque.company,
			"posting_date": day(-30),
			"paid_from": egp.pdc_receivable,
			"paid_to": egp.under_collection,
			"paid_amount": 100,
			"reference_no": cheque.cheque_no,
			"reference_date": day(-30),
		}
		first = posting.create_internal_transfer(**values)
		self.assertEqual(posting.create_internal_transfer(**values), first)
		self.assertEqual(frappe.db.count("Payment Entry", {"treasury_event": "test_internal_transfer"}), 1)

		event = "test_internal_transfer_reversal"
		reverse = posting.make_reverse_payment_entry(first, day(-29), event)
		self.assertEqual(posting.make_reverse_payment_entry(first, day(-29), event), reverse)
		self.assertEqual(
			posting.reverse_voucher("Payment Entry", first, day(-29), event), ("Payment Entry", reverse)
		)
		original = get_pe(first)
		mirror = get_pe(reverse)
		self.assertEqual((mirror.paid_from, mirror.paid_to), (original.paid_to, original.paid_from))
		self.assertEqual(mirror.naming_series, original.naming_series)
		self.assertEqual(
			frappe.db.count("Payment Entry", {"treasury_event": "test_internal_transfer_reversal"}), 1
		)

		cheque.clear(clearance_date=day(-18))
		with self.assertRaisesRegex(frappe.ValidationError, "already"):
			cheque.clear(clearance_date=day(-18))

		self.use_company_settings(outgoing_bank_posting="Manual")
		outgoing = self.delivered_outgoing(amount=500)
		outgoing.clear(clearance_date=day(-10))
		outgoing.post_bank()
		with self.assertRaisesRegex(frappe.ValidationError, "already posted"):
			outgoing.post_bank()
		self.assertEqual(get_voucher_count(DOCTYPE, outgoing.name), 2)

	def test_reverse_pe_rejects_deductions(self):
		egp = self.data.egp
		cheque = self.delivered_outgoing()
		cheque.clear(clearance_date=day(-10), bank_charges=15)
		cheque.reload()
		bank_pe = cheque.bank_payment_entry

		self.assertRaises(
			frappe.ValidationError, posting.make_reverse_payment_entry, bank_pe, day(-5), "test_bank_reversal"
		)
		doctype, reversal = posting.reverse_voucher("Payment Entry", bank_pe, day(-5), "test_bank_reversal")
		self.assertEqual(doctype, "Journal Entry")
		assert_gl_entries(
			self,
			"Journal Entry",
			reversal,
			[
				(egp.bank_gl, 10015, 0, None, day(-5)),
				(egp.pdc_payable, 0, 10000, None, day(-5)),
				(egp.bank_charges, 0, 15, None, day(-5)),
			],
		)
		# a party Payment Entry is never reversed by a Payment Entry
		self.assertRaises(
			frappe.ValidationError,
			posting.make_reverse_payment_entry,
			cheque.party_payment_entry,
			day(-5),
			"test_party_reversal",
		)

	def test_voucher_guard_it_pe(self):
		cheque, _si = self.received_incoming()
		cheque.deposit(deposit_date=day(-20))
		cheque.reload()
		pe = get_pe(cheque.under_collection_payment_entry)
		with self.assertRaisesRegex(frappe.ValidationError, "managed by"):
			pe.cancel()
		self.assertEqual(frappe.db.get_value("Payment Entry", pe.name, "docstatus"), 1)

	def test_internal_transfer_validations(self):
		egp, usd = self.data.egp, self.data.usd
		cheque, _si = self.received_incoming(invoice=False)
		values = {
			"source_doctype": DOCTYPE,
			"source_name": cheque.name,
			"event": "test_internal_transfer_checks",
			"company": cheque.company,
			"posting_date": day(-30),
			"paid_from": egp.pdc_receivable,
			"paid_to": egp.under_collection,
			"paid_amount": 100,
			"reference_no": cheque.cheque_no,
			"reference_date": day(-30),
		}
		for changes in (
			{"paid_to": egp.pdc_receivable},  # same account
			{"paid_to": usd.under_collection},  # other currency
			{"paid_amount": 0},
			{"received_amount": 150},  # more than paid
			{"received_amount": 90},  # difference without an account
			{"received_amount": 90, "deduction_account": usd.pdc_receivable},  # not in the company currency
			{"submit": False, "clearance_date": day(-30)},
		):
			with self.subTest(changes=changes):
				self.assertRaises(
					frappe.ValidationError, posting.create_internal_transfer, **{**values, **changes}
				)
		self.assertFalse(frappe.db.exists("Payment Entry", {"treasury_event": "test_internal_transfer_checks"}))

	# ------------------------------------------------------------------ Journal Entry mode

	def test_je_mode_unchanged(self):
		self.use_company_settings(bank_settlement_voucher="Journal Entry")
		egp = self.data.egp
		cheque = self.delivered_outgoing()
		cheque.clear(clearance_date=day(-10), bank_charges=15)
		self.assert_status(cheque, "Cleared", "Bank Posted")

		self.assertFalse(cheque.bank_payment_entry)
		assert_gl_entries(
			self,
			"Journal Entry",
			cheque.bank_journal,
			[(egp.pdc_payable, 10000, 0, None, day(-10)), (egp.bank_gl, 0, 10000, None, day(-10))],
		)
		assert_gl_entries(
			self,
			"Journal Entry",
			cheque.bank_charges_journal,
			[(egp.bank_charges, 15, 0, None, day(-10)), (egp.bank_gl, 0, 15, None, day(-10))],
		)
		self.assertEqual(frappe.db.get_value("Journal Entry", cheque.bank_journal, "voucher_type"), "Bank Entry")
		self.assertEqual(get_voucher_count(DOCTYPE, cheque.name), 3)
		self.assertEqual(frappe.db.get_value("Payment Entry", cheque.party_payment_entry, "party"), SUPPLIER)
