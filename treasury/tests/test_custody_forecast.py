"""Phase 9b: approved, unpaid Custody Requests in the Bank Position Forecast (spec 9 §6.1, §8 9b).

Skipped when the custody app is not installed; custody's invariants are checked on tearDown.
"""

from unittest.mock import patch

import frappe
from frappe.utils import add_days, cint, flt, getdate, today

from treasury.integrations.custody import REQUEST, forecast
from treasury.pdc.bank_positions import collect_positions
from treasury.tests.custody_utils import (
	as_user,
	cheque_from_request,
	make_settlement,
	use_custody_settings,
)
from treasury.tests.test_custody_cheques import TREASURY_ONLY, CustodyChequeTestCase
from treasury.tests.test_custody_transfers import CustodyTransferTestCase
from treasury.tests.utils import COMPANY, bank_account_name, ensure_user

# assert_flow's bank when not given: the custody bank of the test (None means an unassigned flow)
DEFAULT = object()


class CustodyForecastMixin:
	"""The custody bank and forecast helpers, for the cheque and the transfer test cases."""

	def setUp(self):
		super().setUp()
		self.bank = bank_account_name("EGP")
		self.use_company_settings(custody_bank_account=self.bank, include_custody_replenishment_in_forecast=0)

	def flows(self, banks=None) -> list[dict]:
		return forecast.get_flows({"company": COMPANY, "bank_accounts": banks or [self.bank]})

	def flow_of(self, req, banks=None) -> dict | None:
		return next((f for f in self.flows(banks) if f["reference_name"] == req.name), None)

	def assert_flow(self, req, amount, date=None, bank=DEFAULT):
		flow = self.flow_of(req)
		self.assertIsNotNone(flow, f"no forecast flow for {req.name}")
		self.assertEqual(
			(flow["direction"], flow["bank_account"], getdate(flow["date"])),
			("out", self.bank if bank is DEFAULT else bank, getdate(date or req.posting_date)),
		)
		self.assertEqual((flt(flow["amount"]), flt(flow["amount_in_account_currency"])), (amount, amount))
		self.assertEqual((flow["reference_doctype"], flow["party"]), (REQUEST, req.custodian_name))
		return flow

	def permanent(self, mode="Manual"):
		use_custody_settings(
			self,
			enable_permanent_custody=1,
			replenishment_mode=mode,
			replenishment_threshold_percent=25,
		)
		return self.request(5000, custody_type="Permanent", float_amount=5000)


class TestCustodyForecast(CustodyForecastMixin, CustodyChequeTestCase):
	# ------------------------------------------------------------------ temporary requests

	def test_approved_request_is_an_outflow(self):
		req = self.request()
		self.assert_flow(req, 10000)

	def test_open_cheque_is_subtracted(self):
		req = self.request()
		cheque_from_request(req.name, amount=4000)
		self.assert_flow(req, 6000)

	def test_no_flow_once_posted(self):
		req = self.request()
		self.delivered(req)
		self.assertIsNone(self.flow_of(req))

	def test_delivered_not_posted_cheque_is_subtracted(self):
		"""QA gap 9: a cheque handed over but not posted to the custodian yet (Manual) is a flow of its own."""
		self.use_company_settings(outgoing_party_posting="Manual")
		req = self.request()
		cheque = cheque_from_request(req.name, amount=4000)
		cheque.deliver(handover_date=today())
		cheque.reload()
		self.assertEqual((cheque.operational_status, cheque.accounting_status), ("Delivered", "Not Posted"))
		self.assert_flow(req, 6000)

	# ------------------------------------------------------------------ permanent floats

	def test_awaiting_funding_without_the_option(self):
		req = self.permanent()
		self.assertEqual(req.status, "Awaiting Funding")
		self.assert_flow(req, 5000)

	def test_replenishment_due_follows_the_option(self):
		req = self.permanent()
		self.delivered(req)
		make_settlement(req.name, 3900)
		req.reload()
		self.assertEqual(req.status, "Replenishment Due")
		self.assertIsNone(self.flow_of(req))
		self.use_company_settings(include_custody_replenishment_in_forecast=1)
		self.assert_flow(req, 3900, date=today())

	def test_closing_float_not_replenished(self):
		"""QA gap 10: a float being closed is never topped up again: no flow, even with the option."""
		from custody.custody.permanent import request_close

		self.use_company_settings(include_custody_replenishment_in_forecast=1)
		req = self.permanent()
		self.delivered(req)
		make_settlement(req.name, 3900)
		self.assert_flow(req, 3900, date=today())
		request_close(req.name, "Treasury test closing")
		self.assertEqual(cint(frappe.db.get_value(REQUEST, req.name, "closing_requested")), 1)
		self.assertIsNone(self.flow_of(req))
		# the forecast's own rule, whatever custody stores in replenishment_due while closing
		row = frappe._dict(
			custody_type="Permanent",
			status="Replenishment Due",
			replenishment_due=1,
			closing_requested=1,
			replenishment_amount=3900,
		)
		on = getdate(today())
		self.assertEqual(forecast._base(row, True, on), (0.0, None))
		self.assertEqual(forecast._base(frappe._dict(row, closing_requested=0), True, on), (3900, on))

	# ------------------------------------------------------------------ banks, permissions, installation

	def test_without_custody_bank(self):
		self.use_company_settings(custody_bank_account=None, default_outgoing_cheque_bank_account=None)
		req = self.request()
		self.assert_flow(req, 10000, bank=None)
		# a custody bank outside the selected banks: no flow at all
		self.use_company_settings(custody_bank_account=self.bank)
		self.assertIsNone(self.flow_of(req, banks=[bank_account_name("USD")]))

	def test_no_read_or_not_installed(self):
		req = self.request()
		self.assertTrue(self.flow_of(req))
		with as_user(ensure_user(TREASURY_ONLY, ["Treasury User"])):
			self.assertEqual(self.flows(), [])
		with patch("treasury.integrations.custody.forecast.is_installed", return_value=False):
			self.assertEqual(self.flows(), [])

	def test_collect_positions_includes_the_flow(self):
		req = self.request()
		_positions, flows = collect_positions(COMPANY, 30, [self.bank])
		mine = [f for f in flows if f.get("reference_name") == req.name]
		self.assertEqual(len(mine), 1)
		self.assertEqual((mine[0].direction, flt(mine[0].amount_in_account_currency)), ("out", 10000))


class TestCustodyForecastTransfers(CustodyForecastMixin, CustodyTransferTestCase):
	def test_scheduled_transfer_is_subtracted(self):
		"""QA gap 9: a Scheduled transfer is a flow of its own (subtracted)."""
		req = self.request()
		transfer = self.transfer(req, amount=4000, execution_date=add_days(today(), 3))
		self.assertEqual(transfer.status, "Scheduled")
		self.assert_flow(req, 6000)

	def test_draft_transfer_is_not_subtracted(self):
		"""QA gap 9: a draft transfer is no forecast flow: the request still forecasts it all."""
		req = self.request()
		transfer = self.transfer(req, submit=False, amount=4000)
		self.assertEqual(transfer.docstatus, 0)
		self.assert_flow(req, 10000)
