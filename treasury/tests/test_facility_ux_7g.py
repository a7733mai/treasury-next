"""Phase 7g-2 (design 7g): the server data behind the facilities UX — the `__onload` of the facility forms
(installment info P11 / P14, security summary P5, cheque installment P16, Trust Receipt defaults P18), the
derived installment states (P24) with the "Covered by Cheques" card and the "Auto Debits Awaiting Statement"
number card (P25), Cost of Financing without all-zero categories and with its chart (P20 / P21), the utilisation
tree by lender (P22) with the Istisna'a commitment (P23), the Connections and the Invoice Discounting link
filters (P19).

The reports' "today" is frozen to 2025-07-01 (`treasury.pdc.report_utils.today` is patched).
"""

import json
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import flt, getdate

from treasury.bank_facilities import dashboard, trust_receipt
from treasury.bank_facilities.report.cost_of_financing.cost_of_financing import execute as cost_of_financing
from treasury.bank_facilities.report.facility_repayment_schedule_report.facility_repayment_schedule_report import (
	execute as schedule_report,
)
from treasury.bank_facilities.report.facility_utilisation.facility_utilisation import (
	execute as utilisation_report,
)
from treasury.bank_facilities.report_utils import display_status
from treasury.pdc import lifecycle
from treasury.security_cheques import api as security_api
from treasury.tests.utils import (
	COMPANY,
	FACILITY_PE,
	LENDER,
	PE_SETTINGS,
	TreasuryTestMixin,
	ensure_alert_bank,
	ensure_facility_accounts,
	ensure_islamic_accounts,
	ensure_lender,
	make_bank_facility,
	make_drawdown,
	make_finance_facility,
	make_installment_batch,
	make_letter_of_credit,
	make_own_bank,
	make_repayment,
	set_bank_suppliers,
	unique_cheque_no,
)

D = getdate
T = "2025-07-01"
YEAR = {"company": COMPANY, "from_date": "2025-01-01", "to_date": "2025-12-31"}
SCHEDULE = "Facility Repayment Schedule"


class UX7gTestCase(TreasuryTestMixin, IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.accounts = ensure_facility_accounts()
		cls.islamic = ensure_islamic_accounts()
		ensure_lender()

	def setUp(self):
		super().setUp()
		self.use_company_settings(
			accrue_facility_interest=0, auto_debit_posting="On Due Date", **PE_SETTINGS, **FACILITY_PE
		)
		set_bank_suppliers(self, [])
		patcher = patch("treasury.pdc.report_utils.today", return_value=T)
		patcher.start()
		self.addCleanup(patcher.stop)

	@staticmethod
	def onload(doc, key):
		"""The `__onload` value `key` of the stored document, as the form gets it."""
		doc = frappe.get_doc(doc.doctype, doc.name)
		doc.run_method("onload")
		return doc.get_onload(key)

	def cheque_drawdown(self, installments=(1, 2), **values):
		"""A PDC to Lender drawdown whose first installments have delivered cheques."""
		bank = ensure_alert_bank()
		facility = make_bank_facility(bank_account=bank.bank_account)
		drawdown = make_drawdown(
			facility, bank_account=bank.bank_account, repayment_mode="PDC to Lender", **values
		)
		batch = make_installment_batch(drawdown, submit=False)
		batch.set("cheques", [r for r in batch.cheques if r.installment_no in installments])
		batch.save()
		batch.submit()
		return facility, drawdown

	@staticmethod
	def row(drawdown, installment_no):
		return frappe.db.get_value(
			SCHEDULE,
			{"parent": drawdown.name, "installment_no": installment_no},
			["name", "pdc_cheque"],
			as_dict=True,
		)


class TestFormOnload(UX7gTestCase):
	def test_drawdown_installment_info(self):
		_facility, drawdown = self.cheque_drawdown()
		info = self.onload(drawdown, "installment_info")
		one, two = self.row(drawdown, 1), self.row(drawdown, 2)
		self.assertEqual(info["auto_debit_posting"], "On Due Date")
		self.assertEqual(info["live_statuses"], ["Created", "Delivered"])
		self.assertEqual(set(info["cheques"]), {one.name, two.name})
		self.assertEqual(info["cheques"][one.name]["name"], one.pdc_cheque)
		self.assertEqual(info["cheques"][one.name]["operational_status"], "Delivered")
		self.assertTrue(info["cheques"][one.name]["cheque_no"])
		self.assertTrue(info["has_cheques"])
		self.assertIn("cheque_book", info)

		# the cheque of installment 1 is posted (the installment is paid): still listed, for its status dot
		lifecycle.post_party(one.pdc_cheque)
		info = self.onload(drawdown, "installment_info")
		self.assertIn(one.name, info["cheques"])

		manual = make_drawdown(make_bank_facility())
		info = self.onload(manual, "installment_info")
		self.assertEqual((info["cheques"], info["has_cheques"], info["cheque_book"]), ({}, False, None))

	def test_pdc_cheque_facility_installment(self):
		_facility, drawdown = self.cheque_drawdown(installments=(1,))
		name = self.row(drawdown, 1).pdc_cheque
		cheque = frappe.get_doc("PDC Cheque", name)
		info = self.onload(cheque, "facility_installment")
		self.assertEqual(info["installment_no"], 1)
		self.assertEqual(info["lender_name"], drawdown.lender_name)
		self.assertIsNone(info["payment_entry"])

		lifecycle.post_party(name)
		cheque.reload()
		info = self.onload(cheque, "facility_installment")
		self.assertEqual(info["payment_entry"], cheque.party_payment_entry)
		self.assertTrue(info["payment_entry"])

		# the repayment of a cheque says which cheque (its number) and the drawdown's mode
		repayment = frappe.get_doc("Facility Repayment", cheque.facility_repayment)
		settlement = self.onload(repayment, "settlement_info")
		self.assertEqual(
			(settlement["cheque_no"], settlement["repayment_mode"]), (cheque.cheque_no, "PDC to Lender")
		)
		self.assertFalse(settlement["transaction_date"])

		# a drawdown not created by a Letter of Credit has no settlement date
		plain = make_drawdown(make_bank_facility())
		self.assertIsNone(self.onload(plain, "letter_of_credit_settlement"))

	def test_security_summary(self):
		facility = make_finance_facility(expiry_date="2030-12-31", total_limit=750_000)
		self.assertIsNone(self.onload(facility, "security_summary"))
		data = security_api.make_security_cheque("Bank Facility", facility.name)
		values = {k: v for k, v in data.items() if not k.startswith("__") and k != "name"}
		values.update(cheque_no=unique_cheque_no(), cheque_book=None)
		frappe.get_doc(values).insert()
		summary = self.onload(facility, "security_summary")
		self.assertEqual((summary["count"], summary["total"], summary["currency"]), (1, 750_000, "EGP"))

	def test_trust_receipt_defaults_and_summary(self):
		bank = make_own_bank()
		facility = make_bank_facility(
			bank_account=bank.bank_account,
			limits=[
				{"limit_type": "Letter of Credit", "limit_amount": 500_000, "cash_margin_percent": 10},
				{
					"limit_type": "Trust Receipt",
					"limit_amount": 400_000,
					"interest_rate": 11,
					"default_tenor_months": 3,
					"liability_account": self.islamic.trust_receipt,
				},
			],
		)
		defaults = trust_receipt.limit_defaults(facility.name)
		self.assertEqual(
			(defaults["default_tenor_months"], defaults["interest_rate"], defaults["available"]),
			(3, 11, 400_000),
		)
		self.assertEqual(defaults["is_islamic"], 0)

		credit = make_letter_of_credit(facility, amount=100_000, bank_account=bank.bank_account)
		credit.receive_documents(documents_date="2025-03-01")
		credit.reload()
		# the settle dialog of a credit with documents received gets the defaults
		self.assertEqual(self.onload(credit, "trust_receipt_limit")["default_tenor_months"], 3)
		result = credit.settle(settlement_date="2025-03-10", finance_by_trust_receipt=1)
		credit.reload()
		summary = self.onload(credit, "trust_receipt")
		self.assertEqual(summary["name"], result["trust_receipt_drawdown"])
		self.assertEqual((summary["amount"], summary["repayment_status"]), (90_000, "Open"))
		self.assertEqual(D(summary["due_date"]), D("2025-06-10"))
		# a settled credit no longer offers the settle defaults
		self.assertIsNone(self.onload(credit, "trust_receipt_limit"))
		drawdown = frappe.get_doc("Facility Drawdown", result["trust_receipt_drawdown"])
		self.assertEqual(D(self.onload(drawdown, "letter_of_credit_settlement")), D("2025-03-10"))


class TestDisplayStatus(UX7gTestCase):
	def test_display_status_rules(self):
		def row(status, due, **values):
			return frappe._dict(status=status, due_date=due, **values)

		as_of = D(T)
		past, future = "2025-06-30", "2025-07-31"
		due_date, statement = "On Due Date", "On Bank Statement Match"
		auto = {"repayment_mode": "Auto Debit"}
		cases = [
			(row("Paid", past, cheque_status="Delivered"), due_date, "Paid"),
			(row("Pending", future, cheque_status="Delivered"), due_date, "Cheque Issued"),
			(row("Pending", future, cheque_status="Created"), due_date, "Cheque Issued"),
			(row("Overdue", past, cheque_status="Delivered"), due_date, "Cheque Due"),
			# a bounced cheque no longer pays the installment
			(row("Overdue", past, cheque_status="Bounced"), due_date, "Overdue"),
			(row("Overdue", past, **auto), statement, "Awaiting Statement"),
			(row("Pending", T, **auto), statement, "Awaiting Statement"),
			(row("Pending", T, **auto), due_date, "Posting Today"),
			(row("Overdue", past, **auto), due_date, "Overdue"),
			(row("Pending", future, **auto), statement, "Pending"),
			(row("Overdue", past, repayment_mode="Manual"), statement, "Overdue"),
			(row("Partially Paid", future), due_date, "Partially Paid"),
		]
		for data, mode, expected in cases:
			with self.subTest(row=data, mode=mode):
				self.assertEqual(display_status(data, as_of, mode), expected)

	def test_schedule_report_states_and_cheque_card(self):
		_facility, drawdown = self.cheque_drawdown(drawdown_date="2025-06-30")
		filters = {"company": COMPANY, "from_date": T, "to_date": "2025-12-31", "drawdown": drawdown.name}
		columns, data, _m, _c, summary = schedule_report(filters)
		states = {r.installment_no: r.display_status for r in data}
		self.assertEqual((states[1], states[2], states[3]), ("Cheque Issued", "Cheque Issued", "Pending"))
		self.assertEqual(data[0].status_label, "Cheque Issued")
		fieldnames = [c["fieldname"] for c in columns]
		self.assertLess(fieldnames.index("lender_name"), fieldnames.index("limit_type_label"))
		self.assertEqual(next(c for c in columns if c["fieldname"] == "bank").get("hidden"), 1)
		cards = {c["label"]: c["value"] for c in summary}
		covered = flt(sum(r.unpaid for r in data if r.installment_no in (1, 2)), 2)
		self.assertEqual(cards["Covered by Cheques"], covered)
		self.assertGreater(covered, 0)

	def test_auto_debit_awaiting_statement(self):
		def card():
			return dashboard.get_facility_card({"company": COMPANY, "scope": "awaiting_statement"})["value"]

		facility = make_bank_facility()
		self.use_company_settings(auto_debit_posting="On Bank Statement Match")
		before = card()
		drawdown = make_drawdown(facility, repayment_mode="Auto Debit", drawdown_date="2025-05-31")
		filters = {**YEAR, "drawdown": drawdown.name, "from_date": T}
		_c, data, *_r = schedule_report(filters)
		first = next(r for r in data if r.installment_no == 1)
		self.assertEqual((first.status, first.display_status), ("Overdue", "Awaiting Statement"))
		self.assertEqual(card() - before, 1)
		card_data = dashboard.get_facility_card({"company": COMPANY, "scope": "awaiting_statement"})
		self.assertEqual(card_data["route_options"]["repayment_mode"], "Auto Debit")

		# posted on the due date instead: a plain Overdue, not waiting for the statement
		self.use_company_settings(auto_debit_posting="On Due Date")
		_c, data, *_r = schedule_report(filters)
		first = next(r for r in data if r.installment_no == 1)
		self.assertEqual(first.display_status, "Overdue")
		self.assertEqual(card(), 0)
		self.assertRaises(
			frappe.ValidationError, dashboard.get_facility_card, {"company": COMPANY, "scope": "awaiting"}
		)


class TestReports7gUX(UX7gTestCase):
	def test_cost_of_financing_drops_zero_categories(self):
		conventional = make_bank_facility()
		drawdown = make_drawdown(conventional, upfront_fees=1200, fees_account=self.accounts.fees)
		make_repayment(drawdown, "2025-02-28")
		columns, data, _m, chart, summary = cost_of_financing({**YEAR, "facility": conventional.name})
		fieldnames = [c["fieldname"] for c in columns]
		for fieldname in ("interest", "fees", "total", "lender_name", "lender_type"):
			self.assertIn(fieldname, fieldnames)
		for fieldname in ("profit", "ijarah", "commission", "penalty", "charity"):
			self.assertNotIn(fieldname, fieldnames)
		self.assertEqual(data[0].lender_type, "Bank")
		# the dropped categories stay on the rows (0)
		self.assertEqual((data[0].profit, data[0].interest, data[0].fees), (0, 1104.66, 1200))
		self.assertEqual([c["label"] for c in summary], ["Interest", "Fees", "Total Cost of Financing"])
		self.assertEqual(summary[-1]["value"], flt(1104.66 + 1200, 2))
		# P21: a stacked bar per period, a dataset per category shown
		self.assertEqual(chart["type"], "bar")
		self.assertEqual(chart["barOptions"], {"stacked": 1})
		self.assertEqual([d["name"] for d in chart["data"]["datasets"]], ["Interest", "Fees"])
		self.assertEqual(len(chart["data"]["labels"]), 2)  # January (fees), February (interest)
		self.assertEqual([sum(d["values"]) for d in chart["data"]["datasets"]], [1104.66, 1200])

		# an Islamic facility shows Profit, not Interest
		murabaha = make_finance_facility()
		make_repayment(make_drawdown(murabaha, limit_type="Murabaha"), "2025-02-28")
		columns, data, *_rest = cost_of_financing({**YEAR, "facility": murabaha.name})
		fieldnames = [c["fieldname"] for c in columns]
		self.assertIn("profit", fieldnames)
		self.assertNotIn("interest", fieldnames)
		self.assertEqual(data[0].lender_type, "Finance Company")

		# no row at all: every category, no chart
		columns, data, _m, chart, _s = cost_of_financing({**YEAR, "facility": make_bank_facility().name})
		self.assertEqual((data, chart), ([], None))
		self.assertIn("charity", [c["fieldname"] for c in columns])

	def test_utilisation_by_lender_and_commitment(self):
		bank = make_own_bank()
		facility = make_bank_facility(
			bank_account=bank.bank_account,
			limits=[
				{
					"limit_type": "Istisna'a",
					"limit_amount": 500_000,
					"interest_rate": 10,
					"liability_account": self.islamic.istisna,
				}
			],
		)
		drawdown = make_drawdown(
			facility,
			amount=300_000,
			limit_type="Istisna'a",
			bank_account=bank.bank_account,
			disbursement_stages=[
				{"stage_date": "2025-02-15", "amount": 100_000},
				{"stage_date": "2025-05-15", "amount": 200_000},
			],
		)
		drawdown.disburse_stage(stage=drawdown.disbursement_stages[0].name, disbursement_date="2025-02-15")
		columns, data, *_rest = utilisation_report({"company": COMPANY, "facility": facility.name})
		self.assertEqual(columns[0]["label"], "Lender / Facility / Limit Type")
		top, _facility_row, limit = data
		facility.reload()
		self.assertEqual((top.label, top.lender_name), (facility.lender_name, facility.lender_name))
		self.assertEqual(top.row_id, f"lender-{facility.lender_name}")
		self.assertEqual(
			(limit.limit_type, limit.utilised, limit.commitment), ("Istisna'a", 300_000, 200_000)
		)
		self.assertIn("commitment", [c["fieldname"] for c in columns])

		# a finance company facility is grouped under its lender too
		finance = make_finance_facility()
		_c, data, *_r = utilisation_report({"company": COMPANY, "facility": finance.name})
		self.assertEqual(data[0].label, frappe.db.get_value("Supplier", LENDER, "supplier_name"))
		self.assertEqual(data[2].commitment, 0)


class TestConnectionsAndCustomField(UX7gTestCase):
	def test_dashboards(self):
		facility = frappe.get_meta("Bank Facility").get_dashboard_data()
		items = [item for group in facility.transactions for item in group["items"]]
		self.assertIn("Invoice Discounting", items)
		self.assertEqual(facility.non_standard_fieldnames["Invoice Discounting"], "treasury_facility")

		drawdown = frappe.get_meta("Facility Drawdown").get_dashboard_data()
		groups = {group["label"]: group["items"] for group in drawdown.transactions}
		self.assertEqual(groups["Repayments"], ["Facility Repayment"])
		self.assertEqual(groups["Letters of Credit"], ["Letter of Credit"])
		self.assertEqual(groups["Cheques"], ["PDC Cheque", "Cheque Batch"])

	def test_invoice_discounting_link_filters(self):
		from treasury.install import get_custom_fields

		field = get_custom_fields()["Invoice Discounting"][0]
		self.assertEqual(
			json.loads(field["link_filters"]),
			[
				["Bank Facility", "company", "=", "eval:doc.company"],
				["Bank Facility", "status", "=", "Active"],
			],
		)
