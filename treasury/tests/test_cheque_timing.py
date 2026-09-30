"""Phase 8b: cheque timing (spec 8 §4, §5, §7): Current-dated / Post-dated from the issue and original due dates,
the backfill patch, the report and dashboard filters, and the same-day lifecycle of a current-dated cheque.

IntegrationTestCase rolls back only at the end of the class: the report and dashboard figures belong to each
test's own bank account (`ensure_alert_bank`)."""

import json
from pathlib import Path

import frappe
from frappe.tests import IntegrationTestCase, UnitTestCase
from frappe.utils import add_days, flt, getdate, today

from treasury.patches.v1_2 import set_cheque_timing
from treasury.pdc.doctype.pdc_cheque.pdc_cheque import PROTECTED_FIELDS
from treasury.pdc.overview import get_overview
from treasury.pdc.page.treasury_dashboard.treasury_dashboard import get_dashboard_data
from treasury.pdc.timing import CURRENT, POST_DATED, get_cheque_timing, validate_timing
from treasury.tests.test_dashboard_client import DASHBOARD_JS, method_body
from treasury.tests.utils import (
	COMPANY,
	CUSTOMER,
	DEFAULT_PDC_SETTINGS,
	PE_SETTINGS,
	SUPPLIER,
	TreasuryTestMixin,
	add_user_permission,
	assert_gl_entries,
	day,
	ensure_alert_bank,
	ensure_user,
	get_party_outstanding,
	get_voucher_count,
	leaf_rows,
	make_cheque_book,
	make_pdc_cheque,
	make_purchase_invoice,
	make_sales_invoice,
	make_security_cheque,
	run_report,
	unique_cheque_no,
	unique_leaf_start,
)

DOCTYPE = "PDC Cheque"
RESTRICTED_USER = "treasury-timing-restricted@example.com"


class TestChequeTiming(TreasuryTestMixin, IntegrationTestCase):
	def setUp(self):
		super().setUp()
		self.use_company_settings(**DEFAULT_PDC_SETTINGS)

	# ------------------------------------------------------------------ helpers

	def own_bank_cheques(self):
		"""A bank of this test only with one Current-dated and one Post-dated open outgoing cheque."""
		bank = ensure_alert_bank()
		current = make_pdc_cheque(
			amount=1000, issue_date=day(-10), due_date=day(-10), bank_account=bank.bank_account
		)
		post = make_pdc_cheque(
			amount=2500, issue_date=day(-10), due_date=day(10), bank_account=bank.bank_account
		)
		self.assertEqual((current.cheque_timing, post.cheque_timing), (CURRENT, POST_DATED))
		return bank, current, post

	@staticmethod
	def register_names(filters: dict) -> list[str]:
		_columns, data = run_report("PDC Register", {"company": COMPANY, **filters})
		return [row.cheque for row in data if not row.get("is_total")]

	@staticmethod
	def maturity_total(filters: dict) -> tuple[float, int]:
		result = run_report(
			"PDC Maturity", {"company": COMPANY, "direction": "Outgoing", "as_on": day(0), **filters}
		)
		data = result[1]
		total = next((row for row in data if row.get("is_total")), None)
		return (flt(total.total, 2), total.cheque_count) if total else (0.0, 0)

	# ------------------------------------------------------------------ the rule

	def test_timing_rule(self):
		self.assertEqual(get_cheque_timing(day(0), day(0)), CURRENT)
		self.assertEqual(get_cheque_timing(day(0), day(-1)), CURRENT)
		self.assertEqual(get_cheque_timing(str(day(0)), str(day(1))), POST_DATED)
		self.assertIsNone(get_cheque_timing(None, day(1)))
		self.assertIsNone(get_cheque_timing(day(0), None))

		self.assertIsNone(validate_timing(None))
		self.assertIsNone(validate_timing(""))
		self.assertEqual(validate_timing(CURRENT), CURRENT)
		self.assertEqual(validate_timing(POST_DATED), POST_DATED)
		for bad in ("Current", "post-dated", "X"):
			with self.subTest(value=bad):
				self.assertRaisesRegex(frappe.ValidationError, "Invalid Cheque Timing", validate_timing, bad)

	def test_same_day_is_current(self):
		for direction in ("Outgoing", "Incoming"):
			with self.subTest(direction=direction):
				cheque = make_pdc_cheque(direction, issue_date=day(0), due_date=day(0))
				self.assertEqual(cheque.cheque_timing, CURRENT)
				self.assertEqual(frappe.db.get_value(DOCTYPE, cheque.name, "cheque_timing"), CURRENT)

	def test_later_due_is_post_dated(self):
		for direction in ("Outgoing", "Incoming"):
			with self.subTest(direction=direction):
				cheque = make_pdc_cheque(direction, issue_date=day(0), due_date=day(1))
				self.assertEqual(cheque.cheque_timing, POST_DATED)

	def test_client_value_ignored_and_recomputed(self):
		cheque = make_pdc_cheque(issue_date=day(0), due_date=day(30), cheque_timing=CURRENT)
		self.assertEqual(cheque.cheque_timing, POST_DATED)

		# still Created: the dates may change, and the timing follows them
		cheque.original_due_date = cheque.issue_date
		cheque.save()
		self.assertEqual(frappe.db.get_value(DOCTYPE, cheque.name, "cheque_timing"), CURRENT)

		cheque.reload()
		cheque.cheque_timing = POST_DATED
		cheque.save()
		self.assertEqual(frappe.db.get_value(DOCTYPE, cheque.name, "cheque_timing"), CURRENT)

	def test_postponement_keeps_timing(self):
		cheque = make_pdc_cheque(issue_date=day(-10), due_date=day(-10))
		cheque.deliver(handover_date=day(-10))
		cheque.reload()
		self.assertEqual((cheque.operational_status, cheque.cheque_timing), ("Delivered", CURRENT))

		cheque.postpone(new_due_date=day(5), reason="Supplier agreed")
		cheque.reload()
		self.assertEqual(getdate(cheque.effective_due_date), day(5))
		self.assertEqual(getdate(cheque.original_due_date), day(-10))
		self.assertEqual(cheque.cheque_timing, CURRENT)

	def test_replacement_has_its_own_timing(self):
		cheque = make_pdc_cheque(issue_date=day(-20), due_date=day(10))
		self.assertEqual(cheque.cheque_timing, POST_DATED)
		values = {
			"cheque_no": unique_cheque_no(),
			"replacement_date": day(-5),
			"issue_date": day(-5),
			"original_due_date": day(-5),
			"reason": "Torn",
		}
		result = cheque.replace(new_cheque_values=values)
		new = frappe.get_doc(DOCTYPE, result["replaced_by"])
		self.assertEqual((getdate(new.issue_date), getdate(new.original_due_date)), (day(-5), day(-5)))
		self.assertEqual(new.cheque_timing, CURRENT)
		cheque.reload()
		self.assertEqual((cheque.operational_status, cheque.cheque_timing), ("Replaced", POST_DATED))

	def test_meta(self):
		field = frappe.get_meta(DOCTYPE).get_field("cheque_timing")
		self.assertEqual(field.fieldtype, "Select")
		self.assertEqual(field.options, "Current-dated\nPost-dated")
		self.assertEqual(
			(field.read_only, field.in_list_view, field.in_standard_filter, field.no_copy), (1, 1, 1, 1)
		)
		# derived on every save: a client-sent value is simply overwritten, not rejected
		self.assertNotIn("cheque_timing", PROTECTED_FIELDS)
		list_js = Path(frappe.get_app_path("treasury", "pdc", "doctype", "pdc_cheque", "pdc_cheque_list.js"))
		add_fields = list_js.read_text(encoding="utf-8").split("add_fields:", 1)[1].split("]", 1)[0]
		self.assertIn('"cheque_timing"', add_fields)

	# ------------------------------------------------------------------ patch

	def test_patch_backfills_idempotently(self):
		current = make_pdc_cheque(issue_date=day(-3), due_date=day(-3))
		post = make_pdc_cheque("Incoming", issue_date=day(-3), due_date=day(20))
		empty = make_pdc_cheque(issue_date=day(-3), due_date=day(40))
		kept = make_pdc_cheque(issue_date=day(-3), due_date=day(-3))
		cheque = frappe.qb.DocType(DOCTYPE)
		frappe.qb.update(cheque).set(cheque.cheque_timing, None).where(
			cheque.name.isin([current.name, post.name])
		).run()
		frappe.qb.update(cheque).set(cheque.cheque_timing, "").where(cheque.name == empty.name).run()
		# a row that already has a timing is not touched (only empty values are filled)
		frappe.qb.update(cheque).set(cheque.cheque_timing, POST_DATED).where(cheque.name == kept.name).run()
		names = [current.name, post.name, empty.name, kept.name]
		modified = {
			r.name: r.modified
			for r in frappe.get_all(DOCTYPE, filters={"name": ["in", names]}, fields=["name", "modified"])
		}
		self.assertIsNone(frappe.db.get_value(DOCTYPE, current.name, "cheque_timing"))

		set_cheque_timing.execute()
		set_cheque_timing.execute()

		rows = {
			r.name: r
			for r in frappe.get_all(
				DOCTYPE, filters={"name": ["in", names]}, fields=["name", "cheque_timing", "modified"]
			)
		}
		self.assertEqual(
			{name: rows[name].cheque_timing for name in names},
			{current.name: CURRENT, post.name: POST_DATED, empty.name: POST_DATED, kept.name: POST_DATED},
		)
		self.assertEqual({name: rows[name].modified for name in names}, modified)

	# ------------------------------------------------------------------ reports

	def test_pdc_register_filter(self):
		bank, current, post = self.own_bank_cheques()
		filters = {"bank_account": bank.bank_account}
		self.assertEqual(self.register_names({**filters, "cheque_timing": CURRENT}), [current.name])
		self.assertEqual(self.register_names({**filters, "cheque_timing": POST_DATED}), [post.name])
		both = sorted([current.name, post.name])
		self.assertEqual(sorted(self.register_names(filters)), both)
		self.assertEqual(sorted(self.register_names({**filters, "cheque_timing": ""})), both)
		self.assertRaisesRegex(
			frappe.ValidationError,
			"Invalid Cheque Timing",
			run_report,
			"PDC Register",
			{"company": COMPANY, **filters, "cheque_timing": "X"},
		)

		columns, data = run_report("PDC Register", {"company": COMPANY, **filters})
		fieldnames = [c["fieldname"] for c in columns]
		self.assertIn("cheque_timing", fieldnames)
		self.assertEqual(fieldnames.index("cheque_timing"), fieldnames.index("original_due_date") + 1)
		self.assertEqual(
			{row.cheque: row.cheque_timing for row in data if row.get("cheque")},
			{current.name: CURRENT, post.name: POST_DATED},
		)

	def test_pdc_maturity_filter(self):
		bank, _current, _post = self.own_bank_cheques()
		filters = {"bank_account": bank.bank_account}
		self.assertEqual(self.maturity_total(filters), (3500.0, 2))
		self.assertEqual(self.maturity_total({**filters, "cheque_timing": CURRENT}), (1000.0, 1))
		self.assertEqual(self.maturity_total({**filters, "cheque_timing": POST_DATED}), (2500.0, 1))
		self.assertRaisesRegex(
			frappe.ValidationError,
			"Invalid Cheque Timing",
			run_report,
			"PDC Maturity",
			{"company": COMPANY, "direction": "Outgoing", **filters, "cheque_timing": "X"},
		)

	def test_cheque_leaf_register_filter(self):
		start = unique_leaf_start(5)
		book = make_cheque_book(start, start + 4, leaf_digits=8)
		current = make_pdc_cheque(cheque_book=book.name, issue_date=day(-10), due_date=day(-10))
		post = make_pdc_cheque(cheque_book=book.name, issue_date=day(-10), due_date=day(10))
		make_security_cheque(
			"Outgoing", amount=500, cheque_book=book.name, issue_date=day(-30), expiry_date=day(200)
		)
		self.assertEqual(
			(current.cheque_no, post.cheque_no), (str(start).zfill(8), str(start + 1).zfill(8))
		)

		rows = leaf_rows({"cheque_book": book.name})
		self.assertEqual(len(rows), 5)
		self.assertEqual(
			(rows[start].cheque_timing, rows[start + 1].cheque_timing, rows[start + 2].cheque_timing),
			(CURRENT, POST_DATED, None),
		)
		self.assertEqual(set(leaf_rows({"cheque_book": book.name, "cheque_timing": POST_DATED})), {start + 1})
		self.assertEqual(set(leaf_rows({"cheque_book": book.name, "cheque_timing": CURRENT})), {start})

		# the book header plus only the matching leaf (security and unused leaves never match)
		result = run_report(
			"Cheque Leaf Register", {"company": COMPANY, "cheque_book": book.name, "cheque_timing": POST_DATED}
		)
		data = result[1]
		self.assertEqual(len(data), 2)
		self.assertTrue(data[0].get("is_group"))
		self.assertEqual(data[1].document, post.name)
		columns = [c["fieldname"] for c in result[0]]
		self.assertEqual(columns.index("cheque_timing"), columns.index("due_date") + 1)
		self.assertRaisesRegex(
			frappe.ValidationError,
			"Invalid Cheque Timing",
			run_report,
			"Cheque Leaf Register",
			{"company": COMPANY, "cheque_book": book.name, "cheque_timing": "X"},
		)

		# a leaf of a cheque the user may not read never matches
		ensure_user(RESTRICTED_USER, ["Treasury User", "Accounts User"])
		add_user_permission(RESTRICTED_USER, DOCTYPE, post.name)
		with self.set_user(RESTRICTED_USER):
			self.assertEqual(leaf_rows({"cheque_book": book.name, "cheque_timing": CURRENT}), {})
			self.assertEqual(
				set(leaf_rows({"cheque_book": book.name, "cheque_timing": POST_DATED})), {start + 1}
			)

	# ------------------------------------------------------------------ dashboard

	def test_dashboard_timing(self):
		bank = ensure_alert_bank()
		now = getdate(today())
		values = {"bank_account": bank.bank_account}
		make_pdc_cheque(amount=1000, issue_date=now, due_date=now, **values)  # current, due this week
		make_pdc_cheque(amount=2000, issue_date=add_days(now, -5), due_date=add_days(now, 3), **values)
		make_pdc_cheque(amount=4000, issue_date=now, due_date=add_days(now, 20), **values)

		plain = get_overview(COMPANY, [bank.bank_account], 7)
		expected = {
			None: {"issued": (3, 7000), "due_out": (2, 3000)},
			POST_DATED: {"issued": (2, 6000), "due_out": (1, 2000)},
			CURRENT: {"issued": (1, 1000), "due_out": (1, 1000)},
		}
		for timing, cards in expected.items():
			with self.subTest(timing=timing):
				overview = get_overview(COMPANY, [bank.bank_account], 7, cheque_timing=timing)
				self.assertEqual(overview.cheque_timing, timing or "")
				cheques = overview.sections.cheques
				for key, (count, amount) in cards.items():
					kpi = cheques[key]
					self.assertEqual((kpi.count, flt(kpi.amount, 2)), (count, amount), key)
					self.assertEqual(kpi.route_options.get("cheque_timing"), timing)
					_columns, data = run_report("PDC Register", kpi.route_options)
					total = data[-1]
					self.assertEqual((total.cheque_count, flt(total.base_amount, 2)), (kpi.count, amount), key)
				# not filtered: the positions and this month's bounces (their reports have no timing filter)
				self.assertEqual(overview.sections.positions, plain.sections.positions)
				self.assertEqual(cheques.bounced_month, plain.sections.cheques.bounced_month)

		self.assertRaisesRegex(
			frappe.ValidationError, "Invalid Cheque Timing", get_dashboard_data, COMPANY, None, "7", "bad"
		)
		data = get_dashboard_data(COMPANY, json.dumps([bank.bank_account]), "7", POST_DATED)
		self.assertEqual(data.cheque_timing, POST_DATED)
		self.assertEqual(data.sections.cheques.issued.count, 2)
		self.assertEqual(get_dashboard_data(COMPANY, json.dumps([bank.bank_account]), "7").cheque_timing, "")


class TestDashboardTimingClient(UnitTestCase):
	"""The page script (no node at runtime: checked as text) sends, stores and resets the timing filter."""

	def test_timing_filter_wiring(self):
		source = DASHBOARD_JS.read_text(encoding="utf-8")
		filters = method_body(source, "make_filters() {")
		self.assertIn('fieldname: "cheque_timing"', filters)
		self.assertIn('__("All Cheques", null, TD_CTX)', filters)
		self.assertIn('addClass("trd-filter-timing")', filters)
		self.assertIn("stored.cheque_timing", filters)
		self.assertIn("cheque_timing:", method_body(source, "\tfilters() {"))
		self.assertIn('this.timing_field.set_value("")', method_body(source, "reset_filters() {"))
		self.assertIn("cheque_timing: filters.cheque_timing", method_body(source, "fetch(filters) {"))
		self.assertIn("this.data.cheque_timing", method_body(source, "render_cheques(c) {"))


class TestCurrentDatedLifecycle(TreasuryTestMixin, IntegrationTestCase):
	"""Spec 8 §5: a current-dated cheque follows the normal flow on a single day (Payment Entry settlement)."""

	def setUp(self):
		super().setUp()
		self.use_company_settings(**PE_SETTINGS)

	def assert_statuses(self, cheque, operational: str, accounting: str):
		cheque.reload()
		self.assertEqual(
			(cheque.operational_status, cheque.accounting_status, cheque.cheque_timing),
			(operational, accounting, CURRENT),
		)

	def test_current_dated_cheque_same_day_outgoing(self):
		egp, d = self.data.egp, day(-10)
		pi = make_purchase_invoice(1000, posting_date=d)
		cheque = make_pdc_cheque(amount=1000, issue_date=d, due_date=d, invoice=pi)
		self.assertEqual(cheque.cheque_timing, CURRENT)
		cheque.deliver(handover_date=d)
		cheque.clear(clearance_date=d)
		self.assert_statuses(cheque, "Cleared", "Bank Posted")

		assert_gl_entries(
			self,
			"Payment Entry",
			cheque.party_payment_entry,
			[
				(self.data.payable, 1000, 0, SUPPLIER, d),
				(egp.pdc_payable, 0, 1000, None, d),
			],
		)
		assert_gl_entries(
			self,
			"Payment Entry",
			cheque.bank_payment_entry,
			[
				(egp.pdc_payable, 1000, 0, None, d),
				(egp.bank_gl, 0, 1000, None, d),
			],
		)
		clearance_date = frappe.db.get_value("Payment Entry", cheque.bank_payment_entry, "clearance_date")
		self.assertEqual(getdate(clearance_date), d)
		self.assertEqual(get_party_outstanding("Purchase Invoice", pi.name), 0)
		self.assertEqual(get_voucher_count(DOCTYPE, cheque.name, docstatus=1), 2)

		# idempotent: a second clearance is refused and posts nothing
		self.assertRaises(frappe.ValidationError, cheque.clear, clearance_date=d)
		self.assertEqual(get_voucher_count(DOCTYPE, cheque.name), 2)
		self.assert_statuses(cheque, "Cleared", "Bank Posted")

	def test_current_dated_cheque_same_day_incoming(self):
		egp, d = self.data.egp, day(-10)
		si = make_sales_invoice(2000, posting_date=d)
		cheque = make_pdc_cheque("Incoming", amount=2000, issue_date=d, due_date=d, invoice=si)
		self.assertEqual(cheque.cheque_timing, CURRENT)
		cheque.receive(receipt_date=d)
		cheque.deposit(deposit_date=d)
		cheque.clear(clearance_date=d)
		self.assert_statuses(cheque, "Cleared", "Bank Posted")

		assert_gl_entries(
			self,
			"Payment Entry",
			cheque.party_payment_entry,
			[
				(egp.pdc_receivable, 2000, 0, None, d),
				(self.data.receivable, 0, 2000, CUSTOMER, d),
			],
		)
		assert_gl_entries(
			self,
			"Payment Entry",
			cheque.under_collection_payment_entry,
			[
				(egp.under_collection, 2000, 0, None, d),
				(egp.pdc_receivable, 0, 2000, None, d),
			],
		)
		assert_gl_entries(
			self,
			"Payment Entry",
			cheque.bank_payment_entry,
			[
				(egp.bank_gl, 2000, 0, None, d),
				(egp.under_collection, 0, 2000, None, d),
			],
		)
		self.assertEqual(get_party_outstanding("Sales Invoice", si.name), 0)
		self.assertEqual(get_voucher_count(DOCTYPE, cheque.name, docstatus=1), 3)

		# idempotent: a second clearance is refused and posts nothing
		self.assertRaises(frappe.ValidationError, cheque.clear, clearance_date=d)
		self.assertEqual(get_voucher_count(DOCTYPE, cheque.name), 3)
		self.assert_statuses(cheque, "Cleared", "Bank Posted")
