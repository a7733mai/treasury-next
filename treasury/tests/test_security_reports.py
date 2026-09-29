"""Security Cheque reports, number cards and expiry alerts (Phase 4, PLAN.md §7).

Fixed dates around BASE_DATE (`day`); the reports' "today" is frozen to day(0) (`treasury.pdc.report_utils
.today` is patched). Each test isolates its data through its own party / custody location, and the number
cards are checked as differences, so tests neither depend on each other nor on other test files.
"""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import flt

from treasury.security_cheques import dashboard, scheduler
from treasury.security_cheques.report.expired_security_cheques_not_returned.expired_security_cheques_not_returned import (
	execute as expired_not_returned,
)
from treasury.security_cheques.report.security_cheque_register.security_cheque_register import (
	execute as security_register,
)
from treasury.security_cheques.report.security_cheques_by_custody.security_cheques_by_custody import (
	execute as by_custody,
)
from treasury.security_cheques.report.security_exposure.security_exposure import execute as exposure
from treasury.tests.utils import (
	COMPANY,
	DEFAULT_PDC_SETTINGS,
	OTHER_ABBR,
	OTHER_COMPANY,
	TreasuryTestMixin,
	add_user_permission,
	day,
	ensure_company,
	ensure_custody_location,
	ensure_customer,
	ensure_supplier,
	ensure_user,
	make_sales_order,
	make_security_cheque,
)

T = day(0)  # the reports' "today"
DOCTYPE = "Security Cheque"
MANAGER = "treasury-secalert-manager@example.com"
USER = "treasury-secalert-user@example.com"
OUTSIDER = "treasury-secalert-outsider@example.com"
RESTRICTED = "treasury-secrpt-other-co@example.com"


def new_customer() -> str:
	return ensure_customer(f"_Test Sec Rpt Customer {frappe.generate_hash(length=6)}")


def new_supplier() -> str:
	return ensure_supplier(f"_Test Sec Rpt Supplier {frappe.generate_hash(length=6)}")


def new_location() -> str:
	return ensure_custody_location(f"_Test Sec Rpt Safe {frappe.generate_hash(length=6)}")


def held(party, amount=1000, expiry=30, location=None, custodian=None, **values):
	"""An incoming security cheque received on day(-50) (into custody when `location` is given)."""
	cheque = make_security_cheque(
		party=party, amount=amount, issue_date=day(-60), expiry_date=day(expiry), **values
	)
	cheque.receive(receipt_date=day(-50), custody_location=location, custodian=custodian)
	cheque.reload()
	return cheque


def given(party, amount=1000, expiry=30, **values):
	cheque = make_security_cheque(
		"Outgoing", party=party, amount=amount, issue_date=day(-60), expiry_date=day(expiry), **values
	)
	cheque.issue(handover_date=day(-50))
	cheque.reload()
	return cheque


def cheque_rows(data) -> dict:
	return {r.security_cheque: r for r in data if r.get("security_cheque")}


class SecurityReportTestCase(TreasuryTestMixin, IntegrationTestCase):
	def setUp(self):
		super().setUp()
		self.use_company_settings(**DEFAULT_PDC_SETTINGS)
		patcher = patch("treasury.pdc.report_utils.today", return_value=str(T))
		patcher.start()
		self.addCleanup(patcher.stop)


# ---------------------------------------------------------------------------
# Security Cheque Register
# ---------------------------------------------------------------------------


class TestSecurityChequeRegister(SecurityReportTestCase):
	def test_rows_and_filters(self):
		customer = new_customer()
		location = new_location()
		in_custody = held(customer, 1000, expiry=10, location=location)
		open_expired = held(customer, expiry=-5, amount_type="Open / Blank")
		returned = held(customer, 700, expiry=-20)
		returned.return_cheque(return_date=day(-1), reason="Completed")
		created = make_security_cheque(party=customer, amount=300, issue_date=day(-60), expiry_date=day(90))

		filters = {"company": COMPANY, "party_type": "Customer", "party": customer}
		_columns, data = security_register(filters)
		rows = cheque_rows(data)
		self.assertEqual(set(rows), {in_custody.name, open_expired.name, returned.name, created.name})
		self.assertEqual(rows[in_custody.name].days_to_expiry, 10)
		self.assertEqual(rows[open_expired.name].days_to_expiry, -5)
		self.assertIsNone(rows[returned.name].days_to_expiry)  # final: no longer matters
		self.assertEqual(rows[open_expired.name].base_amount, 0)  # open / blank: no amount
		self.assertEqual(rows[in_custody.name].custody_location, location)
		total = data[-1]
		self.assertTrue(total.is_total)
		self.assertEqual((total.cheque_count, flt(total.base_amount)), (4, 2000))

		_columns, data = security_register({**filters, "status": ["In Custody"]})
		self.assertEqual(list(cheque_rows(data)), [in_custody.name])
		_columns, data = security_register({**filters, "expiry_from": day(-10), "expiry_to": day(20)})
		self.assertEqual(set(cheque_rows(data)), {in_custody.name, open_expired.name})
		_columns, data = security_register({**filters, "custody_location": location})
		self.assertEqual(list(cheque_rows(data)), [in_custody.name])
		_columns, data = security_register({**filters, "amount_type": "Open / Blank"})
		self.assertEqual(list(cheque_rows(data)), [open_expired.name])

		self.assertRaises(frappe.ValidationError, security_register, {**filters, "status": ["Bounced"]})
		self.assertRaises(
			frappe.ValidationError, security_register, {**filters, "expiry_from": day(5), "expiry_to": day(1)}
		)


# ---------------------------------------------------------------------------
# Security Exposure
# ---------------------------------------------------------------------------


class TestSecurityExposure(SecurityReportTestCase):
	def test_by_party(self):
		customer, supplier = new_customer(), new_supplier()
		held(customer, 3000)
		held(customer, 2000, location=new_location())
		held(customer, amount_type="Open / Blank")
		returned = held(customer, 999)
		returned.return_cheque(return_date=day(-1), reason="Completed")
		make_security_cheque(party=customer, amount=5555, issue_date=day(-60))  # Created: not held yet
		given(supplier, 4000)

		_columns, data, _message, _chart, summary = exposure(
			{"company": COMPANY, "party_type": "Customer", "party": customer}
		)
		self.assertEqual(len(data), 1)
		row = data[0]
		self.assertEqual(
			(row.direction, row.party, row.fixed_count, row.open_count, flt(row.amount)),
			("Incoming", customer, 2, 1, 5000),
		)
		totals = {s["label"]: s["value"] for s in summary}
		self.assertEqual(totals["Held from Others"], 5000)
		self.assertEqual(totals["Given to Others"], 0)
		self.assertEqual(totals["Open / Blank Held"], 1)

		_columns, data, _message, _chart, summary = exposure(
			{"company": COMPANY, "party_type": "Supplier", "party": supplier}
		)
		self.assertEqual([(r.direction, flt(r.amount)) for r in data], [("Outgoing", 4000)])
		self.assertEqual({s["label"]: s["value"] for s in summary}["Given to Others"], 4000)

	def test_shortfall_per_reference_document(self):
		customer = new_customer()
		order = make_sales_order(50000, transaction_date=day(-70), customer=customer)
		reference = {"reference_doctype": "Sales Order", "reference_name": order.name, "required_amount": 10000}
		held(customer, 3000, **reference)
		held(customer, 2000, **reference)
		held(customer, amount_type="Open / Blank", **reference)
		lone = held(customer, 1000, required_amount=1500)  # no reference: its own row

		_columns, data, _message, _chart, summary = exposure(
			{"company": COMPANY, "group_by": "Reference Document", "party_type": "Customer", "party": customer}
		)
		rows = {(r.reference_doctype, r.reference_name): r for r in data}
		contract = rows[("Sales Order", order.name)]
		self.assertEqual(
			(contract.fixed_count, contract.open_count, flt(contract.amount)), (2, 1, 5000)
		)
		# the requirement is the document's (the highest on its cheques), not the sum of the cheques' fields
		self.assertEqual((flt(contract.required_amount), flt(contract.shortfall)), (10000, 5000))
		own = rows[(DOCTYPE, lone.name)]
		self.assertEqual((flt(own.required_amount), flt(own.amount), flt(own.shortfall)), (1500, 1000, 500))
		self.assertEqual({s["label"]: s["value"] for s in summary}["Total Shortfall"], 5500)

		# covered: no shortfall, never negative
		held(customer, 7000, **reference)
		_columns, data, *_rest = exposure(
			{
				"company": COMPANY,
				"group_by": "Reference Document",
				"reference_doctype": "Sales Order",
				"reference_name": order.name,
			}
		)
		self.assertEqual((flt(data[0].amount), flt(data[0].shortfall)), (12000, 0))

	def test_invalid_group_by(self):
		self.assertRaises(frappe.ValidationError, exposure, {"company": COMPANY, "group_by": "Colour"})


# ---------------------------------------------------------------------------
# Security Cheques by Custody / Expired Not Returned
# ---------------------------------------------------------------------------


class TestCustodyAndExpiredReports(SecurityReportTestCase):
	def test_by_custody(self):
		customer = new_customer()
		safe, vault = new_location(), new_location()
		custodian = ensure_user("treasury-secrpt-custodian@example.com", ["Treasury User"])
		a = held(customer, 1000, location=safe, custodian=custodian)
		b = held(customer, 2000, location=safe, custodian=custodian)
		moved = held(customer, 500, location=safe)
		moved.transfer_custody(transfer_date=day(-10), custody_location=vault)

		_columns, data = by_custody({"company": COMPANY, "custody_location": safe})
		self.assertEqual(data[0].is_group, 1)
		self.assertEqual((data[0].custody_location, data[0].custodian), (safe, custodian))
		self.assertEqual((data[0].cheque_count, flt(data[0].base_amount)), (2, 3000))
		self.assertEqual({r.security_cheque for r in data if r.get("security_cheque")}, {a.name, b.name})
		self.assertEqual((data[-1].cheque_count, flt(data[-1].base_amount)), (2, 3000))

		_columns, data = by_custody({"company": COMPANY, "custody_location": vault})
		self.assertEqual([r.security_cheque for r in data if r.get("security_cheque")], [moved.name])

		not_stored = held(customer, 800)
		_columns, data = by_custody({"company": COMPANY})
		index = next(i for i, r in enumerate(data) if r.get("security_cheque") == not_stored.name)
		header = next(r for r in reversed(data[:index]) if r.get("is_group"))
		self.assertIsNone(header.custody_location)
		self.assertEqual(header.group_label, "No Custody Location")

	def test_expired_not_returned(self):
		customer = new_customer()
		expired = held(customer, 1000, expiry=-10)
		returned = held(customer, 1000, expiry=-10)
		returned.return_cheque(return_date=day(-1), reason="Completed")
		later = held(customer, 1000, expiry=5)
		make_security_cheque(party=customer, issue_date=day(-60), expiry_date=day(-10))  # Created

		filters = {"company": COMPANY, "party_type": "Customer", "party": customer}
		_columns, data = expired_not_returned(filters)
		rows = cheque_rows(data)
		self.assertEqual(list(rows), [expired.name])
		self.assertEqual(rows[expired.name].days_expired, 10)
		self.assertEqual(data[-1].cheque_count, 1)

		_columns, data = expired_not_returned({**filters, "as_on": day(6)})
		self.assertEqual(set(cheque_rows(data)), {expired.name, later.name})

		supplier = new_supplier()
		outgoing = given(supplier, 2000, expiry=-1)
		_columns, data = expired_not_returned(
			{"company": COMPANY, "party_type": "Supplier", "party": supplier}
		)
		self.assertEqual(list(cheque_rows(data)), [outgoing.name])


# ---------------------------------------------------------------------------
# Number cards and standard records
# ---------------------------------------------------------------------------


class TestSecurityCards(SecurityReportTestCase):
	@staticmethod
	def card(scope):
		return dashboard.get_security_card({"company": COMPANY, "scope": scope})["value"]

	def test_card_values(self):
		before = {scope: self.card(scope) for scope in ("held", "given", "expiring", "expired")}
		customer, supplier = new_customer(), new_supplier()
		held(customer, 1000, expiry=30, location=new_location())  # expiring: day 30 is inside the window
		held(customer, 2500, expiry=31)  # Received, not stored: still held
		held(customer, expiry=10, amount_type="Open / Blank")  # counted, never summed
		given(supplier, 4000, expiry=-2)  # expired, not returned
		returned = held(customer, 9999, expiry=-5)
		returned.return_cheque(return_date=day(-1), reason="Completed")

		self.assertEqual(flt(self.card("held") - before["held"], 2), 3500)
		self.assertEqual(flt(self.card("given") - before["given"], 2), 4000)
		self.assertEqual(self.card("expiring") - before["expiring"], 2)
		self.assertEqual(self.card("expired") - before["expired"], 1)

		result = dashboard.get_security_card({"company": COMPANY, "scope": "expiring"})
		self.assertEqual(result["route"], ["query-report", "Security Cheque Register"])
		self.assertEqual(result["route_options"]["expiry_to"], str(day(30)))
		self.assertRaises(frappe.ValidationError, dashboard.get_security_card, {"company": COMPANY, "scope": "x"})

	def test_standard_records_exist(self):
		for doctype, names in (
			(
				"Number Card",
				(
					"Security Held",
					"Security Given",
					"Security Cheques Expiring in 30 Days",
					"Expired Security Cheques Not Returned",
				),
			),
			(
				"Report",
				(
					"Security Cheque Register",
					"Security Exposure",
					"Security Cheques by Custody",
					"Expired Security Cheques Not Returned",
				),
			),
			("Print Format", ("Security Cheque Receipt",)),
			("DocType", ("Security Cheque", "Custody Location")),
		):
			for name in names:
				self.assertTrue(frappe.db.exists(doctype, name), f"{doctype} {name}")
		workspace = frappe.get_doc("Workspace", "Treasury")
		self.assertIn("Security Held", [c.number_card_name for c in workspace.number_cards])
		self.assertIn("Security Exposure", [link.link_to for link in workspace.links])


class TestSecurityReportPermissions(SecurityReportTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_company(OTHER_COMPANY, OTHER_ABBR)
		ensure_user(RESTRICTED, ["Treasury User"])
		add_user_permission(RESTRICTED, "Company", OTHER_COMPANY)

	def test_forbidden_company(self):
		company = {"company": COMPANY}
		with self.set_user(RESTRICTED):
			for report in (security_register, exposure, by_custody, expired_not_returned):
				self.assertRaises(frappe.PermissionError, report, company)
			self.assertRaises(
				frappe.PermissionError, dashboard.get_security_card, {**company, "scope": "held"}
			)

	def test_user_without_access(self):
		user = ensure_user("treasury-secrpt-noperm@example.com", [])
		with self.set_user(user):
			self.assertRaises(frappe.PermissionError, security_register, {"company": COMPANY})


# ---------------------------------------------------------------------------
# Expiry alerts
# ---------------------------------------------------------------------------


class TestSecurityExpiryAlerts(SecurityReportTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_user(MANAGER, ["Treasury Manager"])
		ensure_user(USER, ["Treasury User"])
		ensure_user(OUTSIDER, ["Accounts User"])

	def setUp(self):
		super().setUp()
		previous = frappe.db.get_single_value("Treasury Settings", "security_alert_days")
		frappe.db.set_single_value("Treasury Settings", "security_alert_days", 15)
		self.addCleanup(frappe.db.set_single_value, "Treasury Settings", "security_alert_days", previous)

	def test_digest_content_and_recipients(self):
		customer = new_customer()
		soon = held(customer, 1111, expiry=5)
		expired = held(customer, 2222, expiry=-3)
		later = held(customer, 3333, expiry=40)
		returned = held(customer, 4444, expiry=-8)
		returned.return_cheque(return_date=day(-1), reason="Completed")
		created = make_security_cheque(party=customer, amount=5555, issue_date=day(-60), expiry_date=day(2))
		outgoing = given(new_supplier(), 6666, expiry=15)  # the last day of the window

		result = scheduler.send_security_expiry_alerts(as_of=day(0))

		digest = result[COMPANY]
		for cheque in (soon, expired, outgoing):
			self.assertIn(cheque.name, digest["cheques"])
		for cheque in (later, returned, created):
			self.assertNotIn(cheque.name, digest["cheques"])
		self.assertIn(MANAGER, digest["recipients"])
		self.assertIn(USER, digest["recipients"])
		self.assertNotIn(OUTSIDER, digest["recipients"])

		content = frappe.get_all(
			"Notification Log",
			filters={"for_user": MANAGER, "document_type": DOCTYPE},
			pluck="email_content",
			order_by="creation desc",
			limit=1,
		)[0]
		for cheque in (soon, expired, outgoing):
			self.assertIn(cheque.cheque_no, content)
		for cheque in (later, returned, created):
			self.assertNotIn(cheque.cheque_no, content)
		# expired cheques come first, in their own group
		self.assertLess(content.index("Expired, Not Returned"), content.index(expired.cheque_no))
		self.assertLess(content.index(expired.cheque_no), content.index("Expiring Soon"))
		self.assertLess(content.index("Expiring Soon"), content.index(soon.cheque_no))
		self.assertFalse(
			frappe.db.exists("Notification Log", {"for_user": OUTSIDER, "document_type": DOCTYPE})
		)

	def test_open_amount_is_shown_as_open(self):
		customer = new_customer()
		cheque = held(customer, expiry=1, amount_type="Open / Blank")
		fields = ["name", "direction", "cheque_no", "party", "party_name", "purpose"]
		fields += ["amount_type", "amount", "currency", "expiry_date", "status", "custody_location"]
		row = frappe.db.get_value(DOCTYPE, cheque.name, fields, as_dict=True)
		html = scheduler.render_digest(COMPANY, [row], day(0))
		self.assertIn(cheque.cheque_no, html)
		self.assertIn("Open / Blank", html)
		self.assertIn("Expiring Soon", html)
		self.assertNotIn("Expired, Not Returned", html)

	# each test below uses its own as-of date: a recipient gets one digest per company and as-of date

	def test_second_run_the_same_day_sends_nothing(self):
		held(new_customer(), 1414, expiry=3)
		first = scheduler.send_security_expiry_alerts(as_of=day(1))
		second = scheduler.send_security_expiry_alerts(as_of=day(1))
		self.assertIn(MANAGER, first[COMPANY]["recipients"])
		self.assertEqual(second[COMPANY]["recipients"], [])

	def test_alert_days_setting(self):
		customer = new_customer()
		cheque = held(customer, 1515, expiry=12)
		frappe.db.set_single_value("Treasury Settings", "security_alert_days", 5)
		result = scheduler.send_security_expiry_alerts(as_of=day(2))
		self.assertNotIn(cheque.name, result.get(COMPANY, {}).get("cheques", []))
		frappe.db.set_single_value("Treasury Settings", "security_alert_days", 10)
		result = scheduler.send_security_expiry_alerts(as_of=day(2))
		self.assertIn(cheque.name, result[COMPANY]["cheques"])

	def test_email_only_when_outgoing_email_is_set_up(self):
		held(new_customer(), 1616, expiry=2)
		with (
			patch.object(scheduler.pdc_scheduler, "outgoing_email_enabled", return_value=True),
			patch("frappe.sendmail") as sendmail,
		):
			result = scheduler.send_security_expiry_alerts(as_of=day(-1))
		emailed = [email for call in sendmail.call_args_list for email in call.kwargs["recipients"]]
		self.assertEqual(sorted(emailed), sorted(result[COMPANY]["recipients"]))

		held(new_customer(), 1717, expiry=2)
		with (
			patch.object(scheduler.pdc_scheduler, "outgoing_email_enabled", return_value=False),
			patch("frappe.sendmail") as sendmail,
		):
			result = scheduler.send_security_expiry_alerts(as_of=day(-2))
		sendmail.assert_not_called()
		self.assertIn(MANAGER, result[COMPANY]["recipients"])

	def test_no_digest_without_expiring_cheques(self):
		frappe.db.set_single_value("Treasury Settings", "security_alert_days", 0)
		result = scheduler.send_security_expiry_alerts(as_of=day(-3650))
		self.assertNotIn(COMPANY, result)
