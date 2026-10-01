"""Phase 9b: approved, unpaid Custody Requests in the Bank Position Forecast (spec 9 §6.1, §8 9b).

Skipped when the custody app is not installed; custody's invariants are checked on tearDown.
"""

from unittest.mock import patch

import frappe
from frappe.utils import flt, getdate, today

from treasury.integrations.custody import REQUEST, forecast
from treasury.pdc.bank_positions import collect_positions
from treasury.tests.custody_utils import (
	as_user,
	cheque_from_request,
	make_settlement,
	use_custody_settings,
)
from treasury.tests.test_custody_cheques import TREASURY_ONLY, CustodyChequeTestCase
from treasury.tests.utils import COMPANY, bank_account_name, ensure_user


class TestCustodyForecast(CustodyChequeTestCase):
	def setUp(self):
		super().setUp()
		self.bank = bank_account_name("EGP")
		self.use_company_settings(
			custody_bank_account=self.bank, include_custody_replenishment_in_forecast=0
		)

	def flows(self, banks=None) -> list[dict]:
		return forecast.get_flows({"company": COMPANY, "bank_accounts": banks or [self.bank]})

	def flow_of(self, req, banks=None) -> dict | None:
		return next((f for f in self.flows(banks) if f["reference_name"] == req.name), None)

	def assert_flow(self, req, amount, date=None, bank=None):
		flow = self.flow_of(req)
		self.assertIsNotNone(flow, f"no forecast flow for {req.name}")
		self.assertEqual(
			(flow["direction"], flow["bank_account"], getdate(flow["date"])),
			("out", bank or self.bank, getdate(date or req.posting_date)),
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
