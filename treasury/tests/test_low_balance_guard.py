"""Phase 7e: the entry-time low balance guard (spec 7e §3.5, §7 D).

E1 (treasury.tests.utils.make_alert_fixture_e1): minimum 20,000, closings T 100,000 · T+1 70,000 · T+2 80,000 ·
T+3 29,980 · T+4 29,980 · T+5 -10,020 · T+6 -10,020. An action is run like a request (`attempt`): when it
fails, everything it wrote is rolled back.
"""

from unittest.mock import patch

import frappe
from frappe import _
from frappe.tests import IntegrationTestCase
from frappe.utils import fmt_money, formatdate

from treasury.balance_alerts import guard
from treasury.bank_transfers import fees
from treasury.bank_transfers.scheduler import post_scheduled_transfers
from treasury.pdc.report.bank_position_forecast import bank_position_forecast
from treasury.tests.utils import (
	COMPANY,
	SUPPLIER,
	BalanceAlertTestMixin,
	alert_logs,
	ensure_alert_bank,
	ensure_charge_category,
	ensure_user,
	fund_bank,
	make_alert_fixture_e1,
	make_bank_charge,
	make_bank_facility,
	make_bank_transaction,
	make_bank_transfer,
	make_drawdown,
	make_pdc_cheque,
	make_repayment,
	make_security_cheque,
	set_thresholds,
	set_transfer_limits,
	unique_cheque_no,
)

TREASURY_USER = "treasury-guard-user@example.com"
TREASURY_MANAGER = "treasury-guard-mgr@example.com"
SAVEPOINT = "treasury_guard_test"


class TestLowBalanceGuard(BalanceAlertTestMixin, IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_user(TREASURY_USER, ["Treasury User"])
		ensure_user(TREASURY_MANAGER, ["Treasury Manager"])

	def setUp(self):
		super().setUp()
		frappe.clear_messages()
		self.e1 = make_alert_fixture_e1(self)
		self.bank = self.e1.bank_account

	# ------------------------------------------------------------------ helpers

	def attempt(self, fn, *args, **kwargs):
		"""Run an action like a web request: its writes are rolled back when it fails (then re-raised)."""
		frappe.db.savepoint(SAVEPOINT)
		try:
			result = fn(*args, **kwargs)
		except Exception:
			frappe.db.rollback(save_point=SAVEPOINT)
			raise
		frappe.db.release_savepoint(SAVEPOINT)
		return result

	def cheque(self, amount: float, due: int, bank_account: str | None = None):
		return make_pdc_cheque(
			amount=amount,
			issue_date=self.today,
			due_date=self.day(due),
			bank_account=bank_account or self.bank,
		)

	def warnings(self) -> list[str]:
		title = _("Low Projected Balance")
		return [m.get("message") for m in frappe.local.message_log if m.get("title") == title]

	def mode(self, mode: str):
		self.use_company_settings(on_low_balance=mode)

	# ------------------------------------------------------------------ PDC Cheque

	def test_warn(self):
		self.mode("Warn")
		c3 = self.cheque(15000, 4)
		c3.deliver(handover_date=self.today)
		c3.reload()
		self.assertEqual(c3.operational_status, "Delivered")
		(message,) = self.warnings()
		self.assertIn(formatdate(self.day(4)), message)
		self.assertIn(fmt_money(14980, currency="EGP"), message)
		self.assertIn(fmt_money(29980, currency="EGP"), message)  # before
		self.assertIn(fmt_money(20000, currency="EGP"), message)
		# T+5 / T+6 are lowered too (below the minimum already, but lower now)
		self.assertIn(formatdate(self.day(5)), message)
		self.assertIn(_("Low projected balance after {0} {1}:").format(_("PDC Cheque"), c3.name), message)
		indicators = [m.get("indicator") for m in frappe.local.message_log if m.get("message") == message]
		self.assertEqual(indicators, ["orange"])
		self.assertFalse(alert_logs(alert_type="Override"))

	def test_block_user(self):
		self.mode("Block")
		c3 = self.cheque(15000, 4)
		with self.set_user(TREASURY_USER):
			with self.assertRaises(guard.LowBalanceError) as raised:
				self.attempt(frappe.get_doc("PDC Cheque", c3.name).deliver, handover_date=self.today)
		self.assertIn(_("A Treasury Manager can override this block."), str(raised.exception))
		c3.reload()
		self.assertEqual((c3.operational_status, c3.accounting_status), ("Created", "Not Posted"))
		self.assertFalse(c3.party_payment_entry)
		self.assertFalse(frappe.db.exists("Payment Entry", {"treasury_source_name": c3.name}))

	def test_block_override_manager(self):
		self.mode("Block")
		c3 = self.cheque(15000, 4)
		with self.set_user(TREASURY_MANAGER):
			doc = frappe.get_doc("PDC Cheque", c3.name)
			# without the confirmation a manager is blocked too, with the hint to confirm
			with self.assertRaises(guard.LowBalanceError) as raised:
				self.attempt(doc.deliver, handover_date=self.today)
			self.assertIn(_("Confirm the override to continue."), str(raised.exception))
			self.attempt(doc.deliver, handover_date=self.today, allow_low_balance=1)
		c3.reload()
		self.assertEqual(c3.operational_status, "Delivered")
		row = c3.status_log[-1]
		self.assertEqual((row.event, row.user), ("Low Balance Override", TREASURY_MANAGER))
		self.assertIn(formatdate(self.day(4)), row.remarks)
		self.assertIn(fmt_money(14980, currency="EGP"), row.remarks)
		(log,) = alert_logs(alert_type="Override")
		self.assertEqual(
			(log.reference_doctype, log.reference_name, log.user, log.bank_account),
			("PDC Cheque", c3.name, TREASURY_MANAGER, self.bank),
		)
		self.assertEqual((log.projected_date, log.balance, log.threshold), (self.day(4), 14980, 20000))
		self.assertEqual(len(self.balance_jobs()), 1)

	def test_override_flag_by_user(self):
		self.mode("Block")
		c3 = self.cheque(100, 20)
		with self.set_user(TREASURY_USER):
			doc = frappe.get_doc("PDC Cheque", c3.name)
			self.assertRaises(
				frappe.PermissionError, doc.deliver, handover_date=self.today, allow_low_balance=1
			)
		c3.reload()
		self.assertEqual(c3.operational_status, "Created")

	def test_ignore(self):
		self.mode("Ignore")
		self.assertIsNone(guard.begin(COMPANY, [self.bank]))
		with patch.object(fees, "lock_bank_account") as lock:
			c3 = self.cheque(15000, 4)
			c3.deliver(handover_date=self.today)
		lock.assert_not_called()
		self.assertEqual(self.warnings(), [])
		c3.reload()
		self.assertEqual(c3.operational_status, "Delivered")

	def test_not_worse_not_blocked(self):
		self.mode("Block")
		# beyond the horizon: no projected day changes
		later = self.cheque(50000, 10)
		later.deliver(handover_date=self.today)
		later.reload()
		self.assertEqual(later.operational_status, "Delivered")
		self.assertEqual(self.warnings(), [])
		# the daily job sends TR1 on its own date: not guarded (a system action), nothing is lowered anyway
		with patch.object(guard, "evaluate", wraps=guard.evaluate) as evaluate:
			result = post_scheduled_transfers(as_of=self.day(3))
		self.assertIn(self.e1.tr1, result["posted"])
		evaluate.assert_not_called()
		self.assertEqual(frappe.db.get_value("Bank Transfer", self.e1.tr1, "status"), "Sent")

	def test_numbers_match_report(self):
		self.mode("Block")
		c3 = self.cheque(15000, 4)
		c3.deliver(handover_date=self.today, allow_low_balance=1)
		c3.reload()
		text = c3.status_log[-1].remarks
		_columns, data, *_rest = bank_position_forecast.execute(
			{
				"company": COMPANY,
				"bank_account": [self.bank],
				"from_date": self.today,
				"to_date": self.day(6),
				"periodicity": "Daily",
				"currency_view": "Bank Currency",
				"include_overdue": 1,
			}
		)
		closing = {r.row_id: r.closing for r in data if not r.get("indent")}
		self.assertEqual([closing["p4"], closing["p5"], closing["p6"]], [14980, -25020, -25020])
		for index in (4, 5, 6):
			amount = fmt_money(closing[f"p{index}"], currency="EGP")
			self.assertIn(f"{formatdate(self.day(index))} projected {amount}", text)

	# ------------------------------------------------------------------ Bank Transfer

	def test_bank_transfer(self):
		self.mode("Block")
		# a payment today of 60,000 + fee 20: T+1 falls to 9,980
		with self.assertRaises(guard.LowBalanceError):
			self.attempt(make_bank_transfer, amount=60000, bank_account=self.bank)
		self.assertFalse(
			frappe.db.exists("Bank Transfer", {"bank_account": self.bank, "amount": 60000, "docstatus": 1})
		)

		with self.set_user(TREASURY_MANAGER):
			transfer = self.attempt(
				make_bank_transfer, amount=60000, bank_account=self.bank, allow_low_balance=1
			)
		self.assertEqual(transfer.status, "Sent")
		self.assertTrue(transfer.payment_entry)
		self.assertEqual(transfer.status_log[-1].event, "Low Balance Override")
		self.assertTrue(
			frappe.db.exists(
				"Bank Transfer Status Log", {"parent": transfer.name, "event": "Low Balance Override"}
			)
		)
		(log,) = alert_logs(alert_type="Override", reference_name=transfer.name)
		self.assertEqual((log.reference_doctype, log.user), ("Bank Transfer", TREASURY_MANAGER))

	def test_bank_transfer_scheduled_and_actions(self):
		self.mode("Block")
		# Scheduled on T+2: T+2 falls to 19,980
		with self.assertRaises(guard.LowBalanceError):
			self.attempt(
				make_bank_transfer, amount=60000, bank_account=self.bank, execution_date=self.day(2)
			)

		# Send Now of TR1 (T+3 -> today): T+1 falls to 19,980
		tr1 = frappe.get_doc("Bank Transfer", self.e1.tr1)
		with self.assertRaises(guard.LowBalanceError):
			self.attempt(tr1.send_now, execution_date=self.today)
		self.assertEqual(frappe.db.get_value("Bank Transfer", tr1.name, "status"), "Scheduled")
		self.attempt(tr1.send_now, execution_date=self.today, allow_low_balance=1)
		tr1.reload()
		self.assertEqual((tr1.status, tr1.status_log[-1].event), ("Sent", "Low Balance Override"))

	def test_bank_transfer_reschedule_receipt(self):
		self.mode("Block")
		receipt = make_bank_transfer(
			transfer_type="Party Receipt", amount=30000, bank_account=self.bank, execution_date=self.day(2)
		)
		self.assertEqual(receipt.status, "Scheduled")
		# moving the inflow to T+6 lowers T+2 .. T+5: T+5 goes from 19,980 to -10,020
		with self.assertRaises(guard.LowBalanceError):
			self.attempt(receipt.reschedule, execution_date=self.day(6))
		self.assertEqual(frappe.db.get_value("Bank Transfer", receipt.name, "execution_date"), self.day(2))
		# moving it earlier raises every day: allowed
		receipt.reschedule(execution_date=self.day(1))
		self.assertEqual(frappe.db.get_value("Bank Transfer", receipt.name, "execution_date"), self.day(1))

	def test_internal_transfer_source_side_only(self):
		self.mode("Warn")
		target = ensure_alert_bank()
		set_thresholds(
			self,
			[
				{"bank_account": self.bank, "warning_balance": 50000, "minimum_balance": 20000},
				{"bank_account": target.bank_account, "warning_balance": 100, "minimum_balance": 100},
			],
		)
		make_bank_transfer(
			transfer_type="Internal Transfer",
			amount=60000,
			bank_account=self.bank,
			to_bank_account=target.bank_account,
		)
		(message,) = self.warnings()
		self.assertIn(f"{self.bank}: {formatdate(self.day(1))}", message)
		self.assertNotIn(f"{target.bank_account}:", message)

	def test_lock_order(self):
		self.mode("Warn")
		banks = {b.bank_account: b for b in (ensure_alert_bank(), ensure_alert_bank())}
		first, second = sorted(banks)
		fund_bank(banks[second], 5000, self.day(-1))
		set_thresholds(
			self,
			[
				{"bank_account": first, "warning_balance": 0, "minimum_balance": 0},
				{"bank_account": second, "warning_balance": 0, "minimum_balance": 0},
			],
		)
		limit = {"bank_account": second, "method": "Normal", "per_transaction_limit": 0, "daily_limit": 10**6}
		set_transfer_limits(self, [limit])
		calls = []
		real_lock, real_check = fees.lock_bank_account, fees.check_limits

		def lock(bank_account):
			calls.append(("lock", bank_account))
			return real_lock(bank_account)

		def check(*args, **kwargs):
			calls.append(("check", None))
			return real_check(*args, **kwargs)

		with (
			patch.object(fees, "lock_bank_account", side_effect=lock),
			patch.object(fees, "check_limits", side_effect=check),
		):
			# from the later name to the earlier one: the guard still locks in name order
			make_bank_transfer(
				transfer_type="Internal Transfer", amount=100, bank_account=second, to_bank_account=first
			)
		self.assertEqual(calls[:2], [("lock", first), ("lock", second)])
		self.assertLess(calls.index(("lock", second)), calls.index(("check", None)))

	# ------------------------------------------------------------------ Facility Repayment

	def test_facility_repayment(self):
		self.mode("Block")
		bank = ensure_alert_bank()
		facility = make_bank_facility(
			bank_account=bank.bank_account,
			start_date=self.day(-30),
			expiry_date=self.day(365),
			limits=[{"limit_type": "Short Term Loan", "limit_amount": 1_000_000, "interest_rate": 0}],
		)
		drawdown = make_drawdown(
			facility,
			amount=150000,
			drawdown_date=self.day(-1),
			tenor_months=2,
			frequency="Bullet",
			method="Bullet",
			first_repayment_date=self.day(60),
		)
		set_thresholds(
			self, [{"bank_account": bank.bank_account, "warning_balance": 70000, "minimum_balance": 70000}]
		)
		values = {"principal_paid": 90000, "interest_paid": 0, "prepayment": 1}
		with self.assertRaises(guard.LowBalanceError):
			self.attempt(make_repayment, drawdown, self.today, **values)
		self.assertFalse(frappe.db.exists("Facility Repayment", {"drawdown": drawdown.name, "docstatus": 1}))

		with self.set_user(TREASURY_MANAGER):
			repayment = self.attempt(make_repayment, drawdown, self.today, allow_low_balance=1, **values)
		self.assertEqual(repayment.docstatus, 1)
		comments = info_comments("Facility Repayment", repayment.name)
		self.assertEqual(len(comments), 1)
		self.assertIn(formatdate(self.today), comments[0])
		(log,) = alert_logs(alert_type="Override", reference_name=repayment.name)
		self.assertEqual(
			(log.reference_doctype, log.balance, log.threshold), ("Facility Repayment", 60000, 70000)
		)

	# ------------------------------------------------------------------ Bank Charge

	def test_bank_charge(self):
		self.mode("Block")
		ensure_charge_category()
		charge = {"bank_account": self.bank, "amount": 85000, "posting_date": self.today}
		# today falls to 15,000
		with self.assertRaises(guard.LowBalanceError):
			self.attempt(make_bank_charge, **charge)
		self.assertFalse(frappe.db.exists("Bank Charge", {"bank_account": self.bank, "docstatus": 1}))
		with self.set_user(TREASURY_MANAGER):
			doc = self.attempt(make_bank_charge, allow_low_balance=1, **charge)
		self.assertEqual(doc.status_log[-1].event, "Low Balance Override")
		self.assertTrue(alert_logs(alert_type="Override", reference_name=doc.name))

	def test_bank_charge_from_statement_only_warns(self):
		self.mode("Block")
		ensure_charge_category()
		transaction = make_bank_transaction(
			self.bank, self.today, withdrawal=85000, description="acct maint fee"
		)
		doc = make_bank_charge(
			bank_account=self.bank, amount=85000, posting_date=self.today, bank_transaction=transaction.name
		)
		self.assertEqual(doc.docstatus, 1)
		self.assertEqual(len(self.warnings()), 1)
		self.assertFalse(alert_logs(alert_type="Override"))

	def test_bank_charge_refund_not_guarded(self):
		ensure_charge_category()
		self.mode("Warn")
		charge = make_bank_charge(bank_account=self.bank, amount=100, posting_date=self.today)
		self.mode("Block")
		with patch.object(guard, "begin", wraps=guard.begin) as begin:
			refund = make_bank_charge(
				entry_type="Refund",
				against_bank_charge=charge.name,
				bank_account=self.bank,
				amount=100,
				posting_date=self.today,
			)
		self.assertEqual(refund.docstatus, 1)
		begin.assert_not_called()

	# ------------------------------------------------------------------ Cheque Batch

	def make_batch(self, **values):
		return frappe.get_doc(
			{
				"doctype": "Cheque Batch",
				"direction": "Outgoing",
				"company": COMPANY,
				"party_type": "Supplier",
				"party": SUPPLIER,
				"bank_account": self.bank,
				"issue_date": self.today,
				"purpose": "Rent",
				"cheques": [
					{"due_date": self.day(1), "amount": 30000, "cheque_no": unique_cheque_no()}
					for _i in range(3)
				],
				**values,
			}
		).insert()

	def test_batch_deliver_all_single_guard(self):
		self.mode("Block")
		batch = self.make_batch()
		batch.submit()
		names = frappe.get_all("PDC Cheque", filters={"cheque_batch": batch.name}, pluck="name")
		self.assertEqual(len(names), 3)

		with self.assertRaises(guard.LowBalanceError):
			self.attempt(batch.deliver_all, handover_date=self.today)
		self.assertEqual(batch_statuses(batch.name), ["Created"] * 3)

		self.enqueue.reset_mock()
		with (
			self.set_user(TREASURY_MANAGER),
			patch.object(guard, "evaluate", wraps=guard.evaluate) as evaluate,
		):
			doc = frappe.get_doc("Cheque Batch", batch.name)
			result = self.attempt(doc.deliver_all, handover_date=self.today, allow_low_balance=1)
		self.assertEqual(sorted(result["succeeded"]), sorted(names))
		# one pre-state and one post-state for the whole batch, not one per cheque
		self.assertEqual(evaluate.call_count, 2)
		self.assertEqual(len(self.balance_jobs()), 1)
		self.assertEqual(batch_statuses(batch.name), ["Delivered"] * 3)
		self.assertEqual(len(info_comments("Cheque Batch", batch.name)), 1)
		(log,) = alert_logs(alert_type="Override", reference_name=batch.name)
		self.assertEqual((log.reference_doctype, log.user), ("Cheque Batch", TREASURY_MANAGER))

	def test_batch_submit_with_handover(self):
		self.mode("Block")
		batch = self.make_batch(handover_date=self.today)
		with self.assertRaises(guard.LowBalanceError):
			self.attempt(batch.submit)
		self.assertEqual(frappe.db.get_value("Cheque Batch", batch.name, "docstatus"), 0)
		self.assertFalse(frappe.db.exists("PDC Cheque", {"cheque_batch": batch.name}))

		batch = frappe.get_doc("Cheque Batch", batch.name)
		batch.allow_low_balance = 1
		batch.save()
		self.attempt(batch.submit)
		self.assertEqual(batch_statuses(batch.name), ["Delivered"] * 3)
		self.assertTrue(alert_logs(alert_type="Override", reference_name=batch.name))

	# ------------------------------------------------------------------ Security Cheque

	def test_security_encash_outgoing(self):
		self.mode("Block")
		security = make_security_cheque(
			"Outgoing",
			amount=85000,
			bank_account=self.bank,
			issue_date=self.day(-10),
			expiry_date=self.day(300),
		)
		security.issue(handover_date=self.day(-9))
		with self.assertRaises(guard.LowBalanceError):
			self.attempt(security.encash, encashment_date=self.today)
		security.reload()
		self.assertEqual(security.status, "Issued")
		self.assertFalse(security.converted_pdc_cheque)
		self.assertFalse(frappe.db.exists("PDC Cheque", {"security_cheque": security.name}))

		result = self.attempt(security.encash, encashment_date=self.today, allow_low_balance=1)
		pdc = frappe.get_doc("PDC Cheque", result["pdc_cheque"])
		self.assertEqual(pdc.operational_status, "Delivered")
		self.assertEqual(pdc.status_log[-1].event, "Low Balance Override")

	# ------------------------------------------------------------------ banks without thresholds

	def test_no_thresholds_no_check(self):
		self.mode("Block")
		plain = ensure_alert_bank()
		with patch.object(fees, "lock_bank_account") as lock, patch.object(guard, "evaluate") as evaluate:
			cheque = self.cheque(1_000_000, 1, bank_account=plain.bank_account)
			cheque.deliver(handover_date=self.today)
		lock.assert_not_called()
		evaluate.assert_not_called()
		self.assertEqual(self.warnings(), [])
		self.assertEqual(frappe.db.get_value("PDC Cheque", cheque.name, "operational_status"), "Delivered")


def batch_statuses(batch: str) -> list[str]:
	return frappe.get_all("PDC Cheque", filters={"cheque_batch": batch}, pluck="operational_status")


def info_comments(doctype: str, name: str) -> list[str]:
	return frappe.get_all(
		"Comment",
		filters={"reference_doctype": doctype, "reference_name": name, "comment_type": "Info"},
		pluck="content",
	)
