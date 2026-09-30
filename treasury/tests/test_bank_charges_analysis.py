"""Phase 7d: the Bank Charges Analysis report (spec 7d §3.7, §7 D).

One fixture for the class, in the window day(300) - day(330) of the test company (EGP):

| Source                     | Vouchers                                                          | Net   |
|----------------------------|-------------------------------------------------------------------|-------|
| Bank Charges               | C1 150 (Account Maintenance), C2 USD 20 @ 50 (SMS Alerts), R1 -50 | 1,100 |
| Transfer Fees              | T1 fee 30; T2 fee 20 returned with the fee refunded (+20, -20)    | 30    |
| Cheque Clearance Charges   | Payment Entry mode 15; Journal Entry mode 10                      | 25    |
| Bounce Charges Recovered   | 75 recovered from the customer                                    | -75   |
| LG Commissions             | 500                                                               | 500   |
| LC Commissions and Charges | 300 + amendment 100 + documents 50                                | 450   |
| Facility Fees              | upfront fees 1,000                                                | 1,000 |

Every test runs in a savepoint rolled back after it, so the data a test adds never reaches another test.
"""

from types import SimpleNamespace

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import flt, getdate

from treasury.accounting import posting
from treasury.bank_transfers.charge_sources import SOURCES
from treasury.bank_transfers.report.bank_charges_analysis.bank_charges_analysis import (
	SOURCE_COLORS,
	execute,
)
from treasury.tests.utils import (
	BANK,
	BANK_CHARGE_SETTINGS,
	COMPANY,
	DEFAULT_PDC_SETTINGS,
	OTHER_ABBR,
	OTHER_BANK,
	OTHER_COMPANY,
	TRANSFER_SETTINGS,
	TreasuryTestMixin,
	acc,
	add_user_permission,
	bank_account_name,
	day,
	ensure_charge_category,
	ensure_company,
	ensure_facility_accounts,
	ensure_instrument_accounts,
	ensure_other_company_bank,
	ensure_user,
	make_bank_charge,
	make_bank_facility,
	make_bank_transfer,
	make_drawdown,
	make_letter_of_credit,
	make_letter_of_guarantee,
	make_own_bank,
	make_pdc_cheque,
	set_bank_suppliers,
	set_fee_rules,
	standard_fee_rules,
)

SAVEPOINT = "treasury_bank_charges_analysis"
FROM, TO = day(300), day(330)
CHARGES_ACCOUNT = "Treasury Bank Charges"
EXPECTED = {
	"Bank Charges": (1150, 50, 1100, 3),
	"Transfer Fees": (50, 20, 30, 3),
	"Cheque Clearance Charges": (25, 0, 25, 2),
	"Bounce Charges Recovered": (0, 75, -75, 1),
	"LG Commissions": (500, 0, 500, 1),
	"LC Commissions and Charges": (450, 0, 450, 3),
	"Facility Fees": (1000, 0, 1000, 1),
}
TREASURY_USER = "treasury-bca-user@example.com"
ACCOUNTS_USER = "treasury-bca-accounts@example.com"
OTHER_CO_USER = "treasury-bca-other-co@example.com"


def run(**filters):
	values = {"company": COMPANY, "from_date": FROM, "to_date": TO, **filters}
	return execute(frappe._dict(values))


def detail(**filters) -> list:
	return run(view="Detail", **filters)[1]


def ensure_named_category(testcase, name: str) -> str:
	"""A default category by name, enabled (created when the site has none)."""
	if not frappe.db.exists("Bank Charge Category", name):
		return ensure_charge_category(name, keywords=())
	if frappe.db.get_value("Bank Charge Category", name, "disabled"):
		frappe.db.set_value("Bank Charge Category", name, "disabled", 0)
		testcase.addClassCleanup(frappe.db.set_value, "Bank Charge Category", name, "disabled", 1)
	return name


class TestBankChargesAnalysis(TreasuryTestMixin, IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_user(TREASURY_USER, ["Treasury User"])
		ensure_user(ACCOUNTS_USER, ["Accounts User"])
		ensure_user(OTHER_CO_USER, ["Treasury User"])
		cls.build_fixture()

	@classmethod
	def set_settings(cls, **values):
		previous = frappe.db.get_value("Treasury Company Settings", COMPANY, list(values), as_dict=True)
		frappe.db.set_value("Treasury Company Settings", COMPANY, values)
		cls.addClassCleanup(frappe.db.set_value, "Treasury Company Settings", COMPANY, dict(previous))

	@classmethod
	def build_fixture(cls):
		shim = SimpleNamespace(addCleanup=cls.addClassCleanup)
		cls.set_settings(
			**DEFAULT_PDC_SETTINGS,
			**TRANSFER_SETTINGS,
			**BANK_CHARGE_SETTINGS,
			accrue_facility_interest=0,
		)
		cls.set_settings(bounce_charges_account=acc(CHARGES_ACCOUNT), refund_fees_on_return=1)
		set_fee_rules(shim, standard_fee_rules())
		set_bank_suppliers(shim, [])
		charges_account = acc(CHARGES_ACCOUNT)
		f = cls.fixture = frappe._dict()

		# Bank Charges
		maintenance = ensure_named_category(cls, "Account Maintenance")
		sms = ensure_named_category(cls, "SMS Alerts")
		f.c1 = make_bank_charge(category=maintenance, expense_account=charges_account, posting_date=day(301))
		f.c2 = make_bank_charge(
			category=sms,
			expense_account=charges_account,
			bank_account=bank_account_name("USD"),
			amount=20,
			posting_date=day(305),
		)
		f.r1 = make_bank_charge(
			entry_type="Refund",
			against_bank_charge=f.c1.name,
			category=maintenance,
			expense_account=charges_account,
			amount=50,
			posting_date=day(306),
		)

		# Transfer Fees: 20,000 -> fee 30; 10,000 -> fee 20, returned with the fee refunded
		f.t1 = make_bank_transfer(amount=20000, request_date=day(306), execution_date=day(306))
		f.t2 = make_bank_transfer(amount=10000, request_date=day(306), execution_date=day(306))
		f.t2.mark_returned(return_date=day(308), reason="Returned by the bank")
		f.t2.reload()

		# Cheque clearance charges: Payment Entry mode 15, Journal Entry mode 10
		cls.set_settings(bank_settlement_voucher="Payment Entry")
		f.pe_cheque = make_pdc_cheque(amount=1500, issue_date=day(300), due_date=day(305))
		f.pe_cheque.deliver(handover_date=day(301))
		f.pe_cheque.clear(clearance_date=day(306), bank_charges=15)
		f.pe_cheque.reload()
		cls.set_settings(bank_settlement_voucher="Journal Entry")
		f.je_cheque = make_pdc_cheque(amount=1600, issue_date=day(300), due_date=day(305))
		f.je_cheque.deliver(handover_date=day(301))
		f.je_cheque.clear(clearance_date=day(307), bank_charges=10)
		f.je_cheque.reload()

		# Bounce charges recovered from the customer
		f.bounced = make_pdc_cheque("Incoming", amount=2000, issue_date=day(300), due_date=day(310))
		f.bounced.receive(receipt_date=day(301))
		f.bounced.bounce(bounce_date=day(311), reason="Insufficient funds", charges=75)
		f.bounced.reload()

		# LG commission, LC commission / amendment / documents charges, facility upfront fees
		accounts = ensure_facility_accounts()
		inst = ensure_instrument_accounts()
		f.bank = make_own_bank()
		f.facility = make_bank_facility(
			total_limit=2_000_000,
			limits=[
				{"limit_type": "Short Term Loan", "limit_amount": 500_000, "interest_rate": 12},
				{
					"limit_type": "Letter of Guarantee",
					"limit_amount": 500_000,
					"liability_account": inst.lg_liability,
				},
				{
					"limit_type": "Letter of Credit",
					"limit_amount": 500_000,
					"cash_margin_percent": 20,
					"commission_rate": 0.5,
				},
			],
			bank_account=f.bank.bank_account,
		)
		f.lg = make_letter_of_guarantee(
			f.facility,
			amount=50_000,
			cash_margin_percent=0,
			commission_rate=0,
			commission_amount=500,
			issue_date=day(302),
			expiry_date=day(420),
		)
		f.lc = make_letter_of_credit(f.facility, amount=60_000, issue_date=day(301), expiry_date=day(400))
		f.lc.amend(amendment_date=day(303), new_expiry_date=day(410), charges=100)
		f.lc.receive_documents(documents_date=day(304), charges=50)
		f.lc.reload()
		f.drawdown = make_drawdown(
			f.facility,
			amount=100_000,
			drawdown_date=day(305),
			tenor_months=6,  # matures before the facility expires
			bank_account=f.bank.bank_account,
			upfront_fees=1000,
			fees_account=accounts.fees,
		)

	def setUp(self):
		super().setUp()
		frappe.db.savepoint(SAVEPOINT)
		self.addCleanup(frappe.db.rollback, save_point=SAVEPOINT)

	@staticmethod
	def group(data, label: str) -> frappe._dict:
		return next(row for row in data if row.get("group") == label)

	# ------------------------------------------------------------------ totals

	def test_totals_per_source(self):
		columns, data, _message, _chart, summary = run(group_by="Source")
		for source, (charges, refunds, net, vouchers) in EXPECTED.items():
			with self.subTest(source=source):
				row = self.group(data, source)
				self.assertEqual(
					(flt(row.charges), flt(row.refunds), flt(row.net), row.vouchers),
					(charges, refunds, net, vouchers),
				)
		total = data[-1]
		self.assertTrue(total.is_total)
		self.assertEqual(
			(flt(total.charges), flt(total.refunds), flt(total.net), total.vouchers), (3175, 145, 3030, 14)
		)
		self.assertEqual(flt(total.share), 100)
		self.assertEqual([flt(s["value"]) for s in summary[:4]], [3175, 145, 3030, 14])
		self.assertEqual(summary[2]["indicator"], "Red")
		self.assertIn("company_currency", [c["fieldname"] for c in columns])

	def test_top_category_and_source_cards(self):
		"""Design 7d P18: the largest net by category (other sources under their label) and by source."""
		summary = run(group_by="Month")[4]
		cards = {card["label"]: card for card in summary[4:]}
		self.assertEqual(set(cards), {"Top Category", "Top Source"})
		for label, group_by in (("Top Category", "Category"), ("Top Source", "Source")):
			with self.subTest(card=label):
				top = run(group_by=group_by)[1][0]
				self.assertEqual(cards[label]["datatype"], "Data")
				self.assertTrue(cards[label]["value"].startswith(f"{top.group} · "), cards[label]["value"])
				self.assertIn(f"({flt(top.share, 1)}%)", cards[label]["value"])
		# the Month chart has one colour per source, in the SOURCES order
		self.assertEqual(list(SOURCE_COLORS), list(SOURCES))
		chart = run(group_by="Month")[3]
		self.assertEqual(chart["colors"], [SOURCE_COLORS[s] for s in SOURCES if s in EXPECTED])
		self.assertEqual(len(chart["colors"]), len(chart["data"]["datasets"]))
		self.assertEqual(len(run(group_by="Bank")[3]["colors"]), 1)

	def test_group_by_month_bank_account_category(self):
		data = run(group_by="Category")[1]
		self.assertEqual(flt(self.group(data, "Account Maintenance").net), 100)
		self.assertEqual(flt(self.group(data, "SMS Alerts").net), 1000)
		for source in list(EXPECTED)[1:]:
			self.assertEqual(flt(self.group(data, source).net), EXPECTED[source][2], source)
		self.assertNotIn("Bank Charges", [row.get("group") for row in data])

		for group_by in ("Month", "Bank Account", "Bank"):
			with self.subTest(group_by=group_by):
				data = run(group_by=group_by)[1]
				self.assertEqual(flt(sum(flt(r.net) for r in data if not r.get("is_total")), 2), 3030)
				self.assertEqual(flt(data[-1].net), 3030)
		months = [row.group for row in run(group_by="Month")[1] if not row.get("is_total")]
		self.assertEqual(months, sorted(months))
		self.assertEqual(months, ["2026-04", "2026-05"])

	def test_detail_rows_link_vouchers(self):
		rows = detail()
		self.assertEqual(len(rows), 14)
		for row in rows:
			self.assertIn(row.voucher_type, ("Payment Entry", "Journal Entry"))
			self.assertTrue(frappe.db.exists(row.voucher_type, row.voucher_no))
			self.assertTrue(row.document and row.document_type and row.event)
		c1 = next(row for row in rows if row.document == self.fixture.c1.name)
		self.assertEqual(
			(c1.voucher_type, c1.voucher_no, flt(c1.amount), c1.category, c1.event),
			("Payment Entry", self.fixture.c1.payment_entry, 150, "Account Maintenance", "Bank Charge"),
		)
		self.assertEqual((c1.bank, c1.bank_account), (BANK, bank_account_name("EGP")))
		self.assertEqual(
			[getdate(r.posting_date) for r in rows], sorted(getdate(r.posting_date) for r in rows)
		)
		reversal = next(row for row in rows if row.document == self.fixture.t2.name and flt(row.amount) < 0)
		self.assertEqual((reversal.voucher_no, flt(reversal.amount)), (self.fixture.t2.reversal_journal, -20))

	# ------------------------------------------------------------------ exclusions and filters

	def test_exclusions(self):
		f = self.fixture
		cancelled = make_bank_charge(amount=77, posting_date=day(315))
		cancelled.cancel()
		outside = make_bank_charge(amount=78, posting_date=day(290))

		# a return that keeps the fee: the reversal books no expense (net 0)
		frappe.db.set_value("Treasury Company Settings", COMPANY, "refund_fees_on_return", 0)
		self.addCleanup(frappe.db.set_value, "Treasury Company Settings", COMPANY, "refund_fees_on_return", 1)
		kept = make_bank_transfer(amount=20000, request_date=day(316), execution_date=day(316))
		kept.mark_returned(return_date=day(317), reason="Returned, fee kept")
		kept.reload()

		# an Exchange Gain / Loss row: only the expense row counts
		usd_draft = make_bank_charge(submit=False, bank_account=bank_account_name("USD"), amount=20)
		egl_journal = posting.create_journal_entry(
			source_doctype="Bank Charge",
			source_name=usd_draft.name,
			event="bank_charge",
			company=COMPANY,
			posting_date=day(318),
			accounts=[
				{
					"account": acc(CHARGES_ACCOUNT),
					"debit_in_account_currency": 1000.01,
					"cost_center": frappe.get_cached_value("Company", COMPANY, "cost_center"),
				},
				{
					"account": self.data.usd.bank_gl,
					"credit_in_account_currency": 20,
					"exchange_rate": 50,
				},
			],
			cheque_no="EGL",
			cheque_date=day(318),
			voucher_type="Bank Entry",
			balance_exchange_difference=True,
		)
		egl_account = frappe.get_cached_value("Company", COMPANY, "exchange_gain_loss_account")
		self.assertTrue(
			frappe.db.exists(
				"GL Entry", {"voucher_no": egl_journal, "account": egl_account, "is_cancelled": 0}
			)
		)

		rows = detail()
		vouchers = {row.voucher_no: row for row in rows}
		documents = {row.document for row in rows}
		self.assertNotIn(cancelled.name, documents)
		self.assertNotIn(outside.name, documents)
		self.assertNotIn(f.lc.margin_journal, vouchers)
		self.assertNotIn(kept.reversal_journal, vouchers)
		self.assertIn(kept.payment_entry, vouchers)  # its fee stays booked
		self.assertEqual(flt(vouchers[egl_journal].amount), 1000.01)
		self.assertNotIn(f.drawdown.journal_entry, vouchers)  # the principal
		self.assertNotIn(f.bounced.party_payment_entry, vouchers)

	def test_filters(self):
		rows = detail(source="Transfer Fees")
		self.assertEqual({r.source_value for r in rows}, {"Transfer Fees"})
		self.assertEqual(flt(sum(r.amount for r in rows)), 30)

		rows = detail(category="Account Maintenance")
		self.assertEqual({r.document for r in rows}, {self.fixture.c1.name, self.fixture.r1.name})
		self.assertEqual(flt(sum(r.amount for r in rows)), 100)
		self.assertEqual(detail(category="Account Maintenance", source="Transfer Fees"), [])

		other = ensure_other_company_bank()
		charge = make_bank_charge(bank_account=other.bank_account, amount=88, posting_date=day(315))
		for filters in ({"bank": OTHER_BANK}, {"bank_account": other.bank_account}):
			with self.subTest(filters=filters):
				self.assertEqual([r.document for r in detail(**filters)], [charge.name])
		self.assertNotIn(charge.name, {r.document for r in detail(bank=BANK)})

		rows = detail(from_date=day(305), to_date=day(305))
		self.assertTrue(rows)
		self.assertEqual({getdate(r.posting_date) for r in rows}, {day(305)})
		self.assertRaises(frappe.ValidationError, run, from_date=day(310), to_date=day(300))
		self.assertRaises(frappe.ValidationError, run, source="Unknown")

	def test_chart(self):
		rows = detail()
		_columns, data, _message, chart, _summary = run(group_by="Month")
		self.assertEqual((chart["type"], chart["barOptions"]["stacked"]), ("bar", 1))
		months = [row.group for row in data if not row.get("is_total")]
		self.assertEqual(chart["data"]["labels"], months)
		self.assertEqual(len(chart["data"]["datasets"]), len(SOURCES))
		for dataset, source in zip(chart["data"]["datasets"], SOURCES, strict=True):
			expected = [
				flt(
					sum(r.amount for r in rows if r.source_value == source and str(r.posting_date)[:7] == m),
					2,
				)
				for m in months
			]
			self.assertEqual([flt(v, 2) for v in dataset["values"]], expected, source)

		chart = run(group_by="Bank")[3]
		self.assertEqual(len(chart["data"]["datasets"]), 1)
		self.assertNotIn("barOptions", chart)
		self.assertIsNone(run(view="Detail")[3])

	# ------------------------------------------------------------------ permissions, queries

	def test_permissions(self):
		add_user_permission(TREASURY_USER, "Bank Account", bank_account_name("EGP"))
		with self.set_user(TREASURY_USER):
			documents = {r.document for r in detail()}
		self.assertIn(self.fixture.c1.name, documents)
		self.assertNotIn(self.fixture.c2.name, documents)  # the USD bank account

		with self.set_user(ACCOUNTS_USER):
			sources = {r.source_value for r in detail()}
		expected = {
			label
			for label, (doctype, _events) in SOURCES.items()
			if frappe.has_permission(doctype, "read", user=ACCOUNTS_USER)
		}
		self.assertEqual(sources, expected)

		ensure_company(OTHER_COMPANY, OTHER_ABBR)
		add_user_permission(OTHER_CO_USER, "Company", OTHER_COMPANY)
		with self.set_user(OTHER_CO_USER):
			self.assertRaises(frappe.PermissionError, run)

	def test_no_n_plus_one(self):
		window = {"from_date": day(340), "to_date": day(360)}
		make_bank_charge(amount=61, posting_date=day(341))
		make_letter_of_guarantee(
			self.fixture.facility,
			amount=10_000,
			cash_margin_percent=0,
			commission_rate=0,
			commission_amount=61,
			issue_date=day(341),
			expiry_date=day(420),
		)
		run(view="Detail", **window)  # warm the caches
		queries = []
		original = frappe.db.__class__.sql

		def counting(*args, **kwargs):
			queries.append(1)
			return original(*args, **kwargs)

		frappe.db.__class__.sql = counting
		try:
			self.assertEqual(len(run(view="Detail", **window)[1]), 2)
		finally:
			frappe.db.__class__.sql = original
		baseline = len(queries)

		for amount in (62, 63, 64):
			make_bank_charge(amount=amount, posting_date=day(342))
		for amount in (65, 66):
			make_letter_of_guarantee(
				self.fixture.facility,
				amount=10_000,
				cash_margin_percent=0,
				commission_rate=0,
				commission_amount=amount,
				issue_date=day(342),
				expiry_date=day(420),
			)
		run(view="Detail", **window)  # the new documents may have reset caches: warm them again
		with self.assertQueryCount(baseline):
			self.assertEqual(len(run(view="Detail", **window)[1]), 7)
