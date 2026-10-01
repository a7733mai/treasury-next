"""Phase 9a: cheques paying out and returning the custody of a Custody Request (spec 9 §4 E1-E6, §8 9a).

Skipped when the custody app is not installed. Every test checks custody's invariants on tearDown: stored,
recomputed and ledger balances of the requests agree, and no custodian GL row is orphaned.
"""

import unittest

import frappe
from frappe import _
from frappe.tests import IntegrationTestCase
from frappe.utils import cint, flt, fmt_money, getdate, today

from treasury.accounting import posting
from treasury.integrations.custody import REQUEST, api, compat, documents, is_installed
from treasury.pdc import lifecycle
from treasury.tests.custody_utils import (
	ACCOUNTANT,
	as_user,
	assert_request,
	build_cheque,
	cheque_from_request,
	custody_account,
	custody_payment_entries,
	ensure_custody_setup,
	invariants,
	make_custodian,
	make_request,
	make_settlement,
	mapped_cheque,
	use_custody_settings,
)
from treasury.tests.utils import (
	COMPANY,
	PE_SETTINGS,
	TreasuryTestMixin,
	assert_gl_entries,
	assert_party_balance,
	bank_account_name,
	ensure_user,
	get_pe,
	unique_cheque_no,
)

DOCTYPE = "PDC Cheque"
TREASURY_ONLY = "trs-only@treasury.test"


class CustodyChequeTestCase(TreasuryTestMixin, IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		if not is_installed():
			raise unittest.SkipTest("custody not installed")
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.custody = ensure_custody_setup()
		cls.cost_center = frappe.get_cached_value("Company", COMPANY, "cost_center")

	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		self.use_company_settings(**PE_SETTINGS)

	def tearDown(self):
		frappe.set_user("Administrator")
		self.assertEqual(invariants(), [], "custody invariants broken")
		super().tearDown()

	# ------------------------------------------------------------------ helpers

	@property
	def cus(self) -> str:
		return custody_account()

	def request(self, amount=10000, **values):
		_employee, custodian = make_custodian()
		return make_request(custodian, amount, cost_center=self.cost_center, **values)

	def delivered(self, req, amount=None):
		cheque = cheque_from_request(req.name, amount=amount)
		cheque.deliver(handover_date=today())
		cheque.reload()
		return cheque

	def returned(self, settled=7000):
		"""A request of 10,000 paid by cheque, settled `settled`, and the cheque returning the rest, received."""
		req = self.request()
		self.delivered(req)
		make_settlement(req.name, settled)
		cheque = cheque_from_request(req.name, "Incoming")
		cheque.receive(receipt_date=today())
		cheque.reload()
		return req, cheque

	def e1(self, req, amount=10000, date=None):
		date = getdate(date or today())
		return [
			(self.cus, amount, 0, req.custodian, date),
			(self.data.egp.pdc_payable, 0, amount, None, date),
		]

	def assert_status(self, cheque, operational, accounting):
		cheque.reload()
		self.assertEqual((cheque.operational_status, cheque.accounting_status), (operational, accounting))

	def assert_refused(self, fn, message, exc=frappe.ValidationError):
		with self.assertRaises(exc) as cm:
			fn()
		self.assertIn(message, str(cm.exception))

	def messages(self) -> str:
		"""Titles and texts of the messages shown so far (custody's refusal and Treasury's message)."""
		parts = []
		for message in frappe.local.message_log:
			if isinstance(message, str):
				message = frappe.parse_json(message)
			parts.append(f"{message.get('title') or ''} {message.get('message') or ''}")
		return " ".join(parts)


class TestCustodyCheques(CustodyChequeTestCase):
	# ------------------------------------------------------------------ payout (E1, E2)

	def test_pay_by_cheque_on_handover(self):
		req = self.request()
		data = mapped_cheque(req.name)
		self.assertEqual(flt(data["amount"]), 10000)
		self.assertEqual((data["source_doctype"], data["source_name"]), (REQUEST, req.name))
		self.assertEqual((data["party_type"], data["party"]), ("Custodian", req.custodian))
		self.assertEqual((data["direction"], data["currency"]), ("Outgoing", "EGP"))
		self.assertFalse(data.get("references"))
		self.assertEqual(data["cost_center"], self.cost_center)

		cheque = self.delivered(req)
		self.assert_status(cheque, "Delivered", "Party Posted")
		pe = get_pe(cheque.party_payment_entry)
		self.assertEqual((pe.payment_type, pe.party_type, pe.party), ("Pay", "Custodian", req.custodian))
		self.assertEqual((pe.custody_request, pe.custody_purpose), (req.name, "Disbursement"))
		self.assertEqual((pe.paid_from, pe.paid_to), (self.data.egp.pdc_payable, self.cus))
		self.assertFalse(pe.references)
		self.assertFalse(pe.deductions)
		self.assertEqual((pe.reference_no, getdate(pe.reference_date)), (cheque.cheque_no, getdate(today())))
		assert_gl_entries(self, "Payment Entry", pe.name, self.e1(req))
		assert_request(self, req.name, paid_amount=10000, remaining_to_pay=0, balance_amount=10000, status="Paid")
		assert_party_balance(self, "Custodian", req.custodian, today(), 10000)

	def test_party_posting_modes(self):
		for mode in ("On Creation", "On Handover", "On Cheque Date", "Manual"):
			with self.subTest(mode=mode):
				self.use_company_settings(outgoing_party_posting=mode)
				req = self.request()
				cheque = cheque_from_request(req.name)
				if mode != "On Creation":
					cheque.deliver(handover_date=today())
				if mode == "On Cheque Date":
					lifecycle.post_party(cheque.name, system=True)
				elif mode == "Manual":
					cheque.reload()
					cheque.post_party()
				operational = "Created" if mode == "On Creation" else "Delivered"
				self.assert_status(cheque, operational, "Party Posted")
				self.assertEqual(getdate(cheque.party_posting_date), getdate(today()))
				assert_gl_entries(self, "Payment Entry", cheque.party_payment_entry, self.e1(req))
				assert_request(self, req.name, paid_amount=10000, remaining_to_pay=0, status="Paid")

	def test_clearing_pe_and_je_modes(self):
		egp = self.data.egp
		for voucher in ("Payment Entry", "Journal Entry"):
			with self.subTest(voucher=voucher):
				self.use_company_settings(bank_settlement_voucher=voucher)
				req = self.request()
				cheque = self.delivered(req)
				cheque.clear(clearance_date=today())
				self.assert_status(cheque, "Cleared", "Bank Posted")
				name = cheque.bank_payment_entry if voucher == "Payment Entry" else cheque.bank_journal
				self.assertTrue(name)
				assert_gl_entries(
					self, voucher, name, [(egp.pdc_payable, 10000, 0, None), (egp.bank_gl, 0, 10000, None)]
				)
				assert_request(self, req.name, paid_amount=10000, balance_amount=10000, status="Paid")

	# ------------------------------------------------------------------ undo by cancelling (E3)

	def test_bounce_cancels_party_pe(self):
		for on_bounce in ("Cancel Payment Entry", "Reverse by Journal Entry"):
			with self.subTest(on_bounce=on_bounce):
				self.use_company_settings(on_bounce=on_bounce)
				req = self.request()
				cheque = self.delivered(req)
				pe = cheque.party_payment_entry
				cheque.bounce(bounce_date=today(), reason="Bounced")
				self.assert_status(cheque, "Bounced", "Reversed")
				self.assertEqual(frappe.db.get_value("Payment Entry", pe, "docstatus"), 2)
				self.assertFalse(cheque.reversal_journal)
				self.assertFalse(
					frappe.get_all(
						"Journal Entry",
						filters={"treasury_source_doctype": DOCTYPE, "treasury_source_name": cheque.name},
					)
				)
				cancelled = [r for r in cheque.status_log if r.event == "Voucher Cancelled"]
				self.assertEqual([(r.voucher_type, r.voucher_no) for r in cancelled], [("Payment Entry", pe)])
				assert_request(
					self, req.name, paid_amount=0, remaining_to_pay=10000, balance_amount=0, status="Approved"
				)

	def test_bounce_refused_when_custody_used(self):
		"""Custody refuses the cancel in both On Bounce modes; the message never suggests switching On Bounce
		(a custodian's Payment Entry can only be cancelled)."""
		use_custody_settings(self, allow_overspend=0)
		for on_bounce in ("Cancel Payment Entry", "Reverse by Journal Entry"):
			with self.subTest(on_bounce=on_bounce):
				self.use_company_settings(on_bounce=on_bounce)
				req = self.request()
				cheque = self.delivered(req)
				make_settlement(req.name, 4000)
				frappe.clear_messages()
				with self.assertRaises(frappe.ValidationError):
					cheque.bounce(bounce_date=today(), reason="Bounced")
				self.assertIn("Custody Already Used", self.messages())
				self.assertIn("could not be cancelled (see the message above)", self.messages())
				self.assertNotIn("On Bounce", self.messages())
				self.assert_status(cheque, "Delivered", "Party Posted")
				self.assertEqual(
					frappe.db.get_value("Payment Entry", cheque.party_payment_entry, "docstatus"), 1
				)

	def test_refused_cancel_rolls_back_earlier_reversal(self):
		"""Bounce of a deposited return cheque (On Bounce = Reverse): the Under Collection transfer is reversed
		first, then the party Payment Entry cannot be cancelled. Rolled back to its savepoint (as a web request,
		the batch and the scheduler do), the reversal is gone and the cheque is still Deposited: status and
		vouchers stay in sync."""
		from unittest.mock import patch

		req, cheque = self.returned()
		cheque.deposit(deposit_date=today())
		cheque.reload()
		party_pe = cheque.party_payment_entry
		cancel_voucher = posting.cancel_voucher
		seen = {}

		def refuse(doctype, name, **kwargs):
			if name == party_pe:
				seen["reversals"] = self.vouchers_of(cheque, lifecycle.EVENT_UNDER_COLLECTION + "_reversal")
				frappe.throw("Refused by custody (test)", title="Custody Already Used")
			return cancel_voucher(doctype, name, **kwargs)

		frappe.db.savepoint("treasury_bounce")
		with patch.object(posting, "cancel_voucher", side_effect=refuse):
			with self.assertRaises(frappe.ValidationError):
				cheque.bounce(bounce_date=today(), reason="Bounced")
		frappe.db.rollback(save_point="treasury_bounce")

		self.assertEqual(len(seen["reversals"]), 1, "the reversal was posted before the refused cancel")
		self.assertEqual(self.vouchers_of(cheque, lifecycle.EVENT_UNDER_COLLECTION + "_reversal"), [])
		self.assert_status(cheque, "Deposited", "Under Collection Posted")
		self.assertEqual(frappe.db.get_value("Payment Entry", party_pe, "docstatus"), 1)
		self.assertEqual(
			frappe.db.get_value("Payment Entry", cheque.under_collection_payment_entry, "docstatus"), 1
		)
		self.assertFalse(cheque.reversal_payment_entry)

	def vouchers_of(self, cheque, event: str) -> list[str]:
		"""Submitted Payment Entries / Journal Entries of the cheque with `event`."""
		filters = {
			"treasury_source_doctype": DOCTYPE,
			"treasury_source_name": cheque.name,
			"treasury_event": event,
			"docstatus": 1,
		}
		return frappe.get_all("Payment Entry", filters=filters, pluck="name") + frappe.get_all(
			"Journal Entry", filters=filters, pluck="name"
		)

	def test_represent_rechecks_request(self):
		"""A bounced cheque reserves nothing: once another cheque took the money, it cannot be re-presented."""
		self.use_company_settings(outgoing_party_posting="Manual")
		req = self.request()
		first = self.delivered(req)
		self.assertEqual(first.accounting_status, "Not Posted")
		first.bounce(bounce_date=today(), reason="Bounced")
		self.assert_status(first, "Bounced", "Not Posted")
		second = cheque_from_request(req.name)
		self.assertEqual(flt(second.amount), 10000)
		self.assert_refused(
			lambda: first.represent(represent_date=today()),
			_("At most {0} can still be paid on Custody Request {1}.").format(
				fmt_money(0, currency="EGP"), frappe.bold(req.name)
			),
		)
		self.assert_status(first, "Bounced", "Not Posted")
		self.assertEqual(cint(frappe.db.get_value(DOCTYPE, first.name, "cycle")), 1)

		# without the second cheque the request still accepts it
		second.cancel_cheque(reason="Not needed")
		first.reload()
		first.represent(represent_date=today())
		self.assert_status(first, "Delivered", "Not Posted")

	def test_replace_refused_by_custody(self):
		"""Custody refuses to cancel the party Payment Entry: the old cheque stays Delivered, no replacement."""
		use_custody_settings(self, allow_overspend=0)
		req = self.request()
		cheque = self.delivered(req)
		make_settlement(req.name, 4000)
		with self.assertRaises(frappe.ValidationError):
			cheque.replace(new_cheque_values={"cheque_no": unique_cheque_no(), "reason": "Damaged"})
		self.assert_status(cheque, "Delivered", "Party Posted")
		self.assertFalse(cheque.replaced_by)
		self.assertEqual(frappe.get_all(DOCTYPE, filters={"replaces": cheque.name}), [])
		self.assertEqual(frappe.db.get_value("Payment Entry", cheque.party_payment_entry, "docstatus"), 1)

	def test_represent_after_bounce(self):
		req = self.request()
		cheque = self.delivered(req)
		first = cheque.party_payment_entry
		cheque.bounce(bounce_date=today(), reason="Bounced")
		cheque.reload()
		cheque.represent(represent_date=today())
		cheque.reload()
		self.assertEqual(cheque.cycle, 2)
		self.assert_status(cheque, "Delivered", "Party Posted")
		self.assertNotEqual(cheque.party_payment_entry, first)
		assert_gl_entries(self, "Payment Entry", cheque.party_payment_entry, self.e1(req))
		self.assertEqual(custody_payment_entries(req.name), [cheque.party_payment_entry])
		assert_request(self, req.name, paid_amount=10000, remaining_to_pay=0, status="Paid")

	def test_replace_delivered(self):
		req = self.request()
		cheque = self.delivered(req)
		old_pe = cheque.party_payment_entry
		result = cheque.replace(new_cheque_values={"cheque_no": unique_cheque_no(), "reason": "Damaged"})
		self.assertEqual(frappe.db.get_value("Payment Entry", old_pe, "docstatus"), 2)
		new = frappe.get_doc(DOCTYPE, result["replaced_by"])
		self.assertEqual((new.source_doctype, new.source_name), (REQUEST, req.name))
		self.assertEqual((new.operational_status, new.accounting_status), ("Delivered", "Party Posted"))
		assert_gl_entries(self, "Payment Entry", new.party_payment_entry, self.e1(req))
		self.assertEqual(custody_payment_entries(req.name), [new.party_payment_entry])
		assert_request(self, req.name, paid_amount=10000, remaining_to_pay=0, status="Paid")

	# ------------------------------------------------------------------ what the request still accepts

	def test_partial_cheques_and_available(self):
		req = self.request()
		first = cheque_from_request(req.name, amount=6000)
		self.assertEqual(flt(mapped_cheque(req.name)["amount"]), 4000)
		self.assert_refused(
			lambda: build_cheque(req, amount=4000.01).insert(),
			_("At most {0} can still be paid on Custody Request {1}.").format(
				fmt_money(4000, currency="EGP"), frappe.bold(req.name)
			),
		)
		second = cheque_from_request(req.name)
		self.assertEqual(flt(second.amount), 4000)
		for cheque in (first, second):
			cheque.deliver(handover_date=today())
		assert_request(self, req.name, paid_amount=10000, remaining_to_pay=0, status="Paid")
		self.assertEqual(len(custody_payment_entries(req.name)), 2)

	# ------------------------------------------------------------------ return cheque (E4, E5, E6)

	def test_receive_cheque_return(self):
		egp = self.data.egp
		req = self.request()
		self.delivered(req)
		make_settlement(req.name, 7000)
		data = mapped_cheque(req.name, "Incoming")
		self.assertEqual((flt(data["amount"]), data["direction"]), (3000, "Incoming"))

		cheque = cheque_from_request(req.name, "Incoming")
		cheque.receive(receipt_date=today())
		self.assert_status(cheque, "Received", "Party Posted")
		pe = get_pe(cheque.party_payment_entry)
		self.assertEqual((pe.payment_type, pe.custody_request, pe.custody_purpose), ("Receive", req.name, "Return"))
		assert_gl_entries(
			self,
			"Payment Entry",
			pe.name,
			[(egp.pdc_receivable, 3000, 0, None), (self.cus, 0, 3000, req.custodian)],
		)
		assert_request(self, req.name, returned_amount=3000, balance_amount=0, status="Closed", is_closed=1)

		cheque.deposit(deposit_date=today())
		cheque.reload()
		assert_gl_entries(
			self,
			"Payment Entry",
			cheque.under_collection_payment_entry,
			[(egp.under_collection, 3000, 0, None), (egp.pdc_receivable, 0, 3000, None)],
		)
		cheque.clear(clearance_date=today())
		cheque.reload()
		assert_gl_entries(
			self,
			"Payment Entry",
			cheque.bank_payment_entry,
			[(egp.bank_gl, 3000, 0, None), (egp.under_collection, 0, 3000, None)],
		)
		assert_request(self, req.name, balance_amount=0, status="Closed")

	def test_bounce_return_cheque(self):
		egp = self.data.egp
		req, cheque = self.returned()
		party_pe = cheque.party_payment_entry
		cheque.deposit(deposit_date=today())
		cheque.reload()
		cheque.bounce(bounce_date=today(), reason="Bounced")
		self.assert_status(cheque, "Bounced", "Reversed")
		# the Under Collection transfer is reversed (On Bounce = Reverse), the party Payment Entry cancelled
		self.assertTrue(cheque.reversal_payment_entry)
		assert_gl_entries(
			self,
			"Payment Entry",
			cheque.reversal_payment_entry,
			[(egp.pdc_receivable, 3000, 0, None, today()), (egp.under_collection, 0, 3000, None, today())],
		)
		self.assertEqual(frappe.db.get_value("Payment Entry", party_pe, "docstatus"), 2)
		self.assertFalse(cheque.reversal_journal)
		assert_request(self, req.name, balance_amount=3000, status="Partly Settled", is_closed=0)

	def test_bounce_return_cheque_cancel_mode(self):
		"""On Bounce = Cancel: the Under Collection transfer and the party Payment Entry are both cancelled."""
		self.use_company_settings(on_bounce="Cancel Payment Entry")
		req, cheque = self.returned()
		party_pe = cheque.party_payment_entry
		cheque.deposit(deposit_date=today())
		cheque.reload()
		under_collection = cheque.under_collection_payment_entry
		cheque.bounce(bounce_date=today(), reason="Bounced")
		self.assert_status(cheque, "Bounced", "Reversed")
		for name in (under_collection, party_pe):
			self.assertEqual(frappe.db.get_value("Payment Entry", name, "docstatus"), 2, name)
		self.assertFalse(cheque.reversal_payment_entry or cheque.reversal_journal)
		self.assertEqual(self.vouchers_of(cheque, lifecycle.EVENT_UNDER_COLLECTION + "_reversal"), [])
		cancelled = [r.voucher_no for r in cheque.status_log if r.event == "Voucher Cancelled"]
		self.assertEqual(cancelled, [under_collection, party_pe])
		assert_request(self, req.name, balance_amount=3000, status="Partly Settled", is_closed=0)

	def test_bounce_return_cheque_je_settlement(self):
		"""Bank Settlement Voucher = Journal Entry, On Bounce = Reverse: the Under Collection Journal Entry is
		reversed by a Journal Entry, then the custodian's party Payment Entry is cancelled."""
		egp = self.data.egp
		self.use_company_settings(bank_settlement_voucher="Journal Entry")
		req, cheque = self.returned()
		party_pe = cheque.party_payment_entry
		cheque.deposit(deposit_date=today())
		cheque.reload()
		self.assertTrue(cheque.under_collection_journal)
		cheque.bounce(bounce_date=today(), reason="Bounced")
		self.assert_status(cheque, "Bounced", "Reversed")
		reversals = self.vouchers_of(cheque, lifecycle.EVENT_UNDER_COLLECTION + "_reversal")
		self.assertEqual(len(reversals), 1)
		assert_gl_entries(
			self,
			"Journal Entry",
			reversals[0],
			[(egp.pdc_receivable, 3000, 0, None, today()), (egp.under_collection, 0, 3000, None, today())],
		)
		self.assertEqual(frappe.db.get_value("Journal Entry", cheque.under_collection_journal, "docstatus"), 1)
		self.assertEqual(frappe.db.get_value("Payment Entry", party_pe, "docstatus"), 2)
		self.assertFalse(cheque.reversal_journal)
		events = [(r.event, r.voucher_no) for r in cheque.status_log]
		self.assertIn(("Voucher Reversed", reversals[0]), events)
		self.assertIn(("Voucher Cancelled", party_pe), events)
		assert_request(self, req.name, balance_amount=3000, status="Partly Settled", is_closed=0)

	# ------------------------------------------------------------------ permanent custody

	def test_permanent_funding_and_replenishment(self):
		use_custody_settings(
			self,
			enable_permanent_custody=1,
			replenishment_mode="Manual",
			replenishment_threshold_percent=25,
		)
		req = self.request(5000, custody_type="Permanent", float_amount=5000)
		self.assertEqual(flt(mapped_cheque(req.name)["amount"]), 5000)
		funding = self.delivered(req)
		pe = get_pe(funding.party_payment_entry)
		self.assertEqual(pe.custody_purpose, "Initial Funding")
		assert_gl_entries(self, "Payment Entry", pe.name, self.e1(req, 5000))

		make_settlement(req.name, 3900)
		assert_request(self, req.name, replenishment_amount=3900, status="Replenishment Due")
		self.assertEqual(flt(mapped_cheque(req.name)["amount"]), 3900)
		replenishment = self.delivered(req)
		pe = get_pe(replenishment.party_payment_entry)
		self.assertEqual(pe.custody_purpose, "Replenishment")
		assert_gl_entries(self, "Payment Entry", pe.name, self.e1(req, 3900))
		assert_request(self, req.name, balance_amount=5000, status="Active")

	def test_draft_replenishment_blocks(self):
		use_custody_settings(
			self,
			enable_permanent_custody=1,
			replenishment_mode="Auto Draft on Settlement Approval",
			replenishment_threshold_percent=25,
		)
		req = self.request(5000, custody_type="Permanent", float_amount=5000)
		self.delivered(req)
		make_settlement(req.name, 3900)
		drafts = compat.draft_replenishments(req.name)
		self.assertEqual(len(drafts), 1)
		message = _(
			"Payment Entry {0} is a draft replenishment of {1}. Submit or delete it first, then pay by cheque or transfer."
		).format(frappe.bold(drafts[0]), frappe.bold(req.name))
		self.assert_refused(lambda: api.make_pdc_cheque(req.name, "Outgoing"), message)
		self.assert_refused(lambda: build_cheque(req, amount=3900).insert(), message)
		context = api.get_payment_context(req.name)
		self.assertEqual(context["pay"]["amount"], 0)
		self.assertTrue(context["pay"]["blocked"])

	# ------------------------------------------------------------------ refusals

	def test_validation_errors(self):
		req = self.request()
		paid_to = _("{0} must be paid to custodian {1} of company {2}.").format(
			_(DOCTYPE), frappe.bold(req.custodian_name), frappe.bold(COMPANY)
		)
		_employee, other = make_custodian()
		self.assert_refused(lambda: build_cheque(req, amount=100, party=other).insert(), paid_to)

		other_company = build_cheque(req, amount=100)
		other_company.company = "_Test Treasury Co 2"
		self.assert_refused(lambda: documents.validate(other_company), paid_to)

		with_references = build_cheque(req, amount=100)
		with_references.append(
			"references",
			{"reference_doctype": "Purchase Invoice", "reference_name": "PI-X", "allocated_amount": 100},
		)
		self.assert_refused(
			lambda: documents.validate(with_references), _("A payment to a custodian cannot have references.")
		)

		self.assert_refused(
			lambda: build_cheque(req, amount=100, bank_account=bank_account_name("USD")).insert(),
			_("Custody is paid in the company currency only: choose a Bank Account in {0}.").format("EGP"),
		)

		without_source = _("A cheque or transfer to a custodian must be created from its Custody Request.")
		self.assert_refused(
			lambda: build_cheque(req, amount=100, source_doctype=None, source_name=None).insert(), without_source
		)
		batch = frappe.get_doc(
			{
				"doctype": "Cheque Batch",
				"direction": "Outgoing",
				"company": COMPANY,
				"party_type": "Custodian",
				"party": req.custodian,
				"bank_account": bank_account_name("EGP"),
				"issue_date": today(),
				"purpose": "Rent",
				"cheques": [{"due_date": today(), "amount": 100, "cheque_no": unique_cheque_no()}],
			}
		)
		self.assert_refused(batch.insert, without_source)

		self.assert_refused(
			lambda: build_cheque(req, amount=10000.01).insert(),
			_("At most {0} can still be paid on Custody Request {1}.").format(
				fmt_money(10000, currency="EGP"), frappe.bold(req.name)
			),
		)

		# a closed request (paid and fully settled)
		self.delivered(req)
		make_settlement(req.name, 10000)
		assert_request(self, req.name, status="Closed", is_closed=1)
		self.assert_refused(
			lambda: build_cheque(req, amount=1).insert(),
			_("Custody Request {0} is closed.").format(frappe.bold(req.name)),
		)

	def test_bounce_charges_refused_for_custodian(self):
		req, cheque = self.returned()
		self.assert_refused(
			lambda: cheque.bounce(bounce_date=today(), reason="Bounced", charges=50),
			_("Bounce charges cannot be charged to {0} {1}.").format(_("Custodian"), frappe.bold(req.custodian)),
		)
		self.assert_status(cheque, "Received", "Party Posted")

	def test_idempotency(self):
		req = self.request()
		cheque = self.delivered(req)
		again = posting.create_payment_entry(
			source_doctype=DOCTYPE,
			source_name=cheque.name,
			event=lifecycle.EVENT_PARTY,
			payment_type="Pay",
			company=COMPANY,
			posting_date=today(),
			party_type="Custodian",
			party=req.custodian,
			amount=10000,
			bank_account_head=self.data.egp.pdc_payable,
			reference_no=cheque.cheque_no,
			reference_date=today(),
			cycle=1,
		)
		self.assertEqual(again, cheque.party_payment_entry)
		self.assert_refused(lambda: cheque.deliver(handover_date=today()), "already")
		self.assertEqual(custody_payment_entries(req.name), [cheque.party_payment_entry])
		assert_request(self, req.name, paid_amount=10000)

	# ------------------------------------------------------------------ permissions

	def test_permissions(self):
		req = self.request()
		treasury_only = ensure_user(TREASURY_ONLY, ["Treasury User"])
		with as_user(treasury_only):
			self.assertRaises(frappe.PermissionError, api.make_pdc_cheque, req.name, "Outgoing")
			self.assertRaises(frappe.PermissionError, api.get_payment_context, req.name)
			# a REST insert of a cheque of a request the user cannot read
			self.assertRaises(frappe.PermissionError, build_cheque(req, amount=100).insert)

		custody_user = ensure_user(f"trs-custody-{frappe.generate_hash(length=6)}@treasury.test", [])
		_employee, custodian = make_custodian(user=custody_user)
		own = make_request(custodian, 1000, cost_center=self.cost_center)
		with as_user(custody_user):
			self.assertRaises(frappe.PermissionError, api.make_pdc_cheque, own.name, "Outgoing")

		with as_user(ACCOUNTANT):
			data = api.make_pdc_cheque(req.name, "Outgoing")
			self.assertEqual(flt(data["amount"]), 10000)
			self.assertTrue(api.get_payment_context(req.name)["can_cheque"])

	def test_context(self):
		req = self.request()
		cheque_from_request(req.name, amount=4000)
		context = api.get_payment_context(req.name)
		self.assertEqual((context["currency"], context["default_method"]), ("EGP", "Cheque"))
		self.assertTrue(context["can_cheque"])
		self.assertEqual((context["pay"]["amount"], context["pay"]["purpose"]), (6000, "Disbursement"))
		self.assertIsNone(context["pay"]["reason"])
		self.assertEqual(context["receive"]["amount"], 0)
		self.assertIn("Nothing is left to return", context["receive"]["reason"])
		self.assertFalse(context["receive"]["blocked"])

		cheque_from_request(req.name)
		context = api.get_payment_context(req.name)
		self.assertEqual(context["pay"]["amount"], 0)
		self.assertIn("Open cheques or transfers already cover", context["pay"]["reason"])
		self.assertTrue(context["pay"]["blocked"])

	# ------------------------------------------------------------------ phase 9 QA gaps

	def test_return_unspent_request(self):
		"""QA gap 1: nothing settled, the whole payout comes back by cheque (Return)."""
		egp = self.data.egp
		req = self.request()
		self.delivered(req)
		data = mapped_cheque(req.name, "Incoming")
		self.assertEqual((flt(data["amount"]), data["direction"]), (10000, "Incoming"))

		cheque = cheque_from_request(req.name, "Incoming")
		cheque.receive(receipt_date=today())
		self.assert_status(cheque, "Received", "Party Posted")
		pe = get_pe(cheque.party_payment_entry)
		self.assertEqual((pe.payment_type, pe.custody_request, pe.custody_purpose), ("Receive", req.name, "Return"))
		assert_gl_entries(
			self,
			"Payment Entry",
			pe.name,
			[(egp.pdc_receivable, 10000, 0, None, today()), (self.cus, 0, 10000, req.custodian, today())],
		)
		assert_request(
			self, req.name, returned_amount=10000, balance_amount=0, status="Closed", is_closed=1
		)

	def test_partial_return(self):
		"""QA gap 2: 3,000 left after the settlement, 1,000 of it returned by cheque: 2,000 can still be."""
		req = self.request()
		self.delivered(req)
		make_settlement(req.name, 7000)
		cheque = cheque_from_request(req.name, "Incoming", amount=1000)
		cheque.receive(receipt_date=today())
		self.assert_status(cheque, "Received", "Party Posted")
		assert_request(self, req.name, returned_amount=1000, balance_amount=2000, is_closed=0)

		self.assertEqual(flt(mapped_cheque(req.name, "Incoming")["amount"]), 2000)
		self.assert_refused(
			lambda: build_cheque(req, "Incoming", amount=2000.01).insert(),
			_("At most {0} can still be returned on Custody Request {1}.").format(
				fmt_money(2000, currency="EGP"), frappe.bold(req.name)
			),
		)
		self.assertEqual(
			frappe.get_all(DOCTYPE, filters={"source_name": req.name, "direction": "Incoming"}, pluck="name"),
			[cheque.name],
		)

	def test_reimbursement_purpose(self):
		"""QA gap 3: overspent (12,000 settled on 10,000 paid): the Outgoing cheque reimburses 2,000, a
		wind-down for custody (the custodian is checked as for a return)."""
		from unittest.mock import patch

		use_custody_settings(self, allow_overspend=1)
		req = self.request()
		self.delivered(req)
		make_settlement(req.name, 12000)
		assert_request(self, req.name, balance_amount=-2000)

		usable = compat.assert_custodian_usable
		with patch.object(documents.compat, "assert_custodian_usable", wraps=usable) as check:
			data = mapped_cheque(req.name)
			self.assertEqual(flt(data["amount"]), 2000)
			check.assert_called_with(req.custodian, winddown=True)
			self.assertEqual({c.kwargs["winddown"] for c in check.call_args_list}, {True})
		context = api.get_payment_context(req.name)
		self.assertEqual((context["pay"]["amount"], context["pay"]["purpose"]), (2000, "Reimbursement"))

		cheque = self.delivered(req)
		self.assertEqual(flt(cheque.amount), 2000)
		pe = get_pe(cheque.party_payment_entry)
		self.assertEqual(
			(pe.payment_type, pe.custody_request, pe.custody_purpose), ("Pay", req.name, "Reimbursement")
		)
		assert_gl_entries(self, "Payment Entry", pe.name, self.e1(req, 2000))
		assert_request(self, req.name, balance_amount=0, status="Closed", is_closed=1)

	def test_closing_return_purpose(self):
		"""QA gap 3: a float being closed returns its balance as a Closing Return."""
		from custody.custody.permanent import request_close

		use_custody_settings(
			self, enable_permanent_custody=1, replenishment_mode="Manual", replenishment_threshold_percent=25
		)
		req = self.request(5000, custody_type="Permanent", float_amount=5000)
		self.delivered(req)
		make_settlement(req.name, 3900)
		request_close(req.name, "Treasury test closing")
		assert_request(self, req.name, closing_requested=1, balance_amount=1100, is_closed=0)

		context = api.get_payment_context(req.name)
		self.assertEqual((context["receive"]["amount"], context["receive"]["purpose"]), (1100, "Closing Return"))
		cheque = cheque_from_request(req.name, "Incoming")
		self.assertEqual(flt(cheque.amount), 1100)
		cheque.receive(receipt_date=today())
		pe = get_pe(cheque.party_payment_entry)
		self.assertEqual((pe.payment_type, pe.custody_purpose), ("Receive", "Closing Return"))
		assert_gl_entries(
			self,
			"Payment Entry",
			pe.name,
			[(self.data.egp.pdc_receivable, 1100, 0, None), (self.cus, 0, 1100, req.custodian)],
		)
		assert_request(self, req.name, balance_amount=0, is_closed=1)

	def test_context_states(self):
		"""QA gap 13: a draft request, a closed request, and the open documents (3 newest, the rest counted)."""
		_employee, custodian = make_custodian()
		draft = frappe.get_doc(
			{
				"doctype": REQUEST,
				"custody_type": "Temporary",
				"custodian": custodian,
				"company": COMPANY,
				"posting_date": today(),
				"purpose": "Treasury test custody",
				"requested_amount": 1000,
			}
		).insert(ignore_permissions=True)
		not_approved = _("Custody Request {0} is not approved.").format(frappe.bold(draft.name))
		context = api.get_payment_context(draft.name)
		for side in ("pay", "receive"):
			self.assertEqual((context[side]["amount"], context[side]["reason"]), (0, not_approved), side)
		self.assertEqual((context["open_documents"], context["more_documents"]), ([], 0))

		closed = self.request()
		self.delivered(closed)
		make_settlement(closed.name, 10000)
		assert_request(self, closed.name, is_closed=1)
		is_closed = _("Custody Request {0} is closed.").format(frappe.bold(closed.name))
		context = api.get_payment_context(closed.name)
		for side in ("pay", "receive"):
			self.assertEqual((context[side]["amount"], context[side]["reason"]), (0, is_closed), side)

		req = self.request()
		cheques = [cheque_from_request(req.name, amount=2500) for _i in range(4)]
		cheques[2].deliver(handover_date=today())
		cheques[2].reload()
		self.assertTrue(cheques[2].party_payment_entry)
		context = api.get_payment_context(req.name)
		self.assertEqual(context["more_documents"], 1)
		rows = context["open_documents"]
		self.assertEqual(
			[(row["doctype"], row["name"], row["status"], row["payment_entry"]) for row in rows],
			[
				(DOCTYPE, cheques[3].name, "Created", None),
				(DOCTYPE, cheques[2].name, "Delivered", cheques[2].party_payment_entry),
				(DOCTYPE, cheques[1].name, "Created", None),
			],
		)
		self.assertEqual([flt(row["amount"]) for row in rows], [2500, 2500, 2500])

	def test_replace_created(self):
		"""QA gap 17: a Created (not posted) cheque replaced: the new one keeps the request, reserves its own
		amount only (the old one is Replaced, no longer reserved), nothing is posted."""
		req = self.request()
		cheque = cheque_from_request(req.name, amount=4000)
		self.assertEqual(documents.reserved(req.name, "Outgoing"), 4000)
		result = cheque.replace(
			new_cheque_values={"cheque_no": unique_cheque_no(), "amount": 6000, "reason": "Damaged"}
		)
		self.assert_status(cheque, "Replaced", "Not Posted")
		new = frappe.get_doc(DOCTYPE, result["replaced_by"])
		self.assertEqual((new.source_doctype, new.source_name, new.replaces), (REQUEST, req.name, cheque.name))
		self.assertEqual((new.operational_status, new.accounting_status), ("Created", "Not Posted"))
		self.assertEqual(flt(new.amount), 6000)
		self.assertEqual(documents.reserved(req.name, "Outgoing"), 6000)
		self.assertEqual(custody_payment_entries(req.name), [])
		assert_request(self, req.name, paid_amount=0, remaining_to_pay=10000)
		self.assertEqual(api.get_payment_context(req.name)["pay"]["amount"], 4000)
