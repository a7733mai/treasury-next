"""Letters of Guarantee (Phase 5b, PLAN.md §8): issue, extension, reduction, release, claim, expiry, alerts,
utilisation, limits, permissions and the LG Register.

Fixed dates in 2025. Every test creates its own facility on its own bank account (`make_own_bank`), so the
utilisation and the bank balance it checks are its own. Facility: total 1,000,000; Letter of Guarantee
sub-limit 500,000 with a 10% cash margin, a 1% commission and a liability account for bank-funded claims.
"""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import getdate

from treasury.bank_facilities import dashboard, scheduler
from treasury.bank_facilities.report.facility_utilisation.facility_utilisation import (
	execute as utilisation_report,
)
from treasury.bank_facilities.report.lg_register.lg_register import execute as lg_register
from treasury.bank_facilities.utilisation import get_utilisation
from treasury.tests.utils import (
	COMPANY,
	CUSTOMER,
	TreasuryTestMixin,
	assert_gl_entries,
	ensure_facility_accounts,
	ensure_instrument_accounts,
	ensure_user,
	get_account_balance,
	get_voucher_count,
	make_bank_facility,
	make_drawdown,
	make_letter_of_guarantee,
	make_own_bank,
)

D = getdate
DOCTYPE = "Letter of Guarantee"
LIMIT = "Letter of Guarantee"
MANAGER = "treasury-lg-manager@example.com"
USER = "treasury-lg-user@example.com"


class LGTestCase(TreasuryTestMixin, IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.accounts = ensure_facility_accounts()
		cls.inst = ensure_instrument_accounts()

	def setUp(self):
		super().setUp()
		self.use_company_settings(accrue_facility_interest=0)

	def facility(self, lg_limit=500_000, total=1_000_000, margin=10, commission=1, extra_limits=None):
		bank = make_own_bank()
		limits = [
			{
				"limit_type": LIMIT,
				"limit_amount": lg_limit,
				"cash_margin_percent": margin,
				"commission_rate": commission,
				"liability_account": self.inst.lg_liability,
			},
			*(extra_limits or []),
		]
		facility = make_bank_facility(total_limit=total, limits=limits, bank_account=bank.bank_account)
		return facility, bank

	def used(self, facility, as_on=None) -> float:
		rows = get_utilisation(facility.name, LIMIT, as_on).limits
		return rows[0].utilised

	def last_voucher(self, doc) -> str:
		doc.reload()
		return doc.status_log[-1].voucher_no


class TestLetterOfGuarantee(LGTestCase):
	def test_issue_posts_margin_and_commission_only(self):
		facility, bank = self.facility()
		lg = make_letter_of_guarantee(facility)
		lg.reload()
		self.assertEqual(
			(lg.status, lg.open_amount, lg.margin_held, lg.cash_margin_amount, lg.commission_amount),
			("Issued", 100_000, 10_000, 10_000, 1_000),
		)
		self.assertEqual((lg.cash_margin_percent, lg.commission_rate), (10, 1))  # from the sub-limit
		self.assertEqual(lg.commission_account, self.accounts.commission)  # the facility's
		self.assertEqual(D(lg.effective_expiry_date), D("2025-12-31"))
		date = D("2025-03-01")
		assert_gl_entries(
			self,
			"Journal Entry",
			lg.margin_journal,
			[(self.inst.lg_margin, 10_000, 0, None, date), (bank.bank_gl, 0, 10_000, None, date)],
		)
		assert_gl_entries(
			self,
			"Journal Entry",
			lg.commission_journal,
			[(self.accounts.commission, 1_000, 0, None, date), (bank.bank_gl, 0, 1_000, None, date)],
		)
		# the guaranteed amount itself is off-balance sheet: only the margin and the commission are booked
		self.assertEqual(get_voucher_count(DOCTYPE, lg.name), 2)
		self.assertEqual(get_account_balance(bank.bank_gl, date), -11_000)
		self.assertEqual([r.event for r in lg.status_log], ["Created", "Issued"])
		self.assertEqual((lg.status_log[-1].event_date, lg.status_log[-1].open_amount), (date, 100_000))
		self.assertEqual(self.used(facility), 100_000)
		self.assertEqual(get_utilisation(facility.name).utilised, 100_000)  # the total too
		self.assertEqual(self.used(facility, as_on="2025-02-28"), 0)

	def test_explicit_rates_and_no_margin(self):
		facility, bank = self.facility()
		lg = make_letter_of_guarantee(
			facility, cash_margin_percent=0, commission_rate=0, commission_amount=750
		)
		lg.reload()
		self.assertEqual((lg.cash_margin_amount, lg.margin_held, lg.commission_amount), (0, 0, 750))
		self.assertFalse(lg.margin_journal)
		assert_gl_entries(
			self,
			"Journal Entry",
			lg.commission_journal,
			[(self.accounts.commission, 750, 0), (bank.bank_gl, 0, 750)],
		)

	def test_extend_reduce_release(self):
		facility, bank = self.facility()
		lg = make_letter_of_guarantee(facility)

		lg.extend_validity(new_expiry_date="2026-06-30", extension_date="2025-05-01", extra_commission=500)
		lg.reload()
		self.assertEqual(
			(lg.status, D(lg.effective_expiry_date), D(lg.expiry_date)),
			("Extended", D("2026-06-30"), D("2025-12-31")),
		)
		date = D("2025-05-01")
		assert_gl_entries(
			self,
			"Journal Entry",
			lg.status_log[-1].voucher_no,
			[(self.accounts.commission, 500, 0, None, date), (bank.bank_gl, 0, 500, None, date)],
		)
		self.assertRaises(
			frappe.ValidationError, lg.extend_validity, new_expiry_date="2026-01-31"
		)  # not later

		lg.reduce(new_amount=60_000, reduction_date="2025-06-01")
		lg.reload()
		self.assertEqual((lg.status, lg.open_amount, lg.margin_held), ("Reduced", 60_000, 6_000))
		# 40% of the guarantee is released: 40% of the margin comes back
		date = D("2025-06-01")
		assert_gl_entries(
			self,
			"Journal Entry",
			lg.status_log[-1].voucher_no,
			[(bank.bank_gl, 4_000, 0, None, date), (self.inst.lg_margin, 0, 4_000, None, date)],
		)
		self.assertEqual(self.used(facility), 60_000)
		self.assertRaises(frappe.ValidationError, lg.reduce, new_amount=70_000)  # not lower
		self.assertRaises(frappe.ValidationError, lg.reduce, new_amount=50_000, reduction_date="2025-05-15")

		lg.release(release_date="2025-09-01")
		lg.reload()
		self.assertEqual(
			(lg.status, lg.open_amount, lg.margin_held, D(lg.release_date)),
			("Released", 0, 0, D("2025-09-01")),
		)
		assert_gl_entries(
			self,
			"Journal Entry",
			lg.status_log[-1].voucher_no,
			[(bank.bank_gl, 6_000, 0), (self.inst.lg_margin, 0, 6_000)],
		)
		self.assertEqual(self.used(facility), 0)
		# the whole margin came back: the bank is out of the two commissions only
		self.assertEqual(get_account_balance(bank.bank_gl, "2025-09-01"), -1_500)
		# utilisation on past dates, from the status log
		self.assertEqual(self.used(facility, as_on="2025-05-31"), 100_000)
		self.assertEqual(self.used(facility, as_on="2025-08-31"), 60_000)

		# idempotency: released once, nothing more to release
		count = get_voucher_count(DOCTYPE, lg.name)
		self.assertRaises(frappe.ValidationError, lg.release, release_date="2025-09-02")
		self.assertEqual(get_voucher_count(DOCTYPE, lg.name), count)
		self.assertEqual(
			[r.event for r in lg.status_log], ["Created", "Issued", "Extended", "Reduced", "Released"]
		)

	def test_claim_uses_the_margin_first_then_the_facility(self):
		facility, bank = self.facility()
		lg = make_letter_of_guarantee(facility)
		lg.claim(amount=30_000, counter_account=self.inst.claims_expense, claim_date="2025-04-01")
		lg.reload()
		self.assertEqual(
			(lg.status, lg.open_amount, lg.margin_held, lg.claim_amount, lg.claim_outstanding),
			("Claimed", 0, 0, 30_000, 20_000),
		)
		date = D("2025-04-01")
		assert_gl_entries(
			self,
			"Journal Entry",
			lg.status_log[-1].voucher_no,
			[
				(self.inst.claims_expense, 30_000, 0, None, date),
				(self.inst.lg_margin, 0, 10_000, None, date),
				(self.inst.lg_liability, 0, 20_000, None, date),
			],
		)
		# the bank-funded part is a facility liability until repaid
		self.assertEqual(self.used(facility), 20_000)

		lg.repay_claim(amount=5_000, repayment_date="2025-05-01")
		lg.reload()
		self.assertEqual(lg.claim_outstanding, 15_000)
		assert_gl_entries(
			self,
			"Journal Entry",
			lg.status_log[-1].voucher_no,
			[(self.inst.lg_liability, 5_000, 0), (bank.bank_gl, 0, 5_000)],
		)
		self.assertEqual(self.used(facility), 15_000)
		self.assertEqual(self.used(facility, as_on="2025-04-30"), 20_000)
		self.assertRaises(frappe.ValidationError, lg.repay_claim, amount=20_000)  # more than owed
		self.assertRaises(frappe.ValidationError, lg.cancel)  # claimed

	def test_claim_below_the_margin_returns_the_rest(self):
		facility, bank = self.facility()
		lg = make_letter_of_guarantee(facility)
		receivable = self.data.receivable
		# a Receivable account needs its party
		self.assertRaises(
			frappe.ValidationError,
			lg.claim,
			amount=4_000,
			counter_account=receivable,
			claim_date="2025-04-01",
		)
		lg.claim(
			amount=4_000,
			counter_account=receivable,
			party_type="Customer",
			party=CUSTOMER,
			claim_date="2025-04-01",
		)
		lg.reload()
		self.assertEqual((lg.claim_outstanding, lg.margin_held), (0, 0))
		assert_gl_entries(
			self,
			"Journal Entry",
			lg.status_log[-1].voucher_no,
			[
				(receivable, 4_000, 0, CUSTOMER),
				(self.inst.lg_margin, 0, 10_000, None),
				(bank.bank_gl, 6_000, 0, None),
			],
		)
		self.assertEqual(self.used(facility), 0)
		self.assertRaises(frappe.ValidationError, lg.repay_claim)  # nothing owed

	def test_claim_above_the_open_amount_and_the_liability_account(self):
		facility, bank = self.facility()
		lg = make_letter_of_guarantee(facility)
		self.assertRaises(
			frappe.ValidationError, lg.claim, amount=100_001, counter_account=self.inst.claims_expense
		)
		# the claim is not debited to a bank / cash account nor to the guarantee's own margin / liability
		for account in (bank.bank_gl, self.inst.lg_margin, self.inst.lg_liability):
			with self.subTest(account=account):
				self.assertRaises(
					frappe.ValidationError,
					lg.claim,
					amount=5_000,
					counter_account=account,
					claim_date="2025-04-01",
				)
		lg.reload()
		self.assertEqual(lg.status, "Issued")
		# no liability account on the sub-limit: a claim beyond the margin cannot be booked
		other, _bank = self.facility()
		frappe.db.set_value(
			"Bank Facility Limit", {"parent": other.name, "limit_type": LIMIT}, "liability_account", None
		)
		lg2 = make_letter_of_guarantee(other)
		self.assertRaises(
			frappe.ValidationError,
			lg2.claim,
			amount=50_000,
			counter_account=self.inst.claims_expense,
			claim_date="2025-04-01",
		)
		lg2.reload()
		self.assertEqual(lg2.status, "Issued")

	def test_limit_exceeded(self):
		facility, _bank = self.facility(lg_limit=150_000)
		make_letter_of_guarantee(facility, amount=100_000)
		self.assertRaises(frappe.ValidationError, make_letter_of_guarantee, facility, amount=60_000)
		make_letter_of_guarantee(facility, amount=50_000)  # exactly the rest

		# the total limit is shared with the other sub-limits
		shared, _bank = self.facility(
			lg_limit=150_000,
			total=150_000,
			extra_limits=[{"limit_type": "Short Term Loan", "limit_amount": 150_000, "interest_rate": 12}],
		)
		make_drawdown(shared, amount=120_000, bank_account=shared.default_bank_account)
		self.assertRaises(frappe.ValidationError, make_letter_of_guarantee, shared, amount=40_000)
		make_letter_of_guarantee(shared, amount=30_000)

	def test_facility_must_be_active_and_cover_the_date(self):
		facility, _bank = self.facility()
		self.assertRaises(
			frappe.ValidationError,
			make_letter_of_guarantee,
			facility,
			issue_date="2024-12-31",
			expiry_date="2025-06-30",
		)
		facility.suspend(suspension_date="2025-02-01", reason="Review")
		self.assertRaises(frappe.ValidationError, make_letter_of_guarantee, facility)

	def test_validations(self):
		facility, _bank = self.facility()
		cases = [
			dict(expiry_date="2025-02-01"),  # before the issue date
			dict(amount=0),
			dict(beneficiary_type="Other", beneficiary=None, beneficiary_name=None),
			dict(reference_doctype="Payment Entry", reference_name="X"),
			dict(margin_account=None),  # a margin needs its account
			dict(margin_account=self.data.egp.bank_gl),  # not an asset margin account (a bank)
			dict(cash_margin_percent=120),
		]
		for values in cases:
			with self.subTest(values=values):
				self.assertRaises(
					frappe.ValidationError, make_letter_of_guarantee, facility, submit=False, **values
				)
		# a facility without an LG sub-limit
		loan_only = make_bank_facility()
		self.assertRaises(frappe.ValidationError, make_letter_of_guarantee, loan_only, submit=False)
		other = make_letter_of_guarantee(
			facility, submit=False, beneficiary_type="Other", beneficiary=None, beneficiary_name="Ministry"
		)
		self.assertEqual(other.beneficiary_name, "Ministry")

	def test_cancel_undoes_every_voucher(self):
		facility, bank = self.facility()
		lg = make_letter_of_guarantee(facility)
		lg.extend_validity(new_expiry_date="2026-03-31", extension_date="2025-04-01", extra_commission=100)
		lg.reload()
		lg.cancel()
		lg.reload()
		self.assertEqual((lg.status, lg.open_amount), ("Cancelled", 0))
		self.assertEqual(get_voucher_count(DOCTYPE, lg.name, docstatus=1), 0)
		self.assertEqual(get_account_balance(bank.bank_gl, "2025-12-31"), 0)
		self.assertEqual(self.used(facility), 0)
		self.assertEqual(self.used(facility, as_on="2025-03-15"), 0)  # cancelled: never counted


class TestLGProtection(LGTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_user(MANAGER, ["Treasury Manager"])
		ensure_user(USER, ["Treasury User"])

	def test_status_and_log_change_only_through_actions(self):
		facility, _bank = self.facility()
		lg = make_letter_of_guarantee(facility)
		lg.reload()
		lg.status = "Released"
		self.assertRaises(frappe.ValidationError, lg.save)
		lg.reload()
		lg.open_amount = 1
		self.assertRaises(frappe.ValidationError, lg.save)
		lg.reload()
		lg.append("status_log", {"event": "Released", "to_status": "Released"})
		self.assertRaises(frappe.ValidationError, lg.save)

	def test_draft_cannot_carry_lifecycle_values(self):
		facility, _bank = self.facility()
		lg = make_letter_of_guarantee(facility, submit=False, open_amount=5, margin_held=7, status="Released")
		self.assertEqual((lg.status, lg.open_amount, lg.margin_held), ("Draft", 0, 0))
		self.assertEqual([r.event for r in lg.status_log], ["Created"])

	def test_permissions(self):
		facility, _bank = self.facility()
		lg = make_letter_of_guarantee(facility)
		with self.set_user(USER):
			draft = make_letter_of_guarantee(facility, submit=False)  # a Treasury User prepares it
			self.assertRaises(frappe.PermissionError, draft.submit)
			doc = frappe.get_doc(DOCTYPE, lg.name)
			self.assertRaises(frappe.PermissionError, doc.release, release_date="2025-04-01")
		with self.set_user(MANAGER):
			frappe.get_doc(DOCTYPE, lg.name).release(release_date="2025-04-01")
		lg.reload()
		self.assertEqual(lg.status, "Released")
		self.assertEqual(lg.status_log[-1].user, MANAGER)


class TestLGJobsAndReports(LGTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_user(MANAGER, ["Treasury Manager"])

	def setUp(self):
		super().setUp()
		previous = frappe.db.get_single_value("Treasury Settings", "facility_alert_days")
		frappe.db.set_single_value("Treasury Settings", "facility_alert_days", 7)
		self.addCleanup(frappe.db.set_single_value, "Treasury Settings", "facility_alert_days", previous)

	def test_expiry_job_and_alerts(self):
		facility, _bank = self.facility()
		lg = make_letter_of_guarantee(facility, expiry_date="2025-05-31")
		# expiring within the alert days
		result = scheduler.send_facility_alerts(as_of="2025-05-26")
		self.assertIn(lg.name, result[COMPANY]["guarantees"])
		self.assertIn(MANAGER, result[COMPANY]["recipients"])
		content = frappe.get_all(
			"Notification Log",
			filters={"for_user": MANAGER, "document_type": "Facility Drawdown"},
			pluck="email_content",
			order_by="creation desc",
			limit=1,
		)[0]
		self.assertIn(lg.name, content)
		early = scheduler.send_facility_alerts(as_of="2025-05-20").get(COMPANY, {})
		self.assertNotIn(lg.name, early.get("guarantees", []))  # beyond the 7 alert days

		# valid through the expiry date: expired the day after, no accounting
		count = get_voucher_count(DOCTYPE, lg.name)
		self.assertNotIn(lg.name, scheduler.refresh_facilities(as_of="2025-05-31")["expired_instruments"])
		self.assertIn(lg.name, scheduler.refresh_facilities(as_of="2025-06-02")["expired_instruments"])
		lg.reload()
		self.assertEqual((lg.status, lg.margin_held), ("Expired", 10_000))
		self.assertEqual(lg.status_log[-1].event_date, D("2025-06-01"))
		self.assertEqual(get_voucher_count(DOCTYPE, lg.name), count)
		self.assertEqual(self.used(facility), 0)
		self.assertEqual(self.used(facility, as_on="2025-05-31"), 100_000)

		# expired with the margin still held: listed until released
		self.assertIn(lg.name, scheduler.send_facility_alerts(as_of="2025-06-10")[COMPANY]["guarantees"])
		lg.release(release_date="2025-06-15")
		result = scheduler.send_facility_alerts(as_of="2025-06-16").get(COMPANY, {})
		self.assertNotIn(lg.name, result.get("guarantees", []))

	def test_reports_and_card(self):
		def card():
			return dashboard.get_instrument_card({"company": COMPANY, "scope": "open_lgs"})["value"]

		before = card()
		facility, _bank = self.facility()
		lg = make_letter_of_guarantee(facility)
		released = make_letter_of_guarantee(facility, amount=20_000)
		released.release(release_date="2025-04-01")
		self.assertEqual(card() - before, 100_000)

		with patch("treasury.pdc.report_utils.today", return_value="2025-07-01"):
			filters = {"company": COMPANY, "facility": facility.name, "open_only": 1}
			_columns, data, _m, _c, summary = lg_register(filters)
			self.assertEqual([r.name for r in data], [lg.name])
			self.assertEqual(
				(data[0].open_amount, data[0].margin_held, data[0].days_to_expiry), (100_000, 10_000, 183)
			)
			cards = {c["label"]: c["value"] for c in summary}
			self.assertEqual(cards["Open Guarantees"], 100_000)
			_columns, data, _m, _c, _s = lg_register({"company": COMPANY, "facility": facility.name})
			self.assertEqual({r.name for r in data}, {lg.name, released.name})

			# Facility Utilisation shows the LG sub-limit through the utilisation hook
			_columns, data, _m, _c, _s = utilisation_report({"company": COMPANY, "facility": facility.name})
			row = next(r for r in data if r.get("limit_type") == LIMIT)
			self.assertEqual((row.utilised, row.available), (100_000, 400_000))
		self.assertRaises(
			frappe.ValidationError, dashboard.get_instrument_card, {"company": COMPANY, "scope": "x"}
		)
