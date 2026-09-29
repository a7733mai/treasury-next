"""Letters of Credit (Phase 5b, PLAN.md §8): opening, amendment, documents, acceptance (usance forecast),
settlement to the supplier (margin + bank, invoice allocation, foreign currency exchange difference),
utilisation, expiry, cancel and the LC Register.

Fixed dates in 2025. Every test creates its own facility on its own bank account (`make_own_bank`). Facility:
total 1,000,000; Letter of Credit sub-limit 500,000 with a 20% cash margin and a 0.5% commission.
"""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, flt, getdate

from treasury.accounting import posting
from treasury.bank_facilities import dashboard, lc, scheduler
from treasury.bank_facilities.report.lc_register.lc_register import execute as lc_register
from treasury.bank_facilities.utilisation import get_utilisation
from treasury.pdc.report.bank_position_forecast.bank_position_forecast import BankPositionForecast
from treasury.tests.utils import (
	COMPANY,
	SUPPLIER,
	USD_SUPPLIER,
	TreasuryTestMixin,
	acc,
	assert_gl_entries,
	ensure_facility_accounts,
	ensure_instrument_accounts,
	ensure_user,
	get_account_balance,
	get_party_outstanding,
	get_voucher_count,
	make_bank_facility,
	make_letter_of_credit,
	make_own_bank,
	make_purchase_invoice,
	make_usd_purchase_invoice,
)

D = getdate
DOCTYPE = "Letter of Credit"
LIMIT = "Letter of Credit"
USER = "treasury-lc-user@example.com"


class LCTestCase(TreasuryTestMixin, IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.accounts = ensure_facility_accounts()
		cls.inst = ensure_instrument_accounts()

	def setUp(self):
		super().setUp()
		self.use_company_settings(accrue_facility_interest=0)

	def facility(self, lc_limit=500_000, currency="EGP", total=1_000_000, margin=20, commission=0.5):
		bank = make_own_bank(currency)
		facility = make_bank_facility(
			currency=currency,
			total_limit=total,
			limits=[
				{
					"limit_type": LIMIT,
					"limit_amount": lc_limit,
					"cash_margin_percent": margin,
					"commission_rate": commission,
				}
			],
			bank_account=bank.bank_account,
		)
		return facility, bank

	def used(self, facility, as_on=None) -> float:
		return get_utilisation(facility.name, LIMIT, as_on).limits[0].utilised

	def voucher(self, doc, event: str, cycle: int = 1, doctype: str = "Journal Entry") -> str:
		return posting.find_voucher(doctype, DOCTYPE, doc.name, event, cycle)


class TestLetterOfCredit(LCTestCase):
	def test_sight_lifecycle_with_invoice_allocation(self):
		facility, bank = self.facility()
		credit = make_letter_of_credit(facility)
		credit.reload()
		self.assertEqual(
			(credit.status, credit.current_amount, credit.open_amount, credit.margin_held),
			("Opened", 100_000, 100_000, 20_000),
		)
		self.assertEqual(credit.commission_amount, 500)
		opening = D("2025-02-01")
		assert_gl_entries(
			self,
			"Journal Entry",
			credit.margin_journal,
			[(self.inst.lc_margin, 20_000, 0, None, opening), (bank.bank_gl, 0, 20_000, None, opening)],
		)
		assert_gl_entries(
			self,
			"Journal Entry",
			credit.commission_journal,
			[(self.accounts.commission, 500, 0), (bank.bank_gl, 0, 500)],
		)
		self.assertEqual(self.used(facility), 100_000)

		# amendment: a higher amount (checked against the limit), later expiry, charges; the margin follows
		credit.amend(
			amendment_date="2025-03-01", new_amount=120_000, new_expiry_date="2025-09-30", charges=200
		)
		credit.reload()
		self.assertEqual(
			(credit.status, credit.current_amount, credit.margin_held, D(credit.effective_expiry_date)),
			("Amended", 120_000, 24_000, D("2025-09-30")),
		)
		assert_gl_entries(
			self,
			"Journal Entry",
			self.voucher(credit, lc.EVENT_MARGIN_TOPUP),
			[(self.inst.lc_margin, 4_000, 0), (bank.bank_gl, 0, 4_000)],
		)
		assert_gl_entries(
			self,
			"Journal Entry",
			self.voucher(credit, lc.EVENT_AMENDMENT_CHARGES),
			[(self.inst.lc_charges, 200, 0), (bank.bank_gl, 0, 200)],
		)
		self.assertEqual(self.used(facility), 120_000)

		# documents: the documents amount becomes the open amount (tolerance 0: at most the amount)
		self.assertRaises(
			frappe.ValidationError,
			credit.receive_documents,
			documents_date="2025-04-01",
			documents_amount=120_001,
		)
		credit.receive_documents(documents_date="2025-04-01", documents_amount=118_000, charges=100)
		credit.reload()
		self.assertEqual((credit.status, credit.open_amount), ("Documents Received", 118_000))
		assert_gl_entries(
			self,
			"Journal Entry",
			self.voucher(credit, lc.EVENT_DOCUMENT_CHARGES),
			[(self.inst.lc_charges, 100, 0), (bank.bank_gl, 0, 100)],
		)
		self.assertRaises(frappe.ValidationError, credit.accept, acceptance_date="2025-04-02")  # Sight

		# settlement: margin back to the bank, the supplier paid from the bank by a Payment Entry
		pi = make_purchase_invoice(118_000, posting_date="2025-03-20")
		credit.settle(settlement_date="2025-04-10", purchase_invoice=pi.name)
		credit.reload()
		date = D("2025-04-10")
		self.assertEqual(
			(credit.status, credit.open_amount, credit.margin_held, credit.settled_amount),
			("Settled", 0, 0, 118_000),
		)
		assert_gl_entries(
			self,
			"Journal Entry",
			self.voucher(credit, lc.EVENT_MARGIN_RELEASE),
			[(bank.bank_gl, 24_000, 0, None, date), (self.inst.lc_margin, 0, 24_000, None, date)],
		)
		assert_gl_entries(
			self,
			"Payment Entry",
			credit.payment_entry,
			[(self.data.payable, 118_000, 0, SUPPLIER, date), (bank.bank_gl, 0, 118_000, None, date)],
		)
		self.assertEqual(get_party_outstanding("Purchase Invoice", pi.name), 0)
		self.assertEqual(get_account_balance(bank.bank_gl, date), flt(-500 - 200 - 100 - 118_000))
		self.assertEqual(self.used(facility), 0)

		# utilisation on past dates, from the status log
		self.assertEqual(self.used(facility, as_on="2025-02-15"), 100_000)
		self.assertEqual(self.used(facility, as_on="2025-03-15"), 120_000)
		self.assertEqual(self.used(facility, as_on="2025-04-05"), 118_000)

		# idempotency and settled credits stay settled
		count = get_voucher_count(DOCTYPE, credit.name)
		self.assertRaises(frappe.ValidationError, credit.settle, settlement_date="2025-04-11")
		self.assertEqual(get_voucher_count(DOCTYPE, credit.name), count)
		self.assertRaises(frappe.ValidationError, credit.cancel)

	def test_amend_down_releases_margin_and_limit_check(self):
		facility, bank = self.facility(lc_limit=110_000)
		credit = make_letter_of_credit(facility)
		date = "2025-03-01"
		self.assertRaises(frappe.ValidationError, credit.amend, amendment_date=date, new_amount=120_000)
		self.assertRaises(frappe.ValidationError, credit.amend, amendment_date=date)  # nothing to amend
		credit.amend(amendment_date=date, new_amount=80_000)
		credit.reload()
		self.assertEqual(
			(credit.current_amount, credit.open_amount, credit.margin_held), (80_000, 80_000, 16_000)
		)
		assert_gl_entries(
			self,
			"Journal Entry",
			self.voucher(credit, lc.EVENT_MARGIN_RELEASE),
			[(bank.bank_gl, 4_000, 0), (self.inst.lc_margin, 0, 4_000)],
		)
		self.assertEqual(self.used(facility), 80_000)
		# the freed limit can be used again
		credit.amend(amendment_date="2025-03-02", new_amount=110_000)
		credit.reload()
		self.assertEqual((credit.current_amount, credit.margin_held), (110_000, 22_000))
		self.assertEqual(self.voucher(credit, lc.EVENT_MARGIN_TOPUP), credit.status_log[-1].voucher_no)

	def test_limit_exceeded_on_open(self):
		facility, _bank = self.facility(lc_limit=150_000)
		make_letter_of_credit(facility, amount=100_000)
		self.assertRaises(frappe.ValidationError, make_letter_of_credit, facility, amount=60_000)

	def test_usance_acceptance_is_forecast_until_settled(self):
		facility, bank = self.facility()
		credit = make_letter_of_credit(facility, amount=50_000, lc_type="Usance", usance_days=90)
		credit.receive_documents(documents_date="2025-05-01")
		credit.reload()
		self.assertEqual(credit.documents_amount, 50_000)
		self.assertRaises(frappe.ValidationError, credit.settle, settlement_date="2025-05-02")  # not accepted
		credit.accept(acceptance_date="2025-05-05")
		credit.reload()
		self.assertEqual((credit.status, D(credit.maturity_date)), ("Accepted", D("2025-08-03")))
		self.assertEqual(self.used(facility), 50_000)

		filters = {"company": COMPANY, "bank_accounts": [bank.bank_account], "to_date": "2025-12-31"}
		flows = [f for f in lc.get_flows(filters) if f["reference_name"] == credit.name]
		# the bank pays at maturity what the margin (10,000) does not cover
		self.assertEqual(
			[(f["date"], f["direction"], f["amount"], f["amount_in_account_currency"]) for f in flows],
			[(D("2025-08-03"), "out", 40_000, 40_000)],
		)

		today = D("2025-07-01")
		with patch("treasury.pdc.report_utils.today", return_value=str(today)):
			report = BankPositionForecast(
				{
					"company": COMPANY,
					"bank_account": [bank.bank_account],
					"from_date": today,
					"to_date": add_days(today, 61),
					"periodicity": "Monthly",
					"currency_view": "Company Currency",
					"show_details": 1,
				}
			)
			_columns, data, _message, _chart, _summary = report.run()
		rows = [r for r in data if r.get("indent") and r.get("reference_name") == credit.name]
		self.assertEqual([(r.due_date, r.expected_out) for r in rows], [(D("2025-08-03"), 40_000)])

		credit.settle(settlement_date="2025-08-03")
		credit.reload()
		assert_gl_entries(
			self,
			"Payment Entry",
			credit.payment_entry,
			[(self.data.payable, 50_000, 0, SUPPLIER), (bank.bank_gl, 0, 50_000, None)],
		)
		self.assertFalse([f for f in lc.get_flows(filters) if f["reference_name"] == credit.name])
		self.assertEqual(self.used(facility), 0)

	def test_usd_settlement_books_exchange_differences(self):
		facility, bank = self.facility(
			currency="USD", total=100_000, lc_limit=50_000, margin=10, commission=0
		)
		gain_loss = frappe.get_cached_value("Company", COMPANY, "exchange_gain_loss_account")
		credit = make_letter_of_credit(
			facility,
			amount=1_000,
			supplier=USD_SUPPLIER,
			margin_account=self.inst.lc_margin_usd,
			exchange_rate=50,
		)
		credit.reload()
		self.assertEqual((credit.currency, credit.margin_held), ("USD", 100))
		assert_gl_entries(
			self,
			"Journal Entry",
			credit.margin_journal,
			[(self.inst.lc_margin_usd, 5_000, 0), (bank.bank_gl, 0, 5_000)],
		)
		credit.receive_documents(documents_date="2025-03-01")
		pi = make_usd_purchase_invoice(1_000, posting_date="2025-02-15")  # 50,000 at 50
		loss_before = get_account_balance(gain_loss, "2025-12-31")

		credit.settle(settlement_date="2025-03-10", exchange_rate=52, purchase_invoice=pi.name)
		credit.reload()
		# the margin carried at 50 comes back to the bank at 52: a gain of 200 on the margin
		assert_gl_entries(
			self,
			"Journal Entry",
			self.voucher(credit, lc.EVENT_MARGIN_RELEASE),
			[(bank.bank_gl, 5_200, 0), (self.inst.lc_margin_usd, 0, 5_000), (gain_loss, 0, 200)],
		)
		usd_payable = acc("Treasury Creditors USD")
		assert_gl_entries(
			self,
			"Payment Entry",
			credit.payment_entry,
			[(usd_payable, 52_000, 0, USD_SUPPLIER), (bank.bank_gl, 0, 52_000, None)],
		)
		self.assertEqual(get_party_outstanding("Purchase Invoice", pi.name), 0)
		# ERPNext books the invoice's loss (1,000 x (52 - 50)); the margin gained 200
		self.assertEqual(flt(get_account_balance(gain_loss, "2025-12-31") - loss_before, 2), 1_800)
		self.assertEqual(get_account_balance(bank.bank_gl, "2025-12-31"), -1_000)  # in USD

	def test_expiry_and_margin_release(self):
		facility, bank = self.facility()
		credit = make_letter_of_credit(facility, expiry_date="2025-03-31")
		with_documents = make_letter_of_credit(facility, amount=10_000, expiry_date="2025-03-31")
		with_documents.receive_documents(documents_date="2025-03-15")
		result = scheduler.refresh_facilities(as_of="2025-04-02")
		self.assertIn(credit.name, result["expired_instruments"])
		self.assertNotIn(with_documents.name, result["expired_instruments"])  # documents are settled
		credit.reload()
		self.assertEqual((credit.status, credit.open_amount, credit.margin_held), ("Expired", 0, 20_000))
		self.assertEqual(self.used(facility), 10_000)
		self.assertEqual(self.used(facility, as_on="2025-03-31"), 110_000)

		credit.release_margin(release_date="2025-04-05")
		credit.reload()
		self.assertEqual((credit.status, credit.margin_held), ("Expired", 0))
		assert_gl_entries(
			self,
			"Journal Entry",
			credit.status_log[-1].voucher_no,
			[(bank.bank_gl, 20_000, 0), (self.inst.lc_margin, 0, 20_000)],
		)
		self.assertRaises(frappe.ValidationError, credit.release_margin, release_date="2025-04-06")

	def test_cancel_and_validations(self):
		facility, bank = self.facility()
		credit = make_letter_of_credit(facility)
		credit.cancel()
		credit.reload()
		self.assertEqual(credit.status, "Cancelled")
		self.assertEqual(get_voucher_count(DOCTYPE, credit.name, docstatus=1), 0)
		self.assertEqual(get_account_balance(bank.bank_gl, "2025-12-31"), 0)
		self.assertEqual(self.used(facility), 0)

		cases = [
			dict(expiry_date="2025-01-15"),  # before the opening
			dict(latest_shipment_date="2025-09-30"),  # after the expiry
			dict(lc_type="Usance", usance_days=0),
			dict(tolerance_percent=150),
			dict(margin_account=None),
		]
		for values in cases:
			with self.subTest(values=values):
				self.assertRaises(
					frappe.ValidationError, make_letter_of_credit, facility, submit=False, **values
				)

	def test_protection_and_permissions(self):
		ensure_user(USER, ["Treasury User"])
		facility, _bank = self.facility()
		credit = make_letter_of_credit(facility)
		credit.reload()
		credit.open_amount = 1
		self.assertRaises(frappe.ValidationError, credit.save)
		with self.set_user(USER):
			doc = frappe.get_doc(DOCTYPE, credit.name)
			self.assertRaises(frappe.PermissionError, doc.amend, amendment_date="2025-03-01", charges=10)

	def test_register_and_card(self):
		def card():
			return dashboard.get_instrument_card({"company": COMPANY, "scope": "open_lcs"})["value"]

		before = card()
		facility, _bank = self.facility()
		credit = make_letter_of_credit(facility, lc_type="Usance", usance_days=60)
		credit.receive_documents(documents_date="2025-05-01", documents_amount=90_000)
		credit.accept(acceptance_date="2025-05-02")
		self.assertEqual(card() - before, 90_000)
		filters = {"company": COMPANY, "facility": facility.name, "open_only": 1}
		with patch("treasury.pdc.report_utils.today", return_value="2025-06-01"):
			_columns, data, _m, _c, summary = lc_register(filters)
		self.assertEqual([r.name for r in data], [credit.name])
		self.assertEqual((D(data[0].maturity_date), data[0].days_to_maturity), (D("2025-07-01"), 30))
		cards = {c["label"]: c["value"] for c in summary}
		self.assertEqual((cards["Open Credits"], cards["Accepted, to Pay"]), (90_000, 70_000))
