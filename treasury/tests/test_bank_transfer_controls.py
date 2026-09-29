"""Phase 7b fix round 1: the money controls of Bank Transfers, enforced on the server and race-safe.

- approval threshold: amount + fee (an overridden fee as entered) at the higher of the transfer's and the system
  exchange rate; no system rate → manager only (review Major 2, security Major 1 / Minor 3);
- transfer limits: every path that posts or moves a transfer (submit, Send Now, Reschedule, the daily job)
  locks the Bank Account row, then counts with a locking read (review Majors 1 / 3, security Major 2);
- form helpers: company permission, a party's bank details only for a user who may read the party (review
  Major 4 / Minor 7, security Minors 4 / 5);
- make_bank_transfer rejects an unsupported currency pair; a return date cannot be in the future (review
  Minors 6 / 8).

IntegrationTestCase rolls back only at the end of a class: every class keeps to its own days / data.
"""

from unittest.mock import Mock, patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, getdate, today

from treasury.bank_transfers import fees, lifecycle
from treasury.bank_transfers.api import get_fee_preview, get_transfer_settings
from treasury.bank_transfers.api import make_bank_transfer as build_transfer
from treasury.bank_transfers.scheduler import post_scheduled_transfers
from treasury.tests.utils import (
	COMPANY,
	OTHER_ABBR,
	OTHER_COMPANY,
	SUPPLIER,
	TRANSFER_SETTINGS,
	USD_RATE,
	TreasuryTestMixin,
	add_user_permission,
	bank_account_name,
	ensure_company,
	ensure_party_bank_account,
	ensure_user,
	fee_rule,
	get_pe,
	get_transfer,
	get_voucher_count,
	make_bank_transfer,
	make_iban,
	make_usd_purchase_invoice,
	set_fee_rules,
	set_transfer_limits,
)

DOCTYPE = "Bank Transfer"
USER = "treasury-bt-controls-user@example.com"
MANAGER = "treasury-bt-controls-manager@example.com"
# may create Bank Transfers and read Suppliers
READER = "treasury-bt-controls-reader@example.com"
# as READER, but restricted to the other company
RESTRICTED = "treasury-bt-controls-restricted@example.com"
BENEFICIARY_KEYS = ("party_bank_account", "beneficiary_iban", "beneficiary_bank", "beneficiary_swift")


def day(offset: int):
	return getdate(add_days(today(), offset))


class ControlsTestCase(TreasuryTestMixin, IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_user(USER, ["Treasury User"])
		ensure_user(MANAGER, ["Treasury Manager"])
		ensure_user(READER, ["Treasury User", "Purchase User"])
		cls.b1 = bank_account_name("EGP")
		cls.bu = bank_account_name("USD")

	def setUp(self):
		super().setUp()
		self.use_company_settings(**TRANSFER_SETTINGS)
		set_fee_rules(self, [])
		set_transfer_limits(self, [])


# ---------------------------------------------------------------------------
# Approval threshold
# ---------------------------------------------------------------------------


class TestApprovalAmount(ControlsTestCase):
	def setUp(self):
		super().setUp()
		self.use_company_settings(transfer_approval_threshold=100000)

	def test_low_rate_does_not_bypass_the_threshold(self):
		"""Review Major 2 / security Major 1: 2,500 USD typed at 0.5 is 125,000 EGP at the system rate."""
		with self.set_user(USER):
			doc = make_bank_transfer(submit=False, bank_account=self.bu, amount=2500, exchange_rate=0.5)
			self.assertEqual((doc.exchange_rate, doc.base_amount), (0.5, 1250))  # the rate is kept
			self.assertEqual(lifecycle.approval_amount(doc), 2500 * USD_RATE)
			with self.assertRaisesRegex(frappe.PermissionError, "Treasury Manager"):
				doc.submit()
		self.assertEqual(get_transfer(doc.name).docstatus, 0)
		self.assertEqual(get_voucher_count(DOCTYPE, doc.name), 0)
		# a manager may submit it
		with self.set_user(MANAGER):
			lifecycle.check_approval(get_transfer(doc.name))

	def test_higher_user_rate_counts(self):
		# 1,800 USD: 90,000 at the system rate, 108,000 at the transfer's own rate of 60
		with self.set_user(USER):
			doc = make_bank_transfer(submit=False, bank_account=self.bu, amount=1800, exchange_rate=60)
			self.assertEqual(lifecycle.approval_amount(doc), 108000)
			self.assertRaises(frappe.PermissionError, doc.submit)

	def test_fee_counts(self):
		"""Security Minor 3: what leaves the bank is compared, fee included."""
		set_fee_rules(self, [fee_rule(self.b1, "Normal", fixed_fee=30)])
		with self.set_user(USER):
			above = make_bank_transfer(submit=False, amount=99980)
			self.assertEqual((above.fee_amount, lifecycle.approval_amount(above)), (30, 100010))
			self.assertRaises(frappe.PermissionError, above.submit)
			at = make_bank_transfer(submit=False, amount=99970)  # 100,000: not above
			at.submit()
		self.assertEqual(get_transfer(at.name).status, "Sent")

	def test_overridden_fee_counts(self):
		values = {"override_fee": 1, "fee_amount": 500, "fee_override_reason": "Urgent correspondent charge"}
		with self.set_user(USER):
			doc = make_bank_transfer(submit=False, amount=99600, **values)
			self.assertEqual(lifecycle.approval_amount(doc), 100100)
			self.assertRaisesRegex(frappe.PermissionError, "Treasury Manager", doc.submit)

	def test_receipt_and_internal(self):
		set_fee_rules(self, [fee_rule(self.b1, "Normal", direction="Incoming", scope="Any", fixed_fee=5)])
		receipt = make_bank_transfer(submit=False, transfer_type="Party Receipt", amount=100000)
		self.assertEqual(receipt.fee_amount, 5)
		# the amount the party pays; the incoming fee does not add to it
		self.assertEqual(lifecycle.approval_amount(receipt), 100000)
		self.assertFalse(lifecycle.approval_required(receipt))

		# into a company-currency account: at least the received amount
		internal = make_bank_transfer(
			submit=False,
			transfer_type="Internal Transfer",
			bank_account=self.bu,
			to_bank_account=self.b1,
			amount=1000,
			received_amount=120000,
		)
		self.assertEqual(lifecycle.approval_amount(internal), 120000)
		self.assertTrue(lifecycle.approval_required(internal))

	def test_no_system_rate(self):
		doc = make_bank_transfer(submit=False, bank_account=self.bu, amount=10, exchange_rate=50)
		with patch("treasury.bank_transfers.lifecycle.get_exchange_rate", return_value=None):
			self.assertIsNone(lifecycle.approval_amount(doc))
			with self.set_user(USER):
				self.assertRaisesRegex(
					frappe.PermissionError, "no exchange rate", lifecycle.check_approval, doc
				)
			with self.set_user(MANAGER):
				lifecycle.check_approval(doc)  # a manager may submit it
		# without a threshold there is no rule at all
		self.use_company_settings(transfer_approval_threshold=0)
		with patch("treasury.bank_transfers.lifecycle.get_exchange_rate", return_value=None):
			with self.set_user(USER):
				lifecycle.check_approval(doc)


# ---------------------------------------------------------------------------
# Transfer limits: locking and every path
# ---------------------------------------------------------------------------


class TestLimitLocking(ControlsTestCase):
	def test_locking_read(self):
		"""Review Major 1: the count of an enforcing path is a locking read (SELECT ... FOR UPDATE)."""
		transfer = frappe._dict(bank_account=self.b1, method="Fawran", name=None)
		self.assertIn("FOR UPDATE", str(fees.used_query(transfer, today(), lock=True)).upper())
		self.assertNotIn("FOR UPDATE", str(fees.used_query(transfer, today())).upper())

	def test_every_path_locks_then_counts(self):
		"""Submit, Reschedule, Send Now and the daily job lock the Bank Account row, then count with a locking
		read (review Majors 1 / 3, security Major 2)."""
		limit = {
			"bank_account": self.b1,
			"method": "Fawran",
			"per_transaction_limit": 0,
			"daily_limit": 1000000,
		}
		set_transfer_limits(self, [limit])
		fawran = {"method": "Fawran"}
		calls = Mock()
		with (
			patch.object(fees, "lock_bank_account", wraps=fees.lock_bank_account) as lock,
			patch.object(fees, "used_on", wraps=fees.used_on) as used,
		):
			calls.attach_mock(lock, "lock")
			calls.attach_mock(used, "used")

			def assert_locked_count(path):
				names = [c[0] for c in calls.mock_calls]
				self.assertIn("lock", names, path)
				first_lock = names.index("lock")
				locking = [
					i for i, c in enumerate(calls.mock_calls) if c[0] == "used" and c.kwargs.get("lock")
				]
				self.assertTrue(locking, f"{path}: no locking count")
				self.assertLess(first_lock, locking[0], f"{path}: counted before the lock")
				lock.assert_called_with(self.b1)
				calls.reset_mock()

			transfer = make_bank_transfer(amount=100, execution_date=day(3), **fawran)
			assert_locked_count("submit")
			transfer.reschedule(execution_date=day(4))
			assert_locked_count("reschedule")
			transfer.send_now()
			assert_locked_count("send now")

			scheduled = make_bank_transfer(amount=100, execution_date=day(5), **fawran)
			calls.reset_mock()
			result = post_scheduled_transfers(as_of=day(5))
			self.assertIn(scheduled.name, result["posted"])
			assert_locked_count("daily job")

	def test_no_lock_without_a_limit(self):
		with patch.object(fees, "lock_bank_account") as lock:
			make_bank_transfer(amount=100, method="Fawran", execution_date=day(-1))
		lock.assert_not_called()


class TestSendNowLimits(ControlsTestCase):
	def test_send_now_into_a_full_day(self):
		"""Review Major 3: Send Now runs the limit check of the send date (Block rejects, Warn warns)."""
		limit = {"bank_account": self.b1, "method": "Fawran", "per_transaction_limit": 0, "daily_limit": 1000}
		set_transfer_limits(self, [limit])
		self.use_company_settings(transfer_limit_action="Block")
		make_bank_transfer(amount=800, method="Fawran", execution_date=day(0))
		scheduled = make_bank_transfer(amount=800, method="Fawran", execution_date=day(3))  # its day is free
		self.assertEqual(scheduled.status, "Scheduled")

		with self.assertRaisesRegex(frappe.ValidationError, "daily"):
			scheduled.send_now()
		after = get_transfer(scheduled.name)
		self.assertEqual((after.status, getdate(after.execution_date)), ("Scheduled", day(3)))
		self.assertFalse(after.payment_entry)
		self.assertEqual(get_voucher_count(DOCTYPE, scheduled.name), 0)
		# nor on a past day that is full
		make_bank_transfer(amount=800, method="Fawran", execution_date=day(-1))
		self.assertRaisesRegex(frappe.ValidationError, "daily", scheduled.send_now, execution_date=day(-1))

		self.use_company_settings(transfer_limit_action="Warn")
		frappe.clear_messages()
		after.send_now()
		after.reload()
		self.assertEqual(after.status, "Sent")
		self.assertEqual(getdate(get_pe(after.payment_entry).posting_date), day(0))
		self.assertTrue(any("daily" in str(m) for m in frappe.message_log))

	def test_daily_job_respects_block(self):
		scheduled = make_bank_transfer(amount=800, method="Fawran", execution_date=day(6))
		# the limit is lowered after the submit
		limit = {"bank_account": self.b1, "method": "Fawran", "per_transaction_limit": 0, "daily_limit": 500}
		set_transfer_limits(self, [limit])
		self.use_company_settings(transfer_limit_action="Block")
		result = post_scheduled_transfers(as_of=day(6))
		self.assertIn(scheduled.name, result["failed"])
		self.assertEqual(get_transfer(scheduled.name).status, "Scheduled")

		self.use_company_settings(transfer_limit_action="Warn")
		result = post_scheduled_transfers(as_of=day(6))
		self.assertIn(scheduled.name, result["posted"])


# ---------------------------------------------------------------------------
# Form helpers: company and party permissions
# ---------------------------------------------------------------------------


class TestHelperPermissions(ControlsTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_company(OTHER_COMPANY, OTHER_ABBR)
		ensure_user(RESTRICTED, ["Treasury User", "Purchase User"])
		add_user_permission(RESTRICTED, "Company", OTHER_COMPANY)
		cls.iban = make_iban("OTHB")
		cls.party_account = ensure_party_bank_account("Supplier", SUPPLIER, iban=cls.iban)

	def preview(self, **values):
		args = {
			"company": COMPANY,
			"bank_account": self.b1,
			"method": "Normal",
			"transfer_type": "Party Payment",
			"amount": 1000,
			"party_type": "Supplier",
			"party": SUPPLIER,
			**values,
		}
		return get_fee_preview(**args)

	def test_preview_party_details_need_party_read(self):
		"""Review Major 4 / security Minor 5: no IBAN of a party the user cannot read."""
		with self.set_user(USER):  # Treasury User only: no read on Supplier
			r = self.preview()
		self.assertFalse(set(BENEFICIARY_KEYS) & set(r))
		with self.set_user(READER):
			r = self.preview()
		self.assertEqual((r["party_bank_account"], r["beneficiary_iban"]), (self.party_account, self.iban))

	def test_save_copies_party_details_only_for_a_reader(self):
		with self.set_user(USER):
			own_iban = make_iban("DOHB")
			doc = make_bank_transfer(submit=False, beneficiary_iban=own_iban)
			self.assertFalse(doc.party_bank_account)  # the default is not filled in
			chosen = make_bank_transfer(
				submit=False, beneficiary_iban=None, party_bank_account=self.party_account
			)
			self.assertEqual(chosen.party_bank_account, self.party_account)  # still checked against the party
			self.assertFalse(chosen.beneficiary_iban)  # but its IBAN is not copied
		with self.set_user(READER):
			doc = make_bank_transfer(submit=False, beneficiary_iban=None)
		self.assertEqual((doc.party_bank_account, doc.beneficiary_iban), (self.party_account, self.iban))

	def test_make_bank_transfer_party_details_need_party_read(self):
		with self.set_user(READER):
			values = build_transfer("Supplier", SUPPLIER, company=COMPANY)
		self.assertEqual(values["party_bank_account"], self.party_account)

	def test_company_permission(self):
		"""Review Minor 7 / security Minor 4: nothing of a company the user is not permitted."""
		with self.set_user(RESTRICTED):
			self.assertRaises(frappe.PermissionError, get_transfer_settings, COMPANY)
			self.assertRaises(frappe.PermissionError, self.preview)
			self.assertRaises(frappe.PermissionError, build_transfer, "Supplier", SUPPLIER, company=COMPANY)
			get_transfer_settings(OTHER_COMPANY)  # the user's own company
		with self.set_user(READER):
			self.assertEqual(get_transfer_settings(COMPANY)["company_currency"], "EGP")
			self.assertEqual(self.preview()["currency"], "EGP")


# ---------------------------------------------------------------------------
# make_bank_transfer currency pairs, return date
# ---------------------------------------------------------------------------


class TestCurrencyPairAndReturnDate(ControlsTestCase):
	def test_unsupported_currency_pair(self):
		"""Review Minor 6: a USD supplier's invoice with an EGP bank gets a clear error, not an amount of 0."""
		self.use_company_settings(default_normal_bank_account=self.b1)
		pi = make_usd_purchase_invoice(1000)
		with self.assertRaisesRegex(frappe.ValidationError, "cannot settle"):
			build_transfer("Purchase Invoice", pi.name)
		# with a USD bank it works
		self.use_company_settings(default_normal_bank_account=self.bu)
		self.assertEqual(build_transfer("Purchase Invoice", pi.name)["amount"], 1000)

	def test_return_date_not_in_the_future(self):
		"""Review Minor 8."""
		transfer = make_bank_transfer(amount=1000)
		self.assertEqual(transfer.status, "Sent")
		with self.assertRaisesRegex(frappe.ValidationError, "future"):
			transfer.mark_returned(return_date=day(1), reason="Returned by the bank")
		self.assertEqual(get_transfer(transfer.name).status, "Sent")
		transfer.mark_returned(return_date=day(0), reason="Returned by the bank")
		self.assertEqual(get_transfer(transfer.name).status, "Returned")
