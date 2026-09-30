"""Phase 7f: Security Cheques Summary and Security Cheque Movements (spec 7f §7.5)."""

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, getdate

from treasury.pdc.report_utils import report_today
from treasury.security_cheques.report.security_cheque_movements.security_cheque_movements import (
	execute as movements,
)
from treasury.security_cheques.report.security_cheques_summary.security_cheques_summary import (
	execute as summary_report,
)
from treasury.tests.utils import (
	COMPANY,
	CUSTOMER,
	OTHER_ABBR,
	OTHER_COMPANY,
	SUPPLIER,
	TreasuryTestMixin,
	add_user_permission,
	ensure_company,
	ensure_custody_location,
	ensure_customer,
	ensure_user,
	make_security_cheque,
)

SAFE = "_Test Treasury Safe"
VAULT = "_Test Treasury Vault"
FIXTURE_PURPOSE = "Tender"  # the summary fixture; the movement scenarios use other purposes
MOVEMENT_CUSTOMER = "_Test Treasury Movement Customer"
OTHER_MOVEMENT_CUSTOMER = "_Test Treasury Movement Customer 2"
COUNT_CUSTOMER = "_Test Treasury Movement Count"
PERMISSION_CUSTOMER = "_Test Treasury Movement Permission"
RESTRICTED_USER = "treasury-movements-restricted@example.com"
OTHER_COMPANY_USER = "treasury-movements-othercompany@example.com"


class TestSecurityChequeReports7f(TreasuryTestMixin, IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		for name in (MOVEMENT_CUSTOMER, OTHER_MOVEMENT_CUSTOMER, COUNT_CUSTOMER, PERMISSION_CUSTOMER):
			ensure_customer(name)
		ensure_custody_location(SAFE)
		ensure_custody_location(VAULT)
		cls.today = report_today()
		cls.fixture = cls.make_fixture()

	@classmethod
	def make_fixture(cls) -> frappe._dict:
		"""Incoming: A 1000 (+10 days), B 2000 (+45), C Open / Blank (-5), D 500 (no expiry), E 800 returned.
		Outgoing: F 3000 (+100), G 700 USD at 50 (-2), both issued."""
		today = cls.today

		def incoming(amount, expiry, amount_type="Fixed"):
			cheque = make_security_cheque(
				amount=amount,
				amount_type=amount_type,
				expiry_date=expiry or add_days(today, 1),
				purpose=FIXTURE_PURPOSE,
			)
			if not expiry:
				frappe.db.set_value("Security Cheque", cheque.name, "expiry_date", None)
			cheque.receive(receipt_date=today)
			return cheque

		def outgoing(amount, expiry, currency="EGP"):
			cheque = make_security_cheque(
				"Outgoing", amount=amount, expiry_date=expiry, currency=currency, purpose=FIXTURE_PURPOSE
			)
			cheque.issue(handover_date=today)
			return cheque

		fixture = frappe._dict(
			a=incoming(1000, add_days(today, 10)),
			b=incoming(2000, add_days(today, 45)),
			c=incoming(0, add_days(today, -5), "Open / Blank"),
			d=incoming(500, None),
			e=incoming(800, add_days(today, 60)),
			f=outgoing(3000, add_days(today, 100)),
			g=outgoing(700, add_days(today, -2), "USD"),
		)
		fixture.e.return_cheque(return_date=today, reason="Tender lost")
		return fixture

	def setUp(self):
		super().setUp()
		previous = frappe.local.lang
		self.addCleanup(setattr, frappe.local, "lang", previous)
		frappe.local.lang = "en"

	# ------------------------------------------------------------------ Security Cheques Summary

	def run_summary(self, **filters):
		return summary_report({"company": COMPANY, "purpose": FIXTURE_PURPOSE, **filters})

	@staticmethod
	def headers(data) -> dict:
		return {row.direction: row for row in data if row.get("is_group")}

	def test_summary_by_party_default(self):
		columns, data, _message, _chart, _summary = self.run_summary()
		headers = self.headers(data)
		held, given = headers["Incoming"], headers["Outgoing"]
		self.assertEqual((held.fixed_count, held.open_count, held.amount), (3, 1, 3500))
		self.assertEqual(
			(held.expired, held.range_1, held.range_2, held.range_3, held.range_last, held.no_expiry),
			(0, 1000, 2000, 0, 0, 500),
		)
		self.assertEqual((given.fixed_count, given.open_count, given.amount), (2, 0, 38000))
		self.assertEqual((given.expired, given.range_last, given.range_1), (35000, 3000, 0))
		self.assertEqual(held.closed_amount, 0)

		# one row per party under each header, E (returned) left out
		customer_row = next(r for r in data if r.indent == 1 and r.party == CUSTOMER)
		self.assertEqual((customer_row.party_type, customer_row.fixed_count), ("Customer", 3))
		supplier_row = next(r for r in data if r.indent == 1 and r.party == SUPPLIER)
		self.assertEqual(supplier_row.amount, 38000)
		labels = [c["label"] for c in columns if c["fieldname"].startswith("range_")]
		self.assertEqual(labels, ["0-30", "31-60", "61-90", "91+"])
		self.assertEqual([r.direction for r in data if r.get("is_group")], ["Incoming", "Outgoing"])

	def test_summary_ranges(self):
		columns, data, *_rest = self.run_summary(ageing_ranges="15, 60")
		labels = [c["label"] for c in columns if c["fieldname"].startswith("range_")]
		self.assertEqual(labels, ["0-15", "16-60", "61+"])
		held, given = self.headers(data)["Incoming"], self.headers(data)["Outgoing"]
		self.assertEqual((held.range_1, held.range_2, held.range_last), (1000, 2000, 0))
		self.assertEqual((given.range_last, given.expired), (3000, 35000))
		for bad in ("60, 30", "30, x", "0, 30", "1, 2, 3, 4, 5, 6", "30, 30"):
			with self.assertRaises(frappe.ValidationError) as ctx:
				self.run_summary(ageing_ranges=bad)
			self.assertIn("Ageing ranges must be increasing", str(ctx.exception))

	def test_summary_group_status_with_final(self):
		statuses = ["Received", "In Custody", "Issued", "Returned"]
		_columns, data, *_rest = self.run_summary(status=statuses, group_by="Status")
		returned = next(r for r in data if r.indent == 1 and r.status == "Returned")
		self.assertEqual((returned.closed_amount, returned.amount, returned.range_2), (800, 0, 0))
		received = next(r for r in data if r.indent == 1 and r.status == "Received")
		self.assertEqual((received.amount, received.fixed_count, received.open_count), (3500, 3, 1))
		held = self.headers(data)["Incoming"]
		self.assertEqual((held.closed_amount, held.amount), (800, 3500))

		_columns, data, *_rest = self.run_summary(group_by="Purpose")
		self.assertEqual([r.purpose for r in data if r.indent == 1], [FIXTURE_PURPOSE, FIXTURE_PURPOSE])
		self.assertRaises(frappe.ValidationError, self.run_summary, status=["Somewhere"])

	def test_summary_cards_chart(self):
		_columns, _data, _message, chart, cards = self.run_summary()
		values = {card["label"]: card["value"] for card in cards}
		self.assertEqual(
			values,
			{
				"Held from Others": 3500,
				"Given to Others": 38000,
				"Expired Not Returned": 2,
				"Expiring in 30 Days": 1,
			},
		)
		self.assertEqual(chart["data"]["labels"], ["Expired", "0-30", "31-60", "61-90", "91+", "No Expiry"])
		datasets = {d["name"]: d["values"] for d in chart["data"]["datasets"]}
		self.assertEqual(datasets["Held from Others"], [0, 1000, 2000, 0, 0, 500])
		self.assertEqual(datasets["Given to Others"], [35000, 0, 0, 0, 3000, 0])

	# ------------------------------------------------------------------ Security Cheque Movements

	def run_movements(self, party=MOVEMENT_CUSTOMER, **filters):
		values = {
			"company": COMPANY,
			"from_date": self.today,
			"to_date": self.today,
			"party_type": "Customer",
			"party": party,
			**filters,
		}
		return movements(values)

	def moved_cheque(self, dates: tuple, party=MOVEMENT_CUSTOMER, purpose="Rent"):
		"""Received into the Safe, transferred to the Vault, returned (on `dates`), expiry extended before."""
		received, transferred, returned = dates
		cheque = make_security_cheque(
			party=party,
			purpose=purpose,
			issue_date=add_days(self.today, -30),
			expiry_date=add_days(self.today, 30),
		)
		cheque.receive(receipt_date=received, custody_location=SAFE)
		cheque.transfer_custody(transfer_date=transferred, custody_location=VAULT)
		cheque.extend_expiry(new_date=add_days(self.today, 90), reason="Contract extended")
		cheque.return_cheque(return_date=returned, reason="Contract closed")
		return cheque

	def test_movements_period(self):
		cheque = self.moved_cheque((self.today, self.today, self.today))
		_columns, data, _message, _chart, cards = self.run_movements()
		self.assertEqual({r.security_cheque for r in data}, {cheque.name})
		self.assertEqual(
			[r.event for r in data],
			["Created", "Received", "Stored", "Custody Transferred", "Expiry Extended", "Returned"],
		)
		rows = {r.event: r for r in data}
		self.assertEqual((rows["Stored"].from_location, rows["Stored"].to_location), (None, SAFE))
		self.assertEqual(
			(rows["Custody Transferred"].from_location, rows["Custody Transferred"].to_location), (SAFE, VAULT)
		)
		self.assertEqual((rows["Returned"].from_location, rows["Returned"].to_location), (VAULT, None))
		self.assertEqual(
			(rows["Returned"].from_status, rows["Returned"].to_status), ("In Custody", "Returned")
		)
		self.assertTrue(all(getdate(r.movement_date) == self.today for r in data))
		self.assertNotIn("Custody Moved", [r.event for r in data])
		self.assertEqual({card["label"]: card["value"] for card in cards}["Returned"], 1)

	def test_movements_filters(self):
		dates = (add_days(self.today, -5), add_days(self.today, -3), add_days(self.today, -1))
		cheque = self.moved_cheque(dates, party=OTHER_MOVEMENT_CUSTOMER)
		period = {"from_date": add_days(self.today, -10), "to_date": self.today}

		def events(**filters):
			_columns, data, *_rest = self.run_movements(OTHER_MOVEMENT_CUSTOMER, **{**period, **filters})
			return [r.event for r in data if r.security_cheque == cheque.name]

		self.assertEqual(events(event=["Returned"]), ["Returned"])
		self.assertEqual(events(movement="Custody"), ["Stored", "Custody Transferred", "Returned"])
		self.assertEqual(events(custody_location=VAULT), ["Custody Transferred", "Returned"])
		self.assertEqual(
			sorted(events(movement="Status")),
			sorted(["Created", "Received", "Stored", "Custody Transferred", "Expiry Extended", "Returned"]),
		)
		# events dated before the period are left out; those without a date count by their recording date
		self.assertEqual(
			sorted(events(from_date=add_days(self.today, -3))),
			sorted(["Created", "Custody Transferred", "Expiry Extended", "Returned"]),
		)
		self.assertEqual(events(from_date=add_days(self.today, -2), to_date=add_days(self.today, -2)), [])
		self.assertEqual(len(events(user="Administrator")), 6)
		self.assertEqual(events(user=RESTRICTED_USER), [])
		self.assertRaises(frappe.ValidationError, self.run_movements, event=["Teleported"])
		self.assertRaises(
			frappe.ValidationError, self.run_movements, from_date=self.today, to_date=add_days(self.today, -1)
		)
		self.assertRaises(frappe.ValidationError, movements, {"company": COMPANY, "from_date": self.today})

	def test_movements_permissions(self):
		allowed = self.moved_cheque((self.today,) * 3, party=PERMISSION_CUSTOMER)
		hidden = self.moved_cheque((self.today,) * 3, party=PERMISSION_CUSTOMER)
		ensure_user(RESTRICTED_USER, ["Treasury User"])
		add_user_permission(RESTRICTED_USER, "Security Cheque", allowed.name, applicable_for="Security Cheque")
		with self.set_user(RESTRICTED_USER):
			_columns, data, *_rest = self.run_movements(PERMISSION_CUSTOMER)
		self.assertEqual({r.security_cheque for r in data}, {allowed.name})
		_columns, data, *_rest = self.run_movements(PERMISSION_CUSTOMER)
		self.assertEqual({r.security_cheque for r in data}, {allowed.name, hidden.name})

		ensure_company(OTHER_COMPANY, OTHER_ABBR)
		ensure_user(OTHER_COMPANY_USER, ["Treasury User"])
		add_user_permission(OTHER_COMPANY_USER, "Company", OTHER_COMPANY)
		with self.set_user(OTHER_COMPANY_USER):
			self.assertRaises(frappe.PermissionError, self.run_movements)

	def test_movements_query_count(self):
		for _i in range(30):
			cheque = make_security_cheque(party=COUNT_CUSTOMER, purpose="Rent")
			cheque.receive(receipt_date=self.today, custody_location=SAFE)
			cheque.return_cheque(return_date=self.today, reason="Done")
		filters = {"company": COMPANY, "from_date": self.today, "to_date": self.today}
		movements(filters)  # warm the caches
		with self.assertQueryCount(6):
			_columns, data, *_rest = movements(filters)
		rows = [r for r in data if r.party == COUNT_CUSTOMER]
		self.assertEqual(len(rows), 30 * 4)  # Created, Received, Stored, Returned
		self.assertEqual(len([r for r in rows if r.event == "Custody Moved"]), 0)
		self.assertEqual(len([r for r in rows if r.to_location == SAFE]), 30)
