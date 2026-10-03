"""EC-20 / EC-21: the bridge uses custody's public API only, and Treasury's reservations bind custody's own cap.

The contract tests run without custody; the reservation tests are skipped when custody is not installed.
"""

import pathlib
import re
import unittest
from unittest import mock

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import flt, today

import treasury
from treasury.integrations.custody import REQUEST, compat, documents, is_installed
from treasury.tests.utils import COMPANY, PE_SETTINGS, TreasuryTestMixin


class TestCustodyBridgeContract(IntegrationTestCase):
	def test_no_private_custody_imports_in_the_app(self):
		"""Only `custody.api` may be imported, and only by compat.py; tests may use custody helpers."""
		root = pathlib.Path(treasury.__file__).parent
		pattern = re.compile(r"^\s*(?:from|import)\s+(custody[\w.]*)", re.M)
		offenders = {}
		for path in root.rglob("*.py"):
			rel = path.relative_to(root).as_posix()
			if rel.startswith("tests/"):
				continue
			for module in pattern.findall(path.read_text(encoding="utf-8")):
				if rel == "integrations/custody/compat.py" and module == "custody.api":
					continue
				offenders.setdefault(rel, []).append(module)
		self.assertEqual(offenders, {})
		self.assertNotIn(
			"custody.custody", (root / "integrations/custody/compat.py").read_text(encoding="utf-8")
		)

	def test_provider_is_registered_for_custody(self):
		self.assertIn(
			"treasury.integrations.custody.documents.reservation_provider",
			frappe.get_hooks("custody_reservation_providers"),
		)
		self.assertTrue(
			callable(frappe.get_attr("treasury.integrations.custody.documents.reservation_provider"))
		)

	def test_graceful_without_custody(self):
		with mock.patch("frappe.get_installed_apps", return_value=["frappe", "erpnext", "treasury"]):
			self.assertFalse(is_installed())
			self.assertIsNone(compat.api_version())
			with self.assertRaises(frappe.ValidationError):
				compat.api()
			with self.assertRaises(frappe.ValidationError):
				compat.request_totals("CR-0001")

	def test_refuses_another_api_major(self):
		if not is_installed():
			self.skipTest("custody not installed")
		with mock.patch.object(compat, "API_MAJOR", 2):
			with self.assertRaises(frappe.ValidationError):
				compat.api()

	def test_api_version(self):
		if not is_installed():
			self.skipTest("custody not installed")
		self.assertEqual(compat.api_version(), "1.0")
		self.assertTrue(hasattr(compat.api(), "get_available_to_pay"))

	def test_provider_ignores_unknown_payment_type(self):
		self.assertEqual(documents.reservation_provider("Any Request", "Internal Transfer"), 0.0)


class TestCustodyReservations(TreasuryTestMixin, IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		if not is_installed():
			raise unittest.SkipTest("custody not installed")
		super().setUpClass()
		frappe.set_user("Administrator")
		from treasury.tests.custody_utils import ensure_custody_setup

		cls.custody = ensure_custody_setup()

	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		# the direct custody Payment Entry pays from the company cash account, which holds nothing (CB-14)
		self.use_company_settings(block_negative_cash_balance=0, **PE_SETTINGS)

	def tearDown(self):
		from treasury.tests.custody_utils import invariants

		frappe.set_user("Administrator")
		self.assertEqual(invariants(), [], "custody invariants broken")
		super().tearDown()

	def request(self, amount=10000):
		from treasury.tests.custody_utils import make_custodian, make_request

		_employee, custodian = make_custodian()
		return make_request(
			custodian, amount, cost_center=frappe.get_cached_value("Company", COMPANY, "cost_center")
		)

	def direct_pe(self, request: str, amount: float, submit=True):
		from custody.custody.payments import make_payment_entry

		pe = make_payment_entry(request, "Disbursement")
		pe.paid_amount = pe.received_amount = amount
		pe.set_amounts()
		pe.insert()
		if submit:
			pe.submit()
		return pe

	def test_open_cheque_reserves_amount_against_a_direct_payment(self):
		from custody.custody.exceptions import CustodyAmountError

		from treasury.tests.custody_utils import cheque_from_request, custody_payment_entries

		req = self.request(10000)
		cheque = cheque_from_request(req.name, amount=7000)
		self.assertEqual(documents.reserved(req.name, "Outgoing"), 7000)
		api = compat.api()
		self.assertEqual(api.get_reserved_amount(req.name, "Pay"), 7000)
		self.assertEqual(api.get_request_allowance(req.name, "Pay"), 10000)
		self.assertEqual(api.get_available_to_pay(req.name, "Pay"), 3000)

		# a direct custody Payment Entry may use only what the cheque did not reserve
		too_much = self.direct_pe(req.name, 5000, submit=False)
		with self.assertRaises(CustodyAmountError):
			too_much.submit()
		self.assertEqual(custody_payment_entries(req.name), [])
		ok = self.direct_pe(req.name, 3000)
		self.assertEqual(ok.docstatus, 1)

		# the cheque still posts its own 7,000: its reservation does not block itself
		cheque.deliver(handover_date=today())
		cheque.reload()
		self.assertEqual(cheque.accounting_status, "Party Posted")
		self.assertEqual(flt(frappe.db.get_value(REQUEST, req.name, "paid_amount")), 10000)

	def test_reservation_released_when_cheque_is_cancelled(self):
		from treasury.tests.custody_utils import cheque_from_request

		req = self.request(5000)
		cheque = cheque_from_request(req.name, amount=5000)
		self.assertEqual(compat.api().get_available_to_pay(req.name, "Pay"), 0)
		cheque.cancel_cheque(reason="Not needed")
		cheque.reload()
		self.assertEqual(compat.api().get_available_to_pay(req.name, "Pay"), 5000)
		self.assertEqual(self.direct_pe(req.name, 5000).docstatus, 1)

	def test_provider_excludes_only_its_own_document(self):
		from treasury.tests.custody_utils import cheque_from_request

		req = self.request(10000)
		first = cheque_from_request(req.name, amount=2000)
		second = cheque_from_request(req.name, amount=3000)
		own = frappe._dict(treasury_source_doctype="PDC Cheque", treasury_source_name=first.name)
		self.assertEqual(documents.reservation_provider(req.name, "Pay", voucher=own), 3000)
		other_doctype = frappe._dict(treasury_source_doctype="Bank Transfer", treasury_source_name=first.name)
		self.assertEqual(documents.reservation_provider(req.name, "Pay", voucher=other_doctype), 5000)
		self.assertEqual(documents.reservation_provider(req.name, "Pay"), 5000)
		self.assertEqual(documents.reservation_provider(req.name, "Receive"), 0)
		self.assertIsNotNone(second)
