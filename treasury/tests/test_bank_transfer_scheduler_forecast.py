"""Phase 7b: the daily job sending Scheduled transfers, and Bank Transfers in the Bank Position Forecast
(spec 7b §3.9, §3.10, §7 E)."""

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, flt, getdate, today

from treasury.bank_transfers import forecast
from treasury.bank_transfers.scheduler import post_scheduled_transfers
from treasury.pdc.report.bank_position_forecast import bank_position_forecast
from treasury.tests.utils import (
	BANK,
	COMPANY,
	TRANSFER_SETTINGS,
	USD_RATE,
	TreasuryTestMixin,
	acc,
	ensure_second_egp_bank,
	ensure_user,
	get_pe,
	make_bank_transfer,
	set_fee_rules,
	standard_fee_rules,
)

DOCTYPE = "Bank Transfer"
NO_ACCESS_USER = "treasury-transfer-forecast-noaccess@example.com"


class TestBankTransferScheduler(TreasuryTestMixin, IntegrationTestCase):
	def setUp(self):
		super().setUp()
		self.use_company_settings(**TRANSFER_SETTINGS)
		set_fee_rules(self, standard_fee_rules())
		self.today = getdate(today())

	def test_post_scheduled_transfers(self):
		execution = getdate(add_days(self.today, 3))
		transfer = make_bank_transfer(amount=20000, execution_date=execution)
		self.assertNotIn(transfer.name, post_scheduled_transfers(as_of=self.today)["posted"])
		transfer.reload()
		self.assertEqual(transfer.status, "Scheduled")

		result = post_scheduled_transfers(as_of=execution)
		self.assertIn(transfer.name, result["posted"])
		transfer.reload()
		self.assertEqual((transfer.status, transfer.accounting_status), ("Sent", "Posted"))
		self.assertEqual(getdate(get_pe(transfer.payment_entry).posting_date), execution)
		self.assertEqual(transfer.status_log[-1].user, "Administrator")

		self.assertNotIn(transfer.name, post_scheduled_transfers(as_of=execution)["posted"])

	def test_failing_transfer_is_rolled_back_and_logged_once(self):
		execution = getdate(add_days(self.today, 2))
		failing = make_bank_transfer(amount=20000, execution_date=execution)  # fee 30
		good = make_bank_transfer(amount=500, execution_date=execution, beneficiary_bank=BANK)  # no fee
		self.assertEqual((failing.fee_amount, good.fee_amount), (30, 0))
		# the fee account is disabled after the submit: posting the Payment Entry with the fee fails
		frappe.db.set_value("Account", acc("Treasury Bank Charges"), "disabled", 1)
		self.addCleanup(frappe.db.set_value, "Account", acc("Treasury Bank Charges"), "disabled", 0)

		def errors():
			return frappe.db.count(
				"Error Log", {"reference_doctype": DOCTYPE, "reference_name": failing.name}
			)

		# Error Logs survive the test rollback and transfer names repeat between runs: start from none
		frappe.db.delete("Error Log", {"reference_doctype": DOCTYPE, "reference_name": failing.name})
		before = errors()
		result = post_scheduled_transfers(as_of=execution)
		self.assertIn(failing.name, result["failed"])
		self.assertIn(good.name, result["posted"])
		failing.reload()
		self.assertEqual((failing.status, failing.payment_entry), ("Scheduled", None))
		self.assertFalse(frappe.db.exists("Payment Entry", {"treasury_source_name": failing.name}))
		self.assertEqual(errors(), before + 1)

		post_scheduled_transfers(as_of=execution)
		self.assertEqual(errors(), before + 1)


class TestBankTransferForecast(TreasuryTestMixin, IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_user(NO_ACCESS_USER, [])

	def setUp(self):
		super().setUp()
		self.use_company_settings(**TRANSFER_SETTINGS)
		set_fee_rules(self, standard_fee_rules())
		self.today = getdate(today())
		self.egp = self.data.egp
		self.second = ensure_second_egp_bank()

	def filters(self, banks, currency_view="Company Currency", to_date=None):
		return frappe._dict(
			company=COMPANY,
			bank_accounts=banks,
			to_date=to_date,
			currency_view=currency_view,
			currency="EGP",
			today=self.today,
		)

	def flows_of(self, transfer, banks, **kwargs):
		return [
			f for f in forecast.get_flows(self.filters(banks, **kwargs)) if f["reference_name"] == transfer
		]

	def test_get_flows(self):
		day3 = getdate(add_days(self.today, 3))
		payment = make_bank_transfer(amount=20000, execution_date=day3)
		internal = make_bank_transfer(
			transfer_type="Internal Transfer",
			amount=5000,
			to_bank_account=self.second.bank_account,
			execution_date=day3,
		)
		sent = make_bank_transfer(amount=3000)
		banks = [self.egp.bank_account, self.second.bank_account]

		flows = self.flows_of(payment.name, banks)
		self.assertEqual(
			[(f["date"], f["direction"], f["bank_account"], f["amount"]) for f in flows],
			[(day3, "out", self.egp.bank_account, 20030)],
		)
		flows = self.flows_of(internal.name, banks)
		self.assertEqual(
			sorted((f["direction"], f["bank_account"], f["amount"]) for f in flows),
			[("in", self.second.bank_account, 5000), ("out", self.egp.bank_account, 5000)],
		)
		# the bank filter
		self.assertEqual(
			[f["direction"] for f in self.flows_of(internal.name, [self.second.bank_account])], ["in"]
		)
		self.assertEqual(self.flows_of(payment.name, [self.second.bank_account]), [])
		# Sent transfers are in the GL already
		self.assertEqual(self.flows_of(sent.name, banks), [])
		# To Date
		self.assertEqual(self.flows_of(payment.name, banks, to_date=add_days(self.today, 2)), [])

	def test_usd_flows_are_converted(self):
		usd = self.data.usd
		transfer = make_bank_transfer(
			amount=100, bank_account=usd.bank_account, execution_date=add_days(self.today, 4)
		)
		flows = self.flows_of(transfer.name, [usd.bank_account])
		fee = flt(transfer.fee_amount)
		self.assertEqual(
			[(f["amount_in_account_currency"], f["amount"]) for f in flows],
			[(100 + fee, flt((100 + fee) * USD_RATE, 2))],
		)

	def test_no_read_permission(self):
		make_bank_transfer(amount=20000, execution_date=add_days(self.today, 3))
		with self.set_user(NO_ACCESS_USER):
			self.assertEqual(forecast.get_flows(self.filters([self.egp.bank_account])), [])
			self.assertEqual(forecast.get_summary(self.filters([self.egp.bank_account])), [])

	def test_get_summary(self):
		banks = [self.egp.bank_account, self.second.bank_account]
		# Sent transfers of other tests of the class are there too: compare the change
		before = self.summary_value(banks) or 0
		before_second = self.summary_value([self.second.bank_account]) or 0
		make_bank_transfer(amount=20000)  # out 20,030
		make_bank_transfer(transfer_type="Party Receipt", amount=1000)  # in 995
		make_bank_transfer(
			transfer_type="Internal Transfer", amount=700, to_bank_account=self.second.bank_account
		)  # out 700 on the EGP bank, in 700 on the second bank
		completed = make_bank_transfer(amount=4000)
		completed.mark_completed(completion_date=self.today)
		scheduled = make_bank_transfer(amount=5000, execution_date=add_days(self.today, 3))
		self.assertEqual(scheduled.status, "Scheduled")
		self.assertEqual(flt(self.summary_value(banks) - before, 2), flt(20030 - 995 + 700 - 700, 2))
		self.assertEqual(flt(self.summary_value([self.second.bank_account]) - before_second, 2), -700)
		self.assertIsNone(self.summary_value([self.data.usd.bank_account]))

	def summary_value(self, banks):
		cards = forecast.get_summary(self.filters(banks))
		self.assertLessEqual(len(cards), 1)
		return flt(cards[0]["value"], 2) if cards else None

	def test_bank_position_forecast(self):
		day3 = getdate(add_days(self.today, 3))
		transfer = make_bank_transfer(amount=20000, execution_date=day3)
		_columns, data, *_rest = bank_position_forecast.execute(
			{
				"company": COMPANY,
				"bank_account": [self.egp.bank_account],
				"from_date": self.today,
				"to_date": add_days(self.today, 13),
				"periodicity": "Daily",
				"show_details": 1,
			}
		)
		detail = [r for r in data if r.get("reference_name") == transfer.name]
		self.assertEqual(len(detail), 1, "the Scheduled transfer is listed in the details")
		self.assertEqual(flt(detail[0].expected_out, 2), 20030)
		self.assertEqual(getdate(detail[0].get("date") or day3), day3)
