"""Phase 7a-2: the Payment Entry as the paper-filing key — the server-rendered voucher summary of the Treasury
documents (spec 7a §3.8), the "Payment Entry" report column (§3.9) and the Payment Entry number on the receipts
(§3.10); spec §7 F."""

import re

import frappe
from frappe.desk.form.load import getdoc
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, formatdate, getdate

from treasury.bank_facilities.report.discounted_cheques.discounted_cheques import (
	execute as discounted_report,
)
from treasury.pdc.report.bounced_and_postponed_cheques.bounced_and_postponed_cheques import (
	execute as bounced_report,
)
from treasury.pdc.report.cheque_book_usage.cheque_book_usage import get_book_payment_entries
from treasury.pdc.report.pdc_maturity.pdc_maturity import execute as maturity_report
from treasury.pdc.report.pdc_register.pdc_register import execute as register_report
from treasury.security_cheques.report.security_cheque_register.security_cheque_register import (
	execute as security_register,
)
from treasury.tests.utils import (
	COMPANY,
	PE_SETTINGS,
	SUPPLIER,
	TreasuryTestMixin,
	add_user_permission,
	bank_account_name,
	day,
	ensure_instrument_accounts,
	ensure_user,
	make_bank_charge,
	make_bank_facility,
	make_bank_transfer,
	make_cheque_book,
	make_cheque_discounting,
	make_own_bank,
	make_pdc_cheque,
	make_sales_invoice,
	make_security_cheque,
	set_bank_suppliers,
	unique_cheque_no,
	unique_leaf_start,
)
from treasury.utils import voucher_summary
from treasury.utils.print_helpers import treasury_payment_entries

CHEQUE = "PDC Cheque"
NO_ACCESS_USER = "treasury-vouchers-noaccess@example.com"
ONE_CHEQUE_USER = "treasury-vouchers-onecheque@example.com"


class TestVoucherVisibility(TreasuryTestMixin, IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_user(NO_ACCESS_USER, [])  # no role: no read permission on PDC Cheque
		# a Treasury role, restricted to single cheques by User Permissions (see `allow_only`)
		ensure_user(ONE_CHEQUE_USER, ["Treasury User", "Accounts User"])

	def allow_only(self, cheque: str):
		"""Restrict ONE_CHEQUE_USER to `cheque` (a User Permission on PDC Cheque)."""
		add_user_permission(ONE_CHEQUE_USER, CHEQUE, cheque)

	def setUp(self):
		super().setUp()
		self.use_company_settings(accrue_facility_interest=0, **PE_SETTINGS)
		previous = frappe.local.lang
		self.addCleanup(setattr, frappe.local, "lang", previous)
		frappe.local.lang = "en"

	# ------------------------------------------------------------------ helpers

	def outgoing_cleared(self):
		"""Outgoing cheque: party Payment Entry on handover, bank Payment Entry at clearance."""
		cheque = make_pdc_cheque(amount=10000, issue_date=day(-60), due_date=day(-30))
		cheque.deliver(handover_date=day(-59))
		cheque.clear(clearance_date=day(-28))
		cheque.reload()
		return cheque

	def incoming_bounced(self, **values):
		"""Incoming cheque received, deposited and bounced (Reverse mode): the party Payment Entry is reversed by
		a Journal Entry, the Under Collection Payment Entry by a reverse Payment Entry."""
		si = make_sales_invoice(20000, posting_date=day(-50))
		cheque = make_pdc_cheque(
			"Incoming", amount=20000, issue_date=day(-50), due_date=day(-20), invoice=si, **values
		)
		cheque.receive(receipt_date=day(-49))
		cheque.deposit(deposit_date=day(-20))
		cheque.bounce(bounce_date=day(-15), reason="Insufficient funds")
		cheque.reload()
		return cheque

	def load_summary(self, doc) -> str:
		doc.run_method("onload")
		return doc.get_onload().voucher_summary

	# ------------------------------------------------------------------ voucher summary (F1)

	def test_pdc_cheque_summary_lists_every_voucher(self):
		cheque = self.incoming_bounced(remarks="<b>Handle with care</b>")
		first = frappe._dict(
			party=cheque.party_payment_entry,
			uc=cheque.under_collection_payment_entry,
			reverse=cheque.reversal_payment_entry,
			journal=cheque.reversal_journal,
		)
		self.assertTrue(all(first.values()))
		cheque.represent(represent_date=day(-10))
		cheque.deposit(deposit_date=day(-8))
		cheque.clear(clearance_date=day(-5))
		cheque.reload()
		second = frappe._dict(
			party=cheque.party_payment_entry,
			uc=cheque.under_collection_payment_entry,
			bank=cheque.bank_payment_entry,
		)
		self.assertTrue(all(second.values()))

		html = self.load_summary(cheque)
		for name in (*first.values(), *second.values()):
			self.assertIn(name, html)
		# Payment Entries first (paper-filing key), then the Journal Entries
		self.assertLess(html.index(first.party), html.index(first.journal))
		self.assertLess(html.index(second.bank), html.index(first.journal))
		for label in ("Party Posting", "Under Collection", "Bank Settlement", "Reversal of Under Collection"):
			self.assertIn(label, html)
		self.assertIn("Reversal of Party Posting", html)
		self.assertIn("Internal Transfer", html)
		# dates, the cycle of each row (the cheque has two) and the earlier cycle dimmed
		for date in (day(-49), day(-20), day(-15), day(-8), day(-5)):
			self.assertIn(formatdate(date), html)
		self.assertIn("Cycle 1", html)
		self.assertIn("Cycle 2", html)
		self.assertIn('<tr class="text-muted">', html)
		self.assertIn(f"/app/payment-entry/{second.bank}", html)
		self.assertIn(f"/app/journal-entry/{first.journal}", html)
		# user data never reaches the page unescaped
		self.assertNotIn("<b>Handle", html)

		# the headline data: the submitted Payment Entries of the current cycle only
		entries = cheque.get_onload().payment_entries
		self.assertEqual([e["name"] for e in entries], [second.party, second.uc, second.bank])
		self.assertEqual(
			[e["event_label"] for e in entries], ["Party Posting", "Under Collection", "Bank Settlement"]
		)
		self.assertEqual(getdate(entries[2]["clearance_date"]), day(-5))
		self.assertIsNone(entries[0]["clearance_date"])

	def test_summary_escapes_every_value(self):
		row = {
			"doctype": "Payment Entry",
			"name": "<script>alert(1)</script>",
			"route": '/app/payment-entry/"><img src=x onerror=alert(1)>',
			"kind": "<i>kind</i>",
			"event": "<u>event</u>",
			"cycle": "",
			"posting_date": "<b>date</b>",
			"mode_of_payment": "<b>Cheque</b>",
			"amount": "<b>1</b>",
			"clearance_date": "",
			"status": "Submitted",
			"status_colour": "blue",
			"muted": False,
			"cancelled": False,
		}
		html = frappe.render_template(voucher_summary.TEMPLATE, {"rows": [row]})
		for raw in ("<script>", "<img", "<i>kind", "<u>event", "<b>date", "<b>Cheque", "<b>1"):
			self.assertNotIn(raw, html)
		self.assertIn("&lt;script&gt;", html)
		self.assertIn("&lt;b&gt;Cheque&lt;/b&gt;", html)

	def test_empty_summary(self):
		cheque = make_pdc_cheque(amount=500, issue_date=day(-60), due_date=day(-30))
		self.assertIn("No Payment Entries or vouchers yet.", self.load_summary(cheque))
		cheque.run_method("onload")
		self.assertEqual(cheque.get_onload().payment_entries, [])

	def test_bank_transfer_summary(self):
		"""Phase 7b: the Payment Entry of a transfer and its reversal, with the Transfer labels."""
		transfer = make_bank_transfer(amount=900, execution_date=add_days(getdate(), -2))
		transfer.mark_returned(return_date=add_days(getdate(), -1), reason="Returned")
		transfer.reload()
		html = self.load_summary(transfer)
		self.assertIn(transfer.payment_entry, html)
		self.assertIn(transfer.reversal_journal, html)
		self.assertIn("Transfer", html)
		self.assertIn("Reversal of Transfer", html)
		self.assertEqual(
			[pe["name"] for pe in transfer.get_onload().payment_entries], [transfer.payment_entry]
		)
		self.assertEqual([pe["name"] for pe in treasury_payment_entries(transfer)], [transfer.payment_entry])
		with self.set_user(NO_ACCESS_USER):
			self.assertEqual(treasury_payment_entries(transfer), [])

	def test_bank_charge_summary(self):
		"""Phase 7d: a charge lists its Payment Entry with the Bank Charge label; a refund shows its own."""
		self.use_company_settings(bank_charge_voucher="Payment Entry", bank_charge_clearance="On Posting")
		set_bank_suppliers(self, [])
		charge = make_bank_charge()
		refund = make_bank_charge(entry_type="Refund", against_bank_charge=charge.name, amount=40)
		html = self.load_summary(charge)
		self.assertIn(charge.payment_entry, html)
		self.assertIn("Bank Charge", html)
		self.assertNotIn(refund.payment_entry, html)
		html = self.load_summary(refund)
		self.assertIn(refund.payment_entry, html)
		self.assertNotIn(charge.payment_entry, html)
		self.assertEqual(voucher_summary.event_label("bank_charge"), "Bank Charge")

	def test_event_labels(self):
		self.assertEqual(voucher_summary.event_label("bt_transfer"), "Transfer")
		self.assertEqual(voucher_summary.event_label("bt_transfer_reversal"), "Reversal of Transfer")
		self.assertEqual(voucher_summary.event_label("pdc_bank_posting"), "Bank Settlement")
		self.assertEqual(
			voucher_summary.event_label("pdc_under_collection_reversal"), "Reversal of Under Collection"
		)
		self.assertEqual(voucher_summary.event_label("interest_accrual:2025-01"), "Interest Accrual 2025-01")
		self.assertEqual(voucher_summary.event_label("lc_settlement"), "Settlement")
		self.assertEqual(voucher_summary.event_label("something_else"), "something_else")

	def test_cancelled_voucher_is_struck_through(self):
		self.use_company_settings(on_bounce="Cancel Payment Entry")
		cheque = self.incoming_bounced()
		rows = voucher_summary.summary_rows(CHEQUE, [cheque.name], cycle=cheque.cycle)
		self.assertTrue(rows)
		self.assertTrue(all(r["cancelled"] and r["status"] == "Cancelled" for r in rows))
		self.assertTrue(all(r["status_colour"] == "red" for r in rows))
		self.assertIn("line-through", self.load_summary(cheque))
		cheque.run_method("onload")
		self.assertEqual(cheque.get_onload().payment_entries, [])

	def test_security_cheque_shows_the_vouchers_of_its_pdc_cheque(self):
		security = make_security_cheque(amount=5000, issue_date=day(-90))
		security.receive(receipt_date=day(-80))
		self.assertIn("No Payment Entries or vouchers yet.", self.load_summary(security))

		security.encash(encashment_date=day(-5), bank_account=bank_account_name("EGP"))
		security.reload()
		pdc = frappe.get_doc(CHEQUE, security.converted_pdc_cheque)
		self.assertTrue(pdc.party_payment_entry)
		html = self.load_summary(security)
		self.assertIn(frappe._("Vouchers of the PDC Cheque {0}").split("{0}")[0].strip(), html)
		self.assertIn(pdc.name, html)
		self.assertIn(pdc.party_payment_entry, html)

	def test_security_cheque_summary_hides_an_unreadable_pdc_cheque(self):
		security = make_security_cheque(amount=5000, issue_date=day(-90))
		security.receive(receipt_date=day(-80))
		security.encash(encashment_date=day(-5), bank_account=bank_account_name("EGP"))
		security.reload()
		pdc = frappe.get_doc(CHEQUE, security.converted_pdc_cheque)
		self.allow_only(make_pdc_cheque(amount=100, issue_date=day(-10), due_date=day(10)).name)

		with self.set_user(ONE_CHEQUE_USER):
			self.assertFalse(frappe.has_permission(CHEQUE, "read", doc=pdc.name))
			html = self.load_summary(security)
		self.assertNotIn(pdc.name, html)
		self.assertNotIn(pdc.party_payment_entry, html)

	def test_form_load_requires_read_permission(self):
		cheque = self.outgoing_cleared()
		getdoc(CHEQUE, cheque.name)
		loaded = frappe.response.docs[-1]
		self.assertIn(cheque.bank_payment_entry, loaded.get("__onload").voucher_summary)

		frappe.set_user(NO_ACCESS_USER)
		self.addCleanup(frappe.set_user, "Administrator")
		self.assertRaises(frappe.PermissionError, getdoc, CHEQUE, cheque.name)

	# ------------------------------------------------------------------ reports (F2)

	def test_pdc_register_payment_entry_column_and_filter(self):
		cheque = self.outgoing_cleared()
		expected = f"{cheque.party_payment_entry}, {cheque.bank_payment_entry}"
		columns, data = register_report({"company": COMPANY, "payment_entry": cheque.bank_payment_entry})
		self.assertIn("payment_entry", [c["fieldname"] for c in columns])
		rows = [r for r in data if r.get("cheque")]
		self.assertEqual([r.cheque for r in rows], [cheque.name])
		self.assertEqual(rows[0].payment_entry, expected)

		# the party Payment Entry finds the same cheque; an unrelated number finds none
		_columns, data = register_report({"company": COMPANY, "payment_entry": cheque.party_payment_entry})
		self.assertEqual([r.cheque for r in data if r.get("cheque")], [cheque.name])
		_columns, data = register_report({"company": COMPANY, "payment_entry": "ACC-PAY-NOT-A-CHEQUE"})
		self.assertEqual([r for r in data if r.get("cheque")], [])

	def test_pdc_maturity_payment_entry_column(self):
		cheque = make_pdc_cheque(amount=3000, issue_date=day(-60), due_date=day(20))
		cheque.deliver(handover_date=day(-59))
		cheque.reload()
		self.assertTrue(cheque.party_payment_entry)
		_columns, data, *_rest = maturity_report(
			{"company": COMPANY, "direction": "Outgoing", "as_on": day(0), "group_by": "Party"}
		)
		row = next(r for r in data if r.get("group_key") == f"Supplier::{SUPPLIER}")
		self.assertIn(cheque.party_payment_entry, row.payment_entry.split(", "))
		total = next(r for r in data if r.get("is_total"))
		self.assertEqual(total.payment_entry, "")

	def test_cheque_book_usage_lists_readable_cheques_only(self):
		# a leaf range of this test only
		start = unique_leaf_start(10, digits=6)
		book = make_cheque_book(start, start + 9)
		allowed, hidden = (
			make_pdc_cheque(
				amount=100 * (i + 1), cheque_book=book.name, issue_date=day(-20), due_date=day(10)
			)
			for i in range(2)
		)
		for cheque in (allowed, hidden):
			cheque.deliver(handover_date=day(-19))
			cheque.reload()
		self.assertEqual(
			get_book_payment_entries([book.name]),
			{book.name: f"{allowed.party_payment_entry}, {hidden.party_payment_entry}"},
		)

		self.allow_only(allowed.name)
		with self.set_user(ONE_CHEQUE_USER):
			self.assertEqual(get_book_payment_entries([book.name]), {book.name: allowed.party_payment_entry})
		with self.set_user(NO_ACCESS_USER):
			self.assertEqual(get_book_payment_entries([book.name]), {})

	def test_bounced_report_payment_entry_column(self):
		cheque = self.incoming_bounced()
		_columns, data = bounced_report(
			{"company": COMPANY, "view": "Bounced", "from_date": day(-16), "to_date": day(-14)}
		)
		row = next(r for r in data if r.cheque == cheque.name)
		# the party Payment Entry (reversed by a Journal Entry), the Under Collection one and its reverse
		expected = [
			cheque.party_payment_entry,
			cheque.under_collection_payment_entry,
			cheque.reversal_payment_entry,
		]
		self.assertEqual(row.payment_entry, ", ".join(expected))

	def test_discounted_cheques_payment_entry_column(self):
		inst = ensure_instrument_accounts()
		bank = make_own_bank()
		facility = make_bank_facility(
			limits=[
				{
					"limit_type": "Cheque Discounting",
					"limit_amount": 200_000,
					"interest_rate": 12,
					"commission_rate": 0.5,
					"liability_account": inst.discounted,
				}
			],
			bank_account=bank.bank_account,
		)
		cheque = make_pdc_cheque("Incoming", amount=50_000, issue_date="2025-03-01", due_date="2025-06-30")
		cheque.receive(receipt_date="2025-03-01")
		cheque.reload()
		batch = make_cheque_discounting(facility, [cheque])
		_columns, data, *_rest = discounted_report({"company": COMPANY, "cheque_discounting": batch.name})
		row = next(r for r in data if r.get("pdc_cheque") == cheque.name)
		self.assertEqual(row.payment_entry, cheque.party_payment_entry)

	def test_security_cheque_register_payment_entry_column(self):
		security = make_security_cheque(amount=5000, issue_date=day(-90))
		security.receive(receipt_date=day(-80))
		security.encash(encashment_date=day(-5), bank_account=bank_account_name("EGP"))
		security.reload()
		pdc = frappe.get_doc(CHEQUE, security.converted_pdc_cheque)
		_columns, data = security_register({"company": COMPANY})
		row = next(r for r in data if r.get("security_cheque") == security.name)
		self.assertEqual(row.payment_entry, pdc.party_payment_entry)

	# ------------------------------------------------------------------ prints (F3)

	def test_pdc_cheque_receipt_prints_the_payment_entry(self):
		cheque = self.outgoing_cleared()
		html = frappe.get_print(CHEQUE, cheque.name, print_format="PDC Cheque Receipt")
		self.assertIn("Payment Entry No.", html)
		cell = re.search(r'class="pdc-pe-no">(.*?)</td>', html, re.S).group(1)
		self.assertEqual(
			re.findall(r"<bdi>([^<]*)</bdi>", cell), [cheque.party_payment_entry, cheque.bank_payment_entry]
		)
		self.assertIn("Party Posting", cell)
		self.assertIn(formatdate(day(-59)), cell)
		self.assertEqual(
			[pe["name"] for pe in treasury_payment_entries(cheque)],
			[cheque.party_payment_entry, cheque.bank_payment_entry],
		)

	def test_jinja_helper_requires_read_permission(self):
		cheque = self.outgoing_cleared()
		self.assertEqual(len(treasury_payment_entries(cheque)), 2)
		self.allow_only(make_pdc_cheque(amount=100, issue_date=day(-10), due_date=day(10)).name)
		for user in (ONE_CHEQUE_USER, NO_ACCESS_USER):
			with self.subTest(user=user), self.set_user(user):
				self.assertEqual(treasury_payment_entries(cheque), [])

	def test_receipt_without_payment_entry_prints_a_dash(self):
		cheque = make_pdc_cheque(amount=700, issue_date=day(-60), due_date=day(-30))
		html = frappe.get_print(CHEQUE, cheque.name, print_format="PDC Cheque Receipt")
		cell = re.search(r'class="pdc-pe-no">(.*?)</td>', html, re.S).group(1)
		self.assertEqual(cell.strip(), "—")
		self.assertEqual(treasury_payment_entries(frappe._dict(doctype="Cheque Batch", name="X")), [])

	def test_cheque_batch_receipt_prints_the_payment_entry_per_cheque(self):
		batch = self.make_outgoing_batch()
		entries = [
			frappe.db.get_value(CHEQUE, row.pdc_cheque, "party_payment_entry") for row in batch.cheques
		]
		self.assertTrue(all(entries))

		html = frappe.get_print("Cheque Batch", batch.name, print_format="Cheque Batch Receipt")
		printed = re.findall(r'class="pdc-pe-no"[^>]*><bdi>([^<]*)</bdi>', html)
		self.assertEqual(printed, entries)
		self.assertIn('colspan="5"', html)

	def test_cheque_batch_receipt_skips_cancelled_payment_entries(self):
		# "Cancel Payment Entry" on bounce: the bounced cheque stays on the receipt, its cancelled Payment
		# Entry does not (every view shows the submitted Payment Entries only)
		self.use_company_settings(on_bounce="Cancel Payment Entry")
		batch = self.make_outgoing_batch()
		first, second = (frappe.get_doc(CHEQUE, row.pdc_cheque) for row in batch.cheques)
		cancelled = first.party_payment_entry
		first.bounce(bounce_date=day(-100), reason="Account closed")
		self.assertEqual(frappe.db.get_value("Payment Entry", cancelled, "docstatus"), 2)

		html = frappe.get_print("Cheque Batch", batch.name, print_format="Cheque Batch Receipt")
		printed = re.findall(r'class="pdc-pe-no"[^>]*><bdi>([^<]*)</bdi>', html)
		self.assertEqual(printed, ["—", second.party_payment_entry])
		self.assertNotIn(cancelled, html)

	def make_outgoing_batch(self):
		"""A submitted outgoing batch of two cheques, handed over (a party Payment Entry each)."""
		issue = day(-160)
		batch = frappe.get_doc(
			{
				"doctype": "Cheque Batch",
				"direction": "Outgoing",
				"company": COMPANY,
				"party_type": "Supplier",
				"party": SUPPLIER,
				"bank_account": bank_account_name("EGP"),
				"issue_date": issue,
				"handover_date": add_days(issue, 1),
				"purpose": "Rent",
				"cheques": [
					{
						"due_date": add_days(issue, 30 * (i + 1)),
						"amount": 1000,
						"cheque_no": unique_cheque_no(),
					}
					for i in range(2)
				],
			}
		).insert()
		batch.submit()
		batch.reload()
		return batch
