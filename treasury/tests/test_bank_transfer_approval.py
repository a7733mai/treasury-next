"""Phase 7b: the approval threshold and the transfer limit actions (spec 7b §3.2 before_submit, §3.3
check_approval, §7 D)."""

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, getdate, today

from treasury.bank_transfers.scheduler import post_scheduled_transfers
from treasury.tests.utils import (
	TRANSFER_SETTINGS,
	TreasuryTestMixin,
	bank_account_name,
	ensure_user,
	make_bank_transfer,
	make_iban,
	set_transfer_limits,
)

USER = "treasury-transfer-approval-user@example.com"
MANAGER = "treasury-transfer-approval-manager@example.com"


class TestBankTransferApproval(TreasuryTestMixin, IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_user(USER, ["Treasury User"])
		ensure_user(MANAGER, ["Treasury Manager"])

	def setUp(self):
		super().setUp()
		self.use_company_settings(**{**TRANSFER_SETTINGS, "transfer_approval_threshold": 100000})

	def submit_as(self, user, **values):
		with self.set_user(user):
			doc = make_bank_transfer(submit=False, **values)
			doc.submit()
		return doc

	def test_threshold(self):
		with self.assertRaisesRegex(frappe.PermissionError, "Treasury Manager"):
			self.submit_as(USER, amount=150000)
		self.assertEqual(self.submit_as(USER, amount=100000).status, "Sent")  # not above
		self.assertEqual(self.submit_as(MANAGER, amount=150000).status, "Sent")

		self.use_company_settings(transfer_approval_threshold=0)
		self.assertEqual(self.submit_as(USER, amount=150000).status, "Sent")

	def test_threshold_in_company_currency(self):
		usd = self.data.usd
		with self.assertRaises(frappe.PermissionError):
			self.submit_as(USER, amount=2500, bank_account=usd.bank_account)  # 125,000 at 50
		self.assertEqual(self.submit_as(USER, amount=1500, bank_account=usd.bank_account).status, "Sent")

	def test_scheduled_transfer_is_posted_without_a_new_check(self):
		execution = getdate(add_days(today(), 3))
		transfer = self.submit_as(MANAGER, amount=150000, execution_date=execution)
		self.assertEqual(transfer.status, "Scheduled")
		with self.set_user(USER):
			result = post_scheduled_transfers(as_of=execution)
		self.assertIn(transfer.name, result["posted"])
		transfer.reload()
		self.assertEqual(transfer.status, "Sent")

	def test_limit_actions(self):
		self.use_company_settings(transfer_approval_threshold=0)
		day = getdate(add_days(today(), -1))
		limit = {"bank_account": bank_account_name("EGP"), "method": "Fawran"}
		set_transfer_limits(self, [{**limit, "per_transaction_limit": 50000, "daily_limit": 100000}])
		fawran = {"method": "Fawran", "execution_date": day, "beneficiary_iban": make_iban("OTHB")}
		cases = {
			"per transaction": [60000],
			"daily": [45000, 45000, 20000],
		}
		for action in ("Warn", "Block"):
			for case, amounts in cases.items():
				with self.subTest(action=action, case=case):
					self.use_company_settings(transfer_limit_action=action)
					# a day of its own for every case
					day = getdate(add_days(day, -1))
					for amount in amounts[:-1]:
						make_bank_transfer(amount=amount, **{**fawran, "execution_date": day})
					frappe.clear_messages()
					if action == "Warn":
						doc = make_bank_transfer(amount=amounts[-1], **{**fawran, "execution_date": day})
						self.assertEqual(doc.status, "Sent")
						messages = [str(m) for m in frappe.message_log]
						self.assertTrue(any("limit" in m for m in messages), messages)
					else:
						self.assertRaisesRegex(
							frappe.ValidationError,
							"limit",
							make_bank_transfer,
							amount=amounts[-1],
							**{**fawran, "execution_date": day},
						)
