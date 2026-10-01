"""Phase 9b: the Employee Custody section of the Treasury Dashboard, the Custody Request connections, the
sidebar section and the Employee Custody settings (spec 9 §2.3, §5, §6.2, §6.3, §8 9b).

Skipped when the custody app is not installed; custody's invariants are checked on tearDown. The dashboard
figures are compared before / after the test's own requests (IntegrationTestCase keeps the data of earlier
tests until the end of the class).
"""

import json
from unittest.mock import patch

import frappe
from frappe import _
from frappe.desk.notifications import get_open_count
from frappe.utils import add_days, flt, today

from treasury.integrations.custody import REQUEST, dashboard
from treasury.pdc import overview
from treasury.tests.custody_utils import (
	as_user,
	assert_request,
	cheque_from_request,
	make_custodian,
	make_request,
	make_settlement,
	use_custody_settings,
)
from treasury.tests.test_custody_cheques import TREASURY_ONLY, CustodyChequeTestCase
from treasury.tests.test_release import CUSTODY_SIDEBAR_ITEMS
from treasury.tests.test_translations import SIDEBAR_FILE
from treasury.tests.utils import (
	COMPANY,
	bank_account_name,
	ensure_other_company_bank,
	ensure_user,
	make_cheque_book,
	unique_leaf_start,
)

SETTINGS = "Treasury Company Settings"
KEYS = ("open", "overdue", "to_pay", "permanent", "replenishment_due")


class TestCustodyDashboard(CustodyChequeTestCase):
	def section(self) -> frappe._dict:
		return overview.get_overview(COMPANY, None, 7).sections.custody

	def figures(self) -> dict:
		section = self.section()
		return {key: (section[key].count, flt(section[key].amount)) for key in KEYS}

	def delta(self, before: dict, after: dict) -> dict:
		return {key: (after[key][0] - before[key][0], flt(after[key][1] - before[key][1], 2)) for key in KEYS}

	# ------------------------------------------------------------------ the section

	def test_section_values(self):
		before = self.figures()
		unpaid = self.request()  # approved 10,000, not paid
		paid = self.request(4000)
		self.delivered(paid)  # balance 4,000
		after = self.figures()
		self.assertEqual(
			self.delta(before, after),
			{
				"open": (1, 4000),
				"overdue": (0, 0),
				"to_pay": (1, 10000),
				"permanent": (0, 0),
				"replenishment_due": (0, 0),
			},
		)
		section = self.section()
		self.assertEqual(section.open.route, ["query-report", "Custody Balances"])
		self.assertEqual(section.open.route_options, {"company": COMPANY})
		self.assertEqual(section.overdue.route_options, {"company": COMPANY, "only_overdue": 1})
		self.assertEqual(section.to_pay.route, ["List", REQUEST])
		self.assertEqual(
			section.to_pay.route_options["status"], ["in", ["Approved", "Partly Paid", "Awaiting Funding"]]
		)
		self.assertEqual(section.permanent.route, ["query-report", "Permanent Custody Register"])
		self.assertEqual(section.replenishment_due.route_options["status"], "Replenishment Due")
		self.assertIn("extra", section.permanent)
		self.assertTrue(unpaid.name)

	def test_permanent_and_replenishment_figures(self):
		"""QA gap 11: a float of 5,000 funded, then 3,900 settled (Replenishment Due)."""
		use_custody_settings(
			self, enable_permanent_custody=1, replenishment_mode="Manual", replenishment_threshold_percent=25
		)
		before = self.figures()
		extra = flt(self.section().permanent.extra)
		req = self.request(5000, custody_type="Permanent", float_amount=5000)
		self.delivered(req)
		funded = self.figures()
		self.assertEqual(
			self.delta(before, funded),
			{
				"open": (1, 5000),
				"overdue": (0, 0),
				"to_pay": (0, 0),
				"permanent": (1, 5000),
				"replenishment_due": (0, 0),
			},
		)
		self.assertEqual(flt(self.section().permanent.extra - extra, 2), 5000)

		make_settlement(req.name, 3900)
		assert_request(self, req.name, status="Replenishment Due", replenishment_amount=3900)
		self.assertEqual(
			self.delta(funded, self.figures()),
			{
				"open": (0, -3900),
				"overdue": (0, 0),
				"to_pay": (0, 0),
				"permanent": (0, -3900),
				"replenishment_due": (1, 3900),
			},
		)

	def test_overdue_figures(self):
		"""QA gap 11: a temporary request paid and past its expected settlement date is overdue."""
		before = self.figures()
		req = self.request(
			1000, posting_date=add_days(today(), -40), expected_settlement_date=add_days(today(), -10)
		)
		self.delivered(req)
		assert_request(self, req.name, is_overdue=1, balance_amount=1000)
		self.assertEqual(
			self.delta(before, self.figures()),
			{
				"open": (1, 1000),
				"overdue": (1, 1000),
				"to_pay": (0, 0),
				"permanent": (0, 0),
				"replenishment_due": (0, 0),
			},
		)

	def test_hidden_without_custody_read(self):
		with as_user(ensure_user(TREASURY_ONLY, ["Treasury User"])):
			result = overview.get_overview(COMPANY, None, 7)
		self.assertIn(overview.CUSTODY, result.hidden)
		self.assertNotIn(overview.CUSTODY, result.sections)

	def test_custody_user_sees_own_requests(self):
		email = f"trs-dash-custody-{frappe.generate_hash(length=6)}@treasury.test"
		user = ensure_user(email, ["Treasury User", "Custody User"])
		_employee, own_custodian = make_custodian(user=user)
		make_request(own_custodian, 700, cost_center=self.cost_center)
		self.request(900)  # another custodian's request
		with as_user(user):
			section = self.section()
		self.assertEqual((section.to_pay.count, flt(section.to_pay.amount)), (1, 700))

	def test_absent_without_custody(self):
		with patch("treasury.integrations.custody.is_installed", return_value=False):
			result = overview.get_overview(COMPANY, None, 7)
		self.assertNotIn(overview.CUSTODY, result.sections)
		self.assertNotIn(overview.CUSTODY, result.hidden)
		self.assertTrue(callable(dashboard.custody_section))

	# ------------------------------------------------------------------ connections, sidebar

	def test_connections(self):
		data = frappe.get_meta(REQUEST).get_dashboard_data()
		group = next(g for g in data.transactions if g.get("label") == _("Treasury"))
		for doctype in ("PDC Cheque", "Bank Transfer", "Payment Entry"):
			self.assertIn(doctype, group["items"])
		self.assertEqual(data.non_standard_fieldnames["PDC Cheque"], "source_name")
		self.assertEqual(data.non_standard_fieldnames["Bank Transfer"], "source_name")
		self.assertEqual(data.non_standard_fieldnames["Payment Entry"], "custody_request")
		self.assertEqual(data.dynamic_links["source_name"], [REQUEST, "source_doctype"])

		req = self.request()
		cheque_from_request(req.name, amount=1000)
		# get_open_count sets a statement timeout on the connection
		self.addCleanup(frappe.db.set_execution_timeout, 0)
		counts = {
			row["doctype"]: row["count"]
			for row in get_open_count(REQUEST, req.name, ["PDC Cheque", "Bank Transfer"])["count"][
				"external_links_found"
			]
		}
		self.assertEqual(counts, {"PDC Cheque": 1, "Bank Transfer": 0})

	def test_sidebar_section(self):
		items = json.loads(SIDEBAR_FILE.read_text(encoding="utf-8"))["items"]
		labels = [i["label"] for i in items if i["type"] == "Section Break"]
		self.assertIn("Employee Custody", labels)
		start = next(i for i, item in enumerate(items) if item["label"] == "Employee Custody")
		section = items[start]
		self.assertEqual((section["icon"], section["indent"], section["keep_closed"]), ("wallet", 1, 1))
		children = []
		for item in items[start + 1 :]:
			if item["type"] == "Section Break":
				break
			children.append((item["link_type"], item["link_to"]))
		self.assertEqual(children, list(CUSTODY_SIDEBAR_ITEMS))
		for link_type, name in children:
			self.assertTrue(frappe.db.exists(link_type, name), name)

	# ------------------------------------------------------------------ settings

	def settings(self, **values):
		doc = frappe.get_doc(SETTINGS, COMPANY)
		doc.update(values)
		return doc

	def test_settings_validation(self):
		egp, usd = bank_account_name("EGP"), bank_account_name("USD")
		self.settings(custody_bank_account=egp).validate_custody_settings()

		# the site's translations apply (en.mo words "PDC" as "Cheque"): compare with the translated messages
		label = _(frappe.get_meta(SETTINGS).get_label("custody_bank_account"))
		unmapped = ensure_other_company_bank().bank_account
		cases = {
			unmapped: _("{0} {1} needs a row in PDC Accounts.").format(label, frappe.bold(unmapped)),
			usd: _("{0} must be in the company currency {1}.").format(f"{label} {frappe.bold(usd)}", "EGP"),
		}
		for bank, message in cases.items():
			with self.subTest(bank=bank):
				with self.assertRaises(frappe.ValidationError) as cm:
					self.settings(custody_bank_account=bank).validate_custody_settings()
				self.assertIn(message, str(cm.exception))

		start = unique_leaf_start(10)
		usd_book = make_cheque_book(start, start + 9, currency="USD")
		message = _("Cheque Book {0} does not belong to Bank Account {1}.").format(
			frappe.bold(usd_book.name), frappe.bold(egp)
		)
		doc = self.settings(custody_bank_account=egp, custody_cheque_book=usd_book.name)
		with self.assertRaises(frappe.ValidationError) as cm:
			doc.validate_custody_settings()
		self.assertIn(message, str(cm.exception))

		# without a bank, the cheque book is dropped
		doc = self.settings(custody_bank_account=None, custody_cheque_book=usd_book.name)
		doc.validate_custody_settings()
		self.assertIsNone(doc.custody_cheque_book)

	def test_settings_onload(self):
		doc = frappe.get_doc(SETTINGS, COMPANY)
		doc.run_method("onload")
		self.assertTrue(doc.get_onload().get("custody_installed"))
		with patch("treasury.integrations.custody.is_installed", return_value=False):
			doc = frappe.get_doc(SETTINGS, COMPANY)
			doc.run_method("onload")
			self.assertFalse(doc.get_onload().get("custody_installed"))
		meta = frappe.get_meta(SETTINGS)
		self.assertEqual(meta.get_field("include_custody_replenishment_in_forecast").default, "0")
		options = meta.get_field("custody_default_method").options.split("\n")
		self.assertEqual(options, ["Cheque", "Bank Transfer", "Fawran"])
