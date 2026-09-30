"""Phase 7g (spec §7 D): PDC to Lender — installment cheques whose party posting is the repayment (spec §4.5,
§4.6): the batch mapper and its validation, no posting at delivery, one Payment Entry from PDC Payable per
installment (no double posting of the liability), bounce / represent / replace, the repayment invariants, the
forecast and the projected party statement.

The standard example: EGP 120,000 drawn on 2025-01-31, 12 monthly Equal Principal installments at 12 %
Actual/365; installment 1 (2025-02-28) = 10,000.00 + 1,104.66 = 11,104.66; installment 2 (2025-03-31) =
10,000.00 + 1,121.10. The cheques are drawn on a bank account of their own with its own PDC accounts
(`ensure_alert_bank`), each with a number of its own.
"""

import copy
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import getdate

from treasury.bank_facilities import facility_cheques, interest, lenders, repayment
from treasury.bank_facilities.utilisation import get_utilisation
from treasury.pdc import lifecycle
from treasury.pdc.report.bank_position_forecast.bank_position_forecast import BankPositionForecast
from treasury.pdc.report.projected_party_statement.projected_party_statement import ProjectedPartyStatement
from treasury.pdc.scheduler import post_due_party_postings
from treasury.tests.utils import (
	COMPANY,
	FACILITY_PE,
	PE_SETTINGS,
	SUPPLIER,
	TreasuryTestMixin,
	add_user_permission,
	assert_deductions,
	assert_gl_entries,
	ensure_alert_bank,
	ensure_facility_accounts,
	ensure_user,
	get_pe,
	liability_voucher_count,
	make_bank_facility,
	make_drawdown,
	make_installment_batch,
	make_repayment,
	set_bank_suppliers,
	unique_cheque_no,
)

D = getdate
CHEQUE = "PDC Cheque"
TREASURY_USER = "treasury-7g-cheques-user@example.com"
ACCOUNTS_USER = "treasury-7g-cheques-accounts@example.com"
# a Treasury User who reads one drawdown only (User Permission, test_batch_needs_read_on_the_drawdown)
RESTRICTED_USER = "treasury-7g-cheques-restricted@example.com"
DRAWDOWN = "Facility Drawdown"


class FacilityChequeTestCase(TreasuryTestMixin, IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.accounts = ensure_facility_accounts()
		ensure_user(TREASURY_USER, ["Treasury User"])
		ensure_user(ACCOUNTS_USER, ["Accounts User"])
		ensure_user(RESTRICTED_USER, ["Treasury User"])

	def setUp(self):
		super().setUp()
		self.use_company_settings(accrue_facility_interest=0, **PE_SETTINGS, **FACILITY_PE)
		set_bank_suppliers(self, [])

	def loan(self, **values):
		bank = ensure_alert_bank()
		facility = make_bank_facility(bank_account=bank.bank_account)
		drawdown = make_drawdown(
			facility, bank_account=bank.bank_account, repayment_mode="PDC to Lender", **values
		)
		return frappe._dict(bank=bank, facility=facility, drawdown=drawdown)

	@staticmethod
	def batch(drawdown, rows: list[int] | None = None, **values):
		"""The installment batch, limited to the installments `rows` (1-based) when given."""
		batch = make_installment_batch(drawdown, submit=False, **values)
		if rows:
			batch.set("cheques", [r for r in batch.cheques if r.installment_no in rows])
			batch.save()
		batch.submit()
		batch.reload()
		return batch

	@staticmethod
	def cheque(drawdown, installment_no: int):
		row = FacilityChequeTestCase.row(drawdown, installment_no)
		return frappe.get_doc(CHEQUE, row.pdc_cheque)

	@staticmethod
	def row(drawdown, installment_no: int) -> frappe._dict:
		name = drawdown if isinstance(drawdown, str) else drawdown.name
		return frappe.get_all(
			"Facility Repayment Schedule",
			filters={"parent": name, "installment_no": installment_no, "is_prepayment": 0},
			fields=["*"],
		)[0]

	@staticmethod
	def post(cheque, date=None) -> frappe._dict:
		lifecycle.post_party(cheque.name, date)
		cheque.reload()
		return cheque

	@staticmethod
	def cheque_repayments(cheque) -> list:
		return frappe.get_all(
			"Facility Repayment",
			filters={"pdc_cheque": cheque.name},
			fields=["name", "docstatus", "cheque_cycle", "payment_entry", "journal_entry", "settlement"],
			order_by="creation asc",
		)


class TestInstallmentBatch(FacilityChequeTestCase):
	def test_generate_batch_mapper(self):
		s = self.loan()
		data = facility_cheques.make_installment_batch(s.drawdown.name)
		rows = data["cheques"]
		self.assertEqual(len(rows), 12)
		schedule = sorted(s.drawdown.repayment_schedule, key=lambda r: r.installment_no)
		self.assertEqual(
			[(r["due_date"], r["amount"], r["installment_no"]) for r in rows],
			[(D(r.due_date), r.total, r.installment_no) for r in schedule],
		)
		lender = lenders.get_lender_supplier(s.facility.name)
		self.assertEqual(
			(data["party_type"], data["party"], data["direction"]), ("Supplier", lender, "Outgoing")
		)
		self.assertEqual(
			(data["allocation_method"], data["facility_drawdown"], data["bank_account"]),
			("Advance (no allocation)", s.drawdown.name, s.bank.bank_account),
		)
		# back-entered installments: issued and handed over on the drawdown date
		drawdown_date = D("2025-01-31")
		self.assertEqual((D(data["issue_date"]), D(data["handover_date"])), (drawdown_date, drawdown_date))

		# the installments with a live cheque are left out of the next batch
		self.batch(s.drawdown, rows=[1, 2, 3])
		data = facility_cheques.make_installment_batch(s.drawdown.name)
		self.assertEqual([r["installment_no"] for r in data["cheques"]], list(range(4, 13)))
		self.batch(s.drawdown)
		self.assertRaises(frappe.ValidationError, facility_cheques.make_installment_batch, s.drawdown.name)

		# refused for a Manual drawdown; PDC to Lender needs the company currency
		manual = make_drawdown(make_bank_facility(), repayment_mode="Manual")
		self.assertRaises(frappe.ValidationError, facility_cheques.make_installment_batch, manual.name)
		usd = make_bank_facility(currency="USD", total_limit=100_000)
		with self.assertRaises(frappe.ValidationError) as ctx:
			make_drawdown(usd, amount=1000, repayment_mode="PDC to Lender")
		self.assertIn("only supported in the company currency", str(ctx.exception))

	def test_batch_validation(self):
		s = self.loan()
		base = facility_cheques.make_installment_batch(s.drawdown.name)
		base.pop("name", None)
		for row in base["cheques"]:
			row.pop("name", None)
			row["cheque_no"] = unique_cheque_no()
		base["cheque_book"] = None

		def change_amount(d):
			d["cheques"][0]["amount"] += 1

		def change_date(d):
			d["cheques"][0]["due_date"] = "2025-03-01"

		def change_party(d):
			d["party"] = SUPPLIER

		def add_reference(d):
			d["cheques"][0].update(reference_doctype="Purchase Invoice", allocated_amount=1)

		def duplicate_row(d):
			d["cheques"].append({**d["cheques"][0], "cheque_no": unique_cheque_no()})

		def use_generator(d):
			d["gen_count"] = 3

		cases = (
			(change_amount, "the cheque must be"),
			(change_date, "the cheque must be"),
			(change_party, "must be made out to the lender"),
			(add_reference, "cannot allocate invoices"),
			(duplicate_row, "already has cheque"),
			(use_generator, "Generate Cheques is not available"),
		)
		for change, message in cases:
			with self.subTest(change=change.__name__):
				data = copy.deepcopy(base)
				change(data)
				with self.assertRaises(frappe.ValidationError) as ctx:
					frappe.get_doc(data).insert()
				self.assertIn(message, str(ctx.exception))
		# the generator of a draft installment batch is refused as well
		batch = frappe.get_doc(copy.deepcopy(base)).insert()
		batch.gen_count, batch.gen_first_due_date = 2, "2025-02-28"
		self.assertRaises(frappe.ValidationError, batch.generate_rows)

	def test_delivery_posts_nothing(self):
		for mode in ("On Handover", "On Creation"):
			with self.subTest(mode=mode):
				self.use_company_settings(outgoing_party_posting=mode)
				s = self.loan()
				batch = self.batch(s.drawdown, rows=[1, 2])
				for cheque in frappe.get_all(
					CHEQUE,
					filters={"cheque_batch": batch.name},
					fields=["name", "operational_status", "accounting_status", "facility_drawdown"],
				):
					self.assertEqual(
						(cheque.operational_status, cheque.accounting_status, cheque.facility_drawdown),
						("Delivered", "Not Posted", s.drawdown.name),
					)
				self.assertEqual(liability_voucher_count(self.accounts.loan_egp, s.drawdown.name), 0)
				self.assertEqual(self.row(s.drawdown, 1).pdc_cheque, self.cheque(s.drawdown, 1).name)
				self.assertEqual(batch.posting_mode, "On Cheque Date")

	def test_permissions(self):
		s = self.loan()
		with self.set_user(ACCOUNTS_USER):
			self.assertRaises(
				frappe.PermissionError, facility_cheques.make_installment_batch, s.drawdown.name
			)
		with self.set_user(TREASURY_USER):
			batch = self.batch(s.drawdown, rows=[1])
			self.assertEqual(batch.docstatus, 1)
		self.assertEqual(self.cheque(s.drawdown, 1).operational_status, "Delivered")


class TestInstallmentCheques(FacilityChequeTestCase):
	def test_party_posting_is_the_repayment(self):
		for voucher in ("Payment Entry", "Journal Entry"):
			with self.subTest(bank_settlement_voucher=voucher):
				self.use_company_settings(bank_settlement_voucher=voucher)
				s = self.loan()
				self.batch(s.drawdown, rows=[1])
				cheque = self.cheque(s.drawdown, 1)
				post_due_party_postings(as_of="2025-02-28")
				cheque.reload()
				date = D("2025-02-28")
				self.assertEqual(
					(cheque.accounting_status, cheque.party_posting_date), ("Party Posted", date)
				)
				pe = cheque.party_payment_entry
				assert_gl_entries(
					self,
					"Payment Entry",
					pe,
					[
						(self.accounts.loan_egp, 10_000, 0, None, date),
						(self.accounts.interest, 1104.66, 0, None, date),
						(s.bank.pdc_payable, 0, 11_104.66, None, date),
					],
				)
				assert_deductions(
					self,
					get_pe(pe),
					[(self.accounts.loan_egp, 10_000, 0), (self.accounts.interest, 1104.66, 0)],
				)
				self.assertEqual(frappe.db.count("Payment Ledger Entry", {"voucher_no": pe}), 0)
				self.assertEqual(
					frappe.db.get_value("Payment Entry", pe, ["treasury_source_doctype", "treasury_event"]),
					(CHEQUE, lifecycle.EVENT_PARTY),
				)
				repayments = self.cheque_repayments(cheque)
				self.assertEqual(len(repayments), 1)
				self.assertEqual(
					(repayments[0].settlement, repayments[0].payment_entry, repayments[0].journal_entry),
					("PDC Cheque", pe, None),
				)
				self.assertEqual(cheque.facility_repayment, repayments[0].name)
				row = self.row(s.drawdown, 1)
				self.assertEqual(
					(row.status, row.payment_voucher_type, row.payment_journal), ("Paid", "Payment Entry", pe)
				)
				self.assertEqual(get_utilisation(s.facility.name).utilised, 110_000)

				# the clearance moves PDC Payable to the bank; the liability is touched by one voucher only
				cheque.clear(clearance_date="2025-03-03")
				cheque.reload()
				bank_voucher = cheque.bank_payment_entry or cheque.bank_journal
				doctype = "Payment Entry" if cheque.bank_payment_entry else "Journal Entry"
				self.assertEqual(doctype, voucher)
				assert_gl_entries(
					self,
					doctype,
					bank_voucher,
					[(s.bank.pdc_payable, 11_104.66, 0), (s.bank.bank_gl, 0, 11_104.66)],
				)
				self.assertEqual(liability_voucher_count(self.accounts.loan_egp, s.drawdown.name), 1)

	def test_idempotent_posting(self):
		s = self.loan()
		self.batch(s.drawdown, rows=[1])
		cheque = self.post(self.cheque(s.drawdown, 1))
		pe = cheque.party_payment_entry
		with self.assertRaises(frappe.ValidationError) as ctx:
			lifecycle.post_party(cheque.name)
		self.assertIn("already posted", str(ctx.exception))
		self.assertEqual(facility_cheques.settle_by_cheque(cheque, "2025-02-28"), pe)
		self.assertEqual(len(self.cheque_repayments(cheque)), 1)

	def test_bounce_reopens_row(self):
		for on_bounce in ("Reverse by Journal Entry", "Cancel Payment Entry"):
			with self.subTest(on_bounce=on_bounce):
				self.use_company_settings(on_bounce=on_bounce)
				s = self.loan()
				self.batch(s.drawdown, rows=[1])
				cheque = self.post(self.cheque(s.drawdown, 1))
				pe = cheque.party_payment_entry
				cheque.bounce(bounce_date="2025-03-03", reason="Insufficient funds")
				cheque.reload()
				self.assertEqual(cheque.operational_status, "Bounced")
				self.assertIsNone(cheque.facility_repayment)
				self.assertEqual([r.docstatus for r in self.cheque_repayments(cheque)], [2])
				row = self.row(s.drawdown, 1)
				self.assertEqual((row.paid_principal, row.paid_interest, row.status), (0, 0, "Overdue"))
				self.assertEqual((row.payment_voucher_type, row.payment_journal), (None, None))
				self.assertEqual(get_utilisation(s.facility.name).utilised, 120_000)
				if on_bounce == "Cancel Payment Entry":
					self.assertEqual(frappe.db.get_value("Payment Entry", pe, "docstatus"), 2)
				else:
					assert_gl_entries(
						self,
						"Journal Entry",
						cheque.reversal_journal,
						[
							(s.bank.pdc_payable, 11_104.66, 0),
							(self.accounts.loan_egp, 0, 10_000),
							(self.accounts.interest, 0, 1104.66),
						],
					)

	def test_bounce_does_not_double_count_the_accrual(self):
		self.use_company_settings(accrue_facility_interest=1)
		s = self.loan()
		interest.accrue_drawdown(s.drawdown.name, "2025-03-01")  # February: 1,104.66
		self.batch(s.drawdown, rows=[1])
		cheque = self.post(self.cheque(s.drawdown, 1))
		repayment_doc = frappe.get_doc("Facility Repayment", cheque.facility_repayment)
		self.assertEqual(repayment_doc.accrued_interest_cleared, 1104.66)
		cheque.bounce(bounce_date="2025-03-03", reason="Insufficient funds")
		s.drawdown.reload()
		self.assertEqual(interest.accrued_balance(s.drawdown, "2025-03-03"), 1104.66)
		# March accrues its own interest only (110,000 x 12 % x 31 / 365)
		posted = interest.accrue_drawdown(s.drawdown.name, "2025-04-01")
		self.assertEqual(len(posted), 1)
		self.assertEqual(frappe.db.get_value("Journal Entry", posted[0], "total_debit"), 1121.10)

	def test_bounce_out_of_order(self):
		s = self.loan()
		self.batch(s.drawdown, rows=[1, 2])
		first = self.post(self.cheque(s.drawdown, 1))
		self.post(self.cheque(s.drawdown, 2))
		first.bounce(bounce_date="2025-04-02", reason="Stopped")
		self.assertEqual(self.row(s.drawdown, 1).status, "Overdue")
		self.assertEqual(self.row(s.drawdown, 2).status, "Paid")
		self.assertEqual(get_utilisation(s.facility.name).utilised, 110_000)

	def test_represent_and_replace(self):
		s = self.loan()
		self.batch(s.drawdown, rows=[1, 2, 3])
		# represent: a new cycle, posted on the new due date
		cheque = self.post(self.cheque(s.drawdown, 1))
		cheque.bounce(bounce_date="2025-03-03", reason="Insufficient funds")
		cheque.reload()
		cheque.represent(represent_date="2025-03-04", new_due_date="2025-03-10")
		cheque.reload()
		self.assertEqual((cheque.cycle, cheque.accounting_status), (2, "Not Posted"))
		self.post(cheque)
		repayments = self.cheque_repayments(cheque)
		self.assertEqual([(r.docstatus, r.cheque_cycle) for r in repayments], [(2, 1), (1, 2)])
		pe = frappe.db.get_value(
			"Payment Entry", cheque.party_payment_entry, ["treasury_cycle", "posting_date"], as_dict=True
		)
		self.assertEqual((pe.treasury_cycle, pe.posting_date), (2, D("2025-03-10")))
		self.assertEqual(self.row(s.drawdown, 1).status, "Paid")

		# replace: the new cheque holds the installment, the old one releases it
		old = self.cheque(s.drawdown, 2)
		result = old.replace(
			new_cheque_values={"cheque_no": unique_cheque_no(), "replacement_date": "2025-02-01"}
		)
		new = frappe.get_doc(CHEQUE, result["replaced_by"])
		self.assertEqual(
			(new.facility_drawdown, new.facility_schedule_row, new.operational_status),
			(s.drawdown.name, self.row(s.drawdown, 2).name, "Delivered"),
		)
		self.assertEqual(self.row(s.drawdown, 2).pdc_cheque, new.name)
		self.post(new)
		self.assertEqual(self.row(s.drawdown, 2).status, "Paid")
		self.assertEqual(self.cheque_repayments(new)[0].docstatus, 1)

		# a replacement of another amount no longer matches the installment
		old = self.cheque(s.drawdown, 3)
		result = old.replace(
			new_cheque_values={
				"cheque_no": unique_cheque_no(),
				"amount": 999,
				"replacement_date": "2025-02-01",
			}
		)
		with self.assertRaises(frappe.ValidationError) as ctx:
			lifecycle.post_party(result["replaced_by"])
		self.assertIn("no longer matches the unpaid installment", str(ctx.exception))

	def test_represent_refused_after_installment_paid(self):
		"""Review 7g m3: after a bounce the installment may be paid by hand (the bounced cheque is not live);
		presenting the cheque again is then refused up front, not at its posting."""
		s = self.loan()
		self.batch(s.drawdown, rows=[1])
		cheque = self.post(self.cheque(s.drawdown, 1))
		cheque.bounce(bounce_date="2025-03-03", reason="Insufficient funds")
		make_repayment(s.drawdown, "2025-03-05", allocation_mode="Selected Row", installment_no=1)
		cheque.reload()
		with self.assertRaises(frappe.ValidationError) as ctx:
			cheque.represent(represent_date="2025-03-06")
		self.assertIn("can no longer pay installment 1", str(ctx.exception))
		cheque.reload()
		self.assertEqual((cheque.operational_status, cheque.cycle), ("Bounced", 1))

	def test_installment_cheque_date_is_the_due_date(self):
		"""Security review 7g Minor 4: a Created installment cheque cannot be re-dated (its posting date is the
		repayment date); Postpone keeps the original due date."""
		s = self.loan()
		self.batch(s.drawdown, rows=[1], handover_date=None)
		cheque = self.cheque(s.drawdown, 1)
		self.assertEqual(cheque.operational_status, "Created")
		cheque.original_due_date = D("2025-03-15")
		with self.assertRaises(frappe.ValidationError) as ctx:
			cheque.save()
		self.assertIn("must be its installment's due date", str(ctx.exception))

	def test_cheque_links_move_the_schedule_version(self):
		"""Security review 7g Major 2: linking or releasing an installment cheque moves the drawdown's
		`schedule_version`, so a second batch waiting on the drawdown lock detects the first one."""

		def version() -> int:
			return frappe.db.get_value("Facility Drawdown", s.drawdown.name, "schedule_version")

		s = self.loan()
		before = version()
		self.batch(s.drawdown, rows=[1, 2], handover_date=None)
		linked = version()
		self.assertGreater(linked, before)
		released = version()
		self.cheque(s.drawdown, 1).cancel_cheque(reason="Wrong leaf")
		self.assertGreater(version(), released)
		self.assertIsNone(self.row(s.drawdown, 1).pdc_cheque)

	def test_batch_needs_read_on_the_drawdown(self):
		"""Security review 7g Minor 6: the batch's messages show the installments; validating an installment
		batch needs read permission on the drawdown. The user may create batches (Treasury User) but reads only
		another drawdown (a User Permission)."""
		s = self.loan()
		other = make_drawdown(make_bank_facility())
		batch = frappe.get_doc(facility_cheques.make_installment_batch(s.drawdown.name))
		add_user_permission(RESTRICTED_USER, DRAWDOWN, other.name, applicable_for=DRAWDOWN)
		self.addCleanup(frappe.cache.hdel, "user_permissions", RESTRICTED_USER)
		with self.set_user(RESTRICTED_USER):
			self.assertTrue(frappe.has_permission("Cheque Batch", "create"))
			self.assertFalse(frappe.has_permission(DRAWDOWN, "read", s.drawdown.name))
			self.assertRaises(frappe.PermissionError, facility_cheques.validate_batch, batch)
		# the same batch validates for a Treasury User who reads the drawdown
		with self.set_user(TREASURY_USER):
			facility_cheques.validate_batch(batch)

	def test_manual_repayment_on_cheque_row_refused(self):
		s = self.loan()
		self.batch(s.drawdown, rows=[1, 2])
		with self.assertRaises(frappe.ValidationError) as ctx:
			make_repayment(s.drawdown, "2025-02-28", allocation_mode="Selected Row", installment_no=1)
		self.assertIn("is settled by cheque", str(ctx.exception))
		# Oldest First skips the installments with a live cheque: the next free one is installment 3
		manual = make_repayment(s.drawdown, "2025-02-28")
		self.assertEqual([a.installment_no for a in manual.allocations], [3])
		self.assertEqual((manual.principal_paid, manual.interest_paid), (10_000, 986.30))
		s.drawdown.reload()
		self.assertEqual(
			[r.name for r in repayment.reschedulable_rows(s.drawdown, "2025-02-01")][:1],
			[self.row(s.drawdown, 4).name],
		)

	def test_prepayment_blocked_by_cheques(self):
		s = self.loan()
		self.batch(s.drawdown, rows=[2, 3])
		with self.assertRaises(frappe.ValidationError) as ctx:
			make_repayment(
				s.drawdown, "2025-02-28", principal_paid=15_000, interest_paid=1104.66, prepayment=1
			)
		self.assertIn("Installments 2, 3 are settled by cheques", str(ctx.exception))

	def test_cancel_cheque_and_batch_release_rows(self):
		s = self.loan()
		batch = self.batch(s.drawdown, rows=[1, 2], handover_date=None)
		first = self.cheque(s.drawdown, 1)
		self.assertEqual(first.operational_status, "Created")
		first.cancel_cheque(reason="Wrong date")
		self.assertIsNone(self.row(s.drawdown, 1).pdc_cheque)
		self.assertTrue(self.row(s.drawdown, 2).pdc_cheque)
		batch.reload()
		batch.cancel()
		self.assertIsNone(self.row(s.drawdown, 2).pdc_cheque)

	def test_user_cannot_cancel_cheque_repayment(self):
		s = self.loan()
		self.batch(s.drawdown, rows=[1])
		cheque = self.post(self.cheque(s.drawdown, 1))
		doc = frappe.get_doc("Facility Repayment", cheque.facility_repayment)
		with self.assertRaises(frappe.ValidationError) as ctx:
			doc.cancel()
		self.assertIn("is settled by cheque", str(ctx.exception))

	def test_forecast_no_double_count(self):
		s = self.loan()
		self.batch(s.drawdown, rows=[1])
		cheque = self.cheque(s.drawdown, 1)

		def flows():
			with patch("treasury.pdc.report_utils.today", return_value="2025-02-01"):
				report = BankPositionForecast(
					{
						"company": COMPANY,
						"bank_account": [s.bank.bank_account],
						"from_date": "2025-02-01",
						"to_date": "2025-02-28",
						"periodicity": "Monthly",
						"currency_view": "Company Currency",
						"show_details": 1,
					}
				)
				_columns, data, *_rest = report.run()
			ours = (cheque.name, s.drawdown.name)
			return [r for r in data if r.get("indent") and r.get("reference_name") in ours]

		self.assertEqual([r.reference_name for r in flows()], [cheque.name])
		# a bounced cheque is no longer the expected outflow: the installment is again
		self.post(cheque)
		cheque.bounce(bounce_date="2025-02-28", reason="Insufficient funds")
		self.assertEqual([r.reference_name for r in flows()], [s.drawdown.name])

	def test_projected_party_statement_excludes_facility_cheques(self):
		s = self.loan()
		self.batch(s.drawdown, rows=[1, 2])
		lender = lenders.get_lender_supplier(s.facility.name)
		report = ProjectedPartyStatement(
			{
				"company": COMPANY,
				"party_type": "Supplier",
				"party": lender,
				"from_date": "2025-01-01",
				"to_date": "2025-12-31",
			}
		)
		ours = {self.cheque(s.drawdown, 1).name, self.cheque(s.drawdown, 2).name}
		self.assertFalse(ours & {c.name for c in report.get_unposted_cheques()})
		self.post(self.cheque(s.drawdown, 1))
		self.assertFalse(ours & {c.name for c in report.get_posted_not_cleared()})
