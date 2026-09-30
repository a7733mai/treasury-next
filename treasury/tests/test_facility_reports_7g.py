"""Phase 7g (spec §7 F): the facility reports with lenders, Islamic products and Payment Entry vouchers — the
Cost of Financing categories, the repayment schedule columns and filters, the utilisation report's lender
columns and the facility fees in the Bank Charges Analysis.

The reports' "today" is frozen to 2025-07-01 (`treasury.pdc.report_utils.today` is patched).
"""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import getdate

from treasury.bank_facilities.report.cost_of_financing.cost_of_financing import execute as cost_of_financing
from treasury.bank_facilities.report.facility_repayment_schedule_report.facility_repayment_schedule_report import (
	execute as schedule_report,
)
from treasury.bank_facilities.report.facility_utilisation.facility_utilisation import (
	execute as utilisation_report,
)
from treasury.bank_transfers.report.bank_charges_analysis.bank_charges_analysis import (
	execute as bank_charges_analysis,
)
from treasury.pdc import lifecycle
from treasury.tests.utils import (
	COMPANY,
	FACILITY_JE,
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
	make_own_bank,
	make_repayment,
	set_bank_suppliers,
)

D = getdate
YEAR = {"company": COMPANY, "from_date": "2025-01-01", "to_date": "2025-12-31"}


class Reports7gTestCase(TreasuryTestMixin, IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.accounts = ensure_facility_accounts()
		cls.islamic = ensure_islamic_accounts()
		ensure_lender()

	def setUp(self):
		super().setUp()
		self.use_company_settings(accrue_facility_interest=0, **PE_SETTINGS, **FACILITY_PE)
		set_bank_suppliers(self, [])
		patcher = patch("treasury.pdc.report_utils.today", return_value="2025-07-01")
		patcher.start()
		self.addCleanup(patcher.stop)

	@staticmethod
	def costs(facility) -> frappe._dict:
		_columns, data, *_rest = cost_of_financing({**YEAR, "facility": facility.name})
		return data[0] if data else frappe._dict()

	def murabaha_facility(self, **values):
		return make_finance_facility(**values)


class TestFacilityReports7g(Reports7gTestCase):
	def test_cost_of_financing_categories(self):
		categories = ("interest", "profit", "ijarah", "commission", "fees", "penalty", "charity")

		def row(facility):
			costs = self.costs(facility)
			return {c: costs.get(c, 0) for c in categories}

		# conventional: interest, fees
		conventional = make_bank_facility()
		dd = make_drawdown(conventional, upfront_fees=1200, fees_account=self.accounts.fees)
		make_repayment(dd, "2025-02-28")
		self.assertEqual(
			row(conventional), {**dict.fromkeys(categories, 0), "interest": 1104.66, "fees": 1200}
		)

		# Islamic Murabaha (rate): profit
		murabaha = self.murabaha_facility()
		make_repayment(make_drawdown(murabaha, limit_type="Murabaha"), "2025-02-28")
		self.assertEqual(row(murabaha), {**dict.fromkeys(categories, 0), "profit": 1104.66})

		# Ijarah: the profit part of the rentals
		ijarah = make_finance_facility(
			limits=[
				{
					"limit_type": "Ijarah",
					"limit_amount": 500_000,
					"interest_rate": 0,
					"liability_account": self.islamic.ijarah,
				}
			]
		)
		dd = make_drawdown(ijarah, limit_type="Ijarah", amount=240_000, tenor_months=24, profit_amount=24_000)
		make_repayment(dd, "2025-02-28")
		self.assertEqual(row(ijarah), {**dict.fromkeys(categories, 0), "ijarah": 1000})

		# late payment payable to charity
		charity = self.murabaha_facility(
			late_payment_mode="Payable to Charity",
			late_payment_charity_account=self.islamic.charity,
			charity_payable_account=self.islamic.charity_payable,
		)
		dd = make_drawdown(charity, limit_type="Murabaha")
		make_repayment(dd, "2025-03-05", allocation_mode="Selected Row", installment_no=1, penalty=500)
		self.assertEqual(row(charity), {**dict.fromkeys(categories, 0), "profit": 1104.66, "charity": 500})

		# an installment cheque's interest, and its bounce reversal netting it to 0
		bank = ensure_alert_bank()
		cheques = make_bank_facility(bank_account=bank.bank_account)
		dd = make_drawdown(cheques, bank_account=bank.bank_account, repayment_mode="PDC to Lender")
		batch = make_installment_batch(dd, submit=False)
		batch.set("cheques", [r for r in batch.cheques if r.installment_no in (1, 2)])
		batch.save()
		batch.submit()
		names = frappe.get_all(
			"PDC Cheque",
			filters={"cheque_batch": batch.name},
			order_by="original_due_date asc",
			pluck="name",
		)
		for name in names:
			lifecycle.post_party(name)
		self.assertEqual(row(cheques)["interest"], 2225.76)  # 1,104.66 + 1,121.10
		frappe.get_doc("PDC Cheque", names[1]).bounce(bounce_date="2025-04-02", reason="Stopped")
		self.assertEqual(row(cheques)["interest"], 1104.66)

	def test_repayment_schedule_columns(self):
		bank = ensure_alert_bank()
		facility = make_bank_facility(bank_account=bank.bank_account)
		cheques = make_drawdown(facility, bank_account=bank.bank_account, repayment_mode="PDC to Lender")
		batch = make_installment_batch(cheques, submit=False)
		batch.set("cheques", [r for r in batch.cheques if r.installment_no in (1, 2)])
		batch.save()
		batch.submit()
		first = frappe.db.get_value(
			"Facility Repayment Schedule", {"parent": cheques.name, "installment_no": 1}, "pdc_cheque"
		)
		lifecycle.post_party(first)
		pe = frappe.db.get_value("PDC Cheque", first, "party_payment_entry")

		self.use_company_settings(**FACILITY_JE)
		manual = make_drawdown(facility, amount=12_000)
		repayment = make_repayment(manual, "2025-02-28")

		filters = {**YEAR, "include_paid": 1, "include_overdue": 1}
		columns, data, *_rest = schedule_report({**filters, "facility": facility.name})
		fieldnames = [c["fieldname"] for c in columns]
		for fieldname in ("repayment_mode_label", "pdc_cheque", "cheque_status_label", "payment_journal"):
			self.assertIn(fieldname, fieldnames)
		voucher = next(c for c in columns if c["fieldname"] == "payment_journal")
		self.assertEqual((voucher["fieldtype"], voucher["options"]), ("Dynamic Link", "payment_voucher_type"))
		rows = {(r.drawdown, r.installment_no): r for r in data}
		one = rows[(cheques.name, 1)]
		self.assertEqual(
			(one.repayment_mode, one.pdc_cheque, one.cheque_status),
			("PDC to Lender", first, "Delivered"),
		)
		self.assertEqual((one.payment_voucher_type, one.payment_journal), ("Payment Entry", pe))
		self.assertEqual(rows[(cheques.name, 3)].pdc_cheque, None)
		self.assertEqual(
			(rows[(manual.name, 1)].payment_voucher_type, rows[(manual.name, 1)].payment_journal),
			("Journal Entry", repayment.journal_entry),
		)

		# filters
		filters["facility"] = facility.name
		_c, data, *_r = schedule_report({**filters, "repayment_mode": "Manual"})
		self.assertEqual({r.drawdown for r in data}, {manual.name})
		_c, data, *_r = schedule_report({**filters, "lender_type": "Finance Company"})
		self.assertEqual(data, [])

	def test_utilisation_report_lender_columns(self):
		finance = make_finance_facility()
		make_drawdown(finance, limit_type="Murabaha", amount=50_000)
		bank = make_bank_facility()
		columns, data, *_rest = utilisation_report({"company": COMPANY, "facility": finance.name})
		fieldnames = [c["fieldname"] for c in columns]
		self.assertIn("lender_type", fieldnames)
		self.assertIn("lender_name", fieldnames)
		lender_name = frappe.db.get_value("Supplier", LENDER, "supplier_name")
		top, facility_row, *_limits = data
		self.assertEqual((top.label, top.lender_name), (lender_name, lender_name))
		self.assertEqual(
			(facility_row.lender_type, facility_row.lender_name), ("Finance Company", lender_name)
		)
		self.assertEqual(facility_row.utilised, 50_000)

		_columns, data, *_rest = utilisation_report({"company": COMPANY, "lender_type": "Finance Company"})
		facilities = {r.get("facility") for r in data if r.get("indent") == 1}
		self.assertIn(finance.name, facilities)
		self.assertNotIn(bank.name, facilities)

	def test_charge_sources_facility_fees_pe(self):
		own = make_own_bank()
		facility = make_bank_facility(bank_account=own.bank_account)
		drawdown = make_drawdown(
			facility,
			bank_account=own.bank_account,
			drawdown_date="2025-03-10",
			upfront_fees=1200,
			fees_account=self.accounts.fees,
		)
		drawdown.reload()
		self.assertTrue(drawdown.payment_entry)
		_columns, data, *_rest = bank_charges_analysis(
			{
				"company": COMPANY,
				"from_date": "2025-03-01",
				"to_date": "2025-03-31",
				"view": "Detail",
				"source": "Facility Fees",
				"bank_account": own.bank_account,
			}
		)
		self.assertEqual(
			[(r.voucher_type, r.voucher_no, r.amount) for r in data],
			[("Payment Entry", drawdown.payment_entry, 1200)],
		)
