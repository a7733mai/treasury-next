"""Phase 7b: Bank Transfer Register and Transfer Fees Analysis (spec 7b §3.13, §7 H)."""

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, flt, getdate, today

from treasury.bank_transfers.report.bank_transfer_register.bank_transfer_register import (
	execute as register,
)
from treasury.bank_transfers.report.transfer_fees_analysis.transfer_fees_analysis import (
	execute as fees_analysis,
)
from treasury.tests.utils import (
	BANK,
	COMPANY,
	CUSTOMER,
	OTHER_ABBR,
	OTHER_COMPANY,
	SUPPLIER,
	TRANSFER_SETTINGS,
	TreasuryTestMixin,
	add_user_permission,
	ensure_company,
	ensure_second_egp_bank,
	ensure_user,
	make_bank_transfer,
	set_fee_rules,
	standard_fee_rules,
)

OTHER_COMPANY_USER = "treasury-transfer-report-other@example.com"
# Transfers persist for the whole class: every test works in a 50-day window of its own (days before today,
# inside the fiscal years ensure_company creates); the reports read days 0-40 of it.
WINDOWS = {
	"test_register_filters": -300,
	"test_register_summary": -250,
	"test_register_user_permission": -200,
	"test_fees_analysis": -150,
	"test_refunded_fee_is_not_counted": -100,
}


class TestBankTransferReports(TreasuryTestMixin, IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_company(OTHER_COMPANY, OTHER_ABBR)
		ensure_user(OTHER_COMPANY_USER, ["Treasury User"])
		add_user_permission(OTHER_COMPANY_USER, "Company", OTHER_COMPANY)

	def setUp(self):
		super().setUp()
		self.use_company_settings(**TRANSFER_SETTINGS)
		set_fee_rules(self, standard_fee_rules())
		self.base = getdate(add_days(today(), WINDOWS[self._testMethodName]))

	def day(self, offset: int):
		return getdate(add_days(self.base, offset))

	def filters(self, **values):
		return {"company": COMPANY, "from_date": self.day(0), "to_date": self.day(40), **values}

	def names(self, filters) -> list[str]:
		_columns, data, *_rest = register(filters)
		return [r.bank_transfer for r in data]

	# ------------------------------------------------------------------ register

	def test_register_filters(self):
		second = ensure_second_egp_bank()
		payment = make_bank_transfer(amount=20000, execution_date=self.day(1))
		fawran = make_bank_transfer(method="Fawran", amount=500, execution_date=self.day(2))
		receipt = make_bank_transfer(transfer_type="Party Receipt", amount=1000, execution_date=self.day(3))
		internal = make_bank_transfer(
			transfer_type="Internal Transfer",
			amount=700,
			bank_account=second.bank_account,
			to_bank_account=self.data.egp.bank_account,
			execution_date=self.day(4),
		)
		draft = make_bank_transfer(submit=False, amount=300, execution_date=self.day(5))
		cancelled = make_bank_transfer(amount=400, execution_date=self.day(6))
		cancelled.cancel()
		outside = make_bank_transfer(amount=600, execution_date=self.day(45))  # before the next window

		everything = self.names(self.filters())
		for name in (payment.name, fawran.name, receipt.name, internal.name, draft.name):
			self.assertIn(name, everything)
		self.assertNotIn(cancelled.name, everything)
		self.assertNotIn(outside.name, everything)
		self.assertEqual(self.names(self.filters(status="Cancelled")), [cancelled.name])

		# a bank account matches the target of an internal transfer too
		egp_names = self.names(self.filters(bank_account=self.data.egp.bank_account))
		self.assertIn(internal.name, egp_names)
		self.assertEqual(self.names(self.filters(bank_account=second.bank_account)), [internal.name])
		self.assertEqual(self.names(self.filters(method="Fawran")), [fawran.name])
		self.assertEqual(self.names(self.filters(transfer_type="Party Receipt")), [receipt.name])
		self.assertEqual(self.names(self.filters(status="Draft")), [draft.name])
		self.assertEqual(self.names(self.filters(party_type="Customer", party=CUSTOMER)), [receipt.name])
		self.assertNotIn(receipt.name, self.names(self.filters(party_type="Supplier", party=SUPPLIER)))

		filters = self.filters(method="Normal", transfer_type="Party Payment")
		columns, data, _message, _chart, summary = register(filters)
		self.assertIn("payment_entry", [c["fieldname"] for c in columns])
		row = next(r for r in data if r.bank_transfer == payment.name)
		self.assertEqual(row.payment_entry, payment.payment_entry)
		self.assertEqual((flt(row.fee_amount), flt(row.bank_amount)), (30, 20030))
		self.assertEqual([c["value"] for c in summary][:1], [len(data)])

	def test_register_summary(self):
		make_bank_transfer(amount=20000, execution_date=self.day(1))  # Sent, fee 30
		make_bank_transfer(amount=5000, execution_date=self.day(2)).mark_completed(completion_date=self.day(3))
		make_bank_transfer(amount=100, execution_date=add_days(today(), 20))  # Scheduled: outside the range
		_columns, data, _message, _chart, summary = register(self.filters())
		values = [flt(c["value"], 2) for c in summary]
		self.assertEqual(values, [len(data), 25000, 45])  # fees 30 + 15

	def test_register_user_permission(self):
		make_bank_transfer(amount=500, execution_date=self.day(1))
		with self.set_user(OTHER_COMPANY_USER):
			self.assertRaises(frappe.PermissionError, register, self.filters())

	# ------------------------------------------------------------------ fees analysis

	def test_fees_analysis(self):
		usd = self.data.usd
		make_bank_transfer(amount=20000, execution_date=self.day(1))  # Normal, fee 30
		make_bank_transfer(amount=100000, execution_date=self.day(2))  # Normal, fee 50
		make_bank_transfer(method="Fawran", amount=800, execution_date=self.day(35))  # Fawran, fee 0.5
		make_bank_transfer(amount=100, bank_account=usd.bank_account, execution_date=self.day(3))  # 5 USD = 250
		make_bank_transfer(submit=False, amount=20000, execution_date=self.day(4))  # draft: excluded
		cancelled = make_bank_transfer(amount=20000, execution_date=self.day(5))
		cancelled.cancel()

		rows = self.groups("Method")
		self.assertEqual(
			{r.group: (r.transfers, r.amount, r.fees) for r in rows},
			{"Normal": (3, 20000 + 100000 + 5000, 30 + 50 + 250), "Fawran": (1, 800, 0.5)},
		)
		normal = next(r for r in rows if r.group == "Normal")
		self.assertEqual(normal.average_fee, flt(330 / 3, 2))
		self.assertEqual(normal.fee_percent, flt(330 / 125000 * 100, 2))
		self.assertEqual([r.group for r in rows], ["Normal", "Fawran"])  # by fees, highest first

		months = self.groups("Month")
		self.assertEqual([r.group for r in months], sorted(r.group for r in months))
		self.assertEqual(sum(r.transfers for r in months), 4)

		accounts = self.groups("Bank Account")
		self.assertEqual({r.group for r in accounts}, {self.data.egp.bank_account, usd.bank_account})
		banks = self.groups("Bank")
		self.assertEqual([(r.group, r.transfers) for r in banks], [(BANK, 4)])

		_columns, data, _message, chart = fees_analysis(self.fee_filters("Method"))
		total = data[-1]
		self.assertEqual((total.transfers, flt(total.fees, 2)), (4, 330.5))
		self.assertEqual(chart["data"]["labels"], ["Normal", "Fawran"])
		self.assertEqual(chart["data"]["datasets"][0]["values"], [330, 0.5])

	def test_refunded_fee_is_not_counted(self):
		self.use_company_settings(refund_fees_on_return=1)
		transfer = make_bank_transfer(amount=20000, execution_date=self.day(1))
		transfer.mark_returned(return_date=self.day(2), reason="Returned")
		kept = make_bank_transfer(amount=20000, execution_date=self.day(3))
		self.use_company_settings(refund_fees_on_return=0)
		kept.mark_returned(return_date=self.day(4), reason="Returned")
		rows = self.groups("Method")
		self.assertEqual([(r.group, r.transfers, r.fees) for r in rows], [("Normal", 2, 30)])

	def fee_filters(self, group_by):
		return {"company": COMPANY, "from_date": self.day(0), "to_date": self.day(40), "group_by": group_by}

	def groups(self, group_by) -> list:
		_columns, data, *_rest = fees_analysis(self.fee_filters(group_by))
		return [r for r in data if not r.get("is_total")]
