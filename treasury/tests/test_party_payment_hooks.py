"""Phase 9a: the generic party payment extension point of the posting service (`posting.get_party_rules`,
hook `treasury_party_payment_hooks`), the generic "Created From" link (treasury.utils.document_sources) and the
custody bridge being inert without the custody app. Runs with or without custody: the rules come from a fake
hook patched into `posting._party_rule_methods` (spec 9 §8, 9a)."""

from typing import ClassVar
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from treasury.accounting import posting
from treasury.integrations.custody import documents, rules
from treasury.tests.utils import (
	COMPANY,
	CUSTOMER,
	PE_SETTINGS,
	SUPPLIER,
	TreasuryTestMixin,
	acc,
	assert_gl_entries,
	day,
	ensure_account,
	ensure_branch_dimension,
	ensure_customer,
	get_pe,
	make_pdc_cheque,
	make_purchase_invoice,
)

DOCTYPE = "PDC Cheque"
ALT_DEBTORS = "Treasury Alt Debtors"
OTHER_CUSTOMER = "_Test Treasury Customer Sources"


def hooks(*methods):
	"""Patch the party payment hooks of the installed apps with `methods`."""
	return patch.object(posting, "_party_rule_methods", return_value=list(methods))


def for_party(party_type: str, answer: dict):
	"""A fake hook answering `answer` for `party_type` only."""

	def hook(ctx):
		return dict(answer) if ctx.party_type == party_type else None

	return hook


class TestPartyPaymentHooks(TreasuryTestMixin, IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		cls.branch_field = ensure_branch_dimension()  # persistent fixture (DDL) before any test data
		super().setUpClass()
		cls.alt_debtors = ensure_account(ALT_DEBTORS, "Accounts Receivable", "Receivable")
		cls.root_cost_center = frappe.db.get_value("Cost Center", {"company": COMPANY, "is_group": 1}, "name")
		cls.alt_cost_center = acc("Treasury Hook Center")
		if not frappe.db.exists("Cost Center", cls.alt_cost_center):
			frappe.get_doc(
				{
					"doctype": "Cost Center",
					"cost_center_name": "Treasury Hook Center",
					"parent_cost_center": cls.root_cost_center,
					"company": COMPANY,
					"is_group": 0,
				}
			).insert(ignore_permissions=True)

	def setUp(self):
		super().setUp()
		self.use_company_settings(**PE_SETTINGS)

	def received(self, amount=1000):
		cheque = make_pdc_cheque("Incoming", amount=amount, issue_date=day(-10), due_date=day(10))
		cheque.receive(receipt_date=day(-9))
		cheque.reload()
		return cheque

	def delivered(self, amount=1000, **values):
		cheque = make_pdc_cheque(amount=amount, issue_date=day(-10), due_date=day(10), **values)
		cheque.deliver(handover_date=day(-9))
		cheque.reload()
		return cheque

	# ------------------------------------------------------------------ defaults

	def test_no_hook_defaults(self):
		with hooks():
			rules_ = posting.get_party_rules(
				source_doctype=DOCTYPE,
				source_name="x",
				company=COMPANY,
				party_type="Supplier",
				party=SUPPLIER,
				action="payment",
			)
			self.assertEqual(dict(rules_), posting.RULE_DEFAULTS)
			cheque = self.delivered()
		pe = get_pe(cheque.party_payment_entry)
		self.assertEqual(pe.paid_to, self.data.payable)
		assert_gl_entries(
			self,
			"Payment Entry",
			pe.name,
			[(self.data.payable, 1000, 0, SUPPLIER), (self.data.egp.pdc_payable, 0, 1000, None)],
		)

	def test_no_party_type_skips_hooks(self):
		hook = MagicMock(return_value={"allow_references": False})
		with hooks(hook):
			result = posting.get_party_rules(
				source_doctype=DOCTYPE,
				source_name="x",
				company=COMPANY,
				party_type=None,
				party=None,
				action="payment",
			)
		hook.assert_not_called()
		self.assertEqual(dict(result), posting.RULE_DEFAULTS)

	# ------------------------------------------------------------------ payment rules

	def test_party_account_override(self):
		with hooks(for_party("Customer", {"party_account": self.alt_debtors})):
			cheque = self.received()
		pe = get_pe(cheque.party_payment_entry)
		self.assertEqual(pe.paid_from, self.alt_debtors)
		assert_gl_entries(
			self,
			"Payment Entry",
			pe.name,
			[(self.data.egp.pdc_receivable, 1000, 0, None), (self.alt_debtors, 0, 1000, CUSTOMER)],
		)

	def test_posting_owned_fields_refused(self):
		"""A hook can never set a field the posting service owns: standard fields (accounts, amounts, party,
		cost center ...), Treasury's own fields and the Accounting Dimensions."""
		for fieldname, value in (
			("paid_to", self.alt_debtors),
			("paid_amount", 1),
			("party", OTHER_CUSTOMER),
			("cost_center", self.alt_cost_center),
			("treasury_event", "forged"),
			(self.branch_field, "x"),
		):
			with self.subTest(fieldname=fieldname):
				cheque = make_pdc_cheque("Incoming", amount=1000, issue_date=day(-10), due_date=day(10))
				with hooks(for_party("Customer", {"fields": {fieldname: value}})):
					with self.assertRaisesRegex(frappe.ValidationError, "Party rules cannot set field"):
						cheque.receive(receipt_date=day(-9))
				self.assertFalse(frappe.db.get_value(DOCTYPE, cheque.name, "party_payment_entry"))
				self.assertEqual(frappe.db.get_value(DOCTYPE, cheque.name, "operational_status"), "Created")

	def test_rule_field_refusal(self):
		"""Only a custom field of another app is accepted (custody's `custody_request` is one)."""

		class Meta:
			fields: ClassVar[dict] = {
				"other_app_link": frappe._dict(is_custom_field=1),
				"treasury_source_name": frappe._dict(is_custom_field=1),
				"branch": frappe._dict(is_custom_field=1),
				# a standard field: no custom flag (an empty definition is still a field, not "no field")
				"paid_to": frappe._dict(),
				"paid_amount": frappe._dict(fieldname="paid_amount", is_custom_field=0),
			}

			def get_field(self, fieldname):
				return self.fields.get(fieldname)

		with patch.object(posting, "_all_dimension_fieldnames", return_value=["branch"]):
			self.assertIsNone(posting.rule_field_refusal(Meta(), "other_app_link"))
			for fieldname in ("treasury_source_name", "branch", "paid_to", "paid_amount"):
				self.assertIn("Party rules cannot set field", posting.rule_field_refusal(Meta(), fieldname))
			self.assertIn("has no field", posting.rule_field_refusal(Meta(), "nothing"))

	def test_conflicting_rule_fields_throw(self):
		first = for_party("Customer", {"fields": {"other_app_link": "A"}})
		kwargs = dict(
			source_doctype=DOCTYPE,
			source_name="x",
			company=COMPANY,
			party_type="Customer",
			party=CUSTOMER,
			action="payment",
		)
		with hooks(first, for_party("Customer", {"fields": {"other_app_link": "B"}})):
			with self.assertRaisesRegex(frappe.ValidationError, "Conflicting values for field"):
				posting.get_party_rules(**kwargs)
		# the same value twice is no conflict
		with hooks(first, for_party("Customer", {"fields": {"other_app_link": "A"}})):
			self.assertEqual(posting.get_party_rules(**kwargs).fields, {"other_app_link": "A"})

	def test_invalid_action_without_party(self):
		with self.assertRaisesRegex(frappe.ValidationError, "Invalid party rule action"):
			posting.get_party_rules(
				source_doctype=DOCTYPE,
				source_name="x",
				company=COMPANY,
				party_type=None,
				party=None,
				action="x",
			)

	def test_unknown_field_throws(self):
		cheque = make_pdc_cheque("Incoming", amount=1000, issue_date=day(-10), due_date=day(10))
		with hooks(for_party("Customer", {"fields": {"treasury_no_such_field": 1}})):
			with self.assertRaisesRegex(frappe.ValidationError, "Payment Entry has no field"):
				cheque.receive(receipt_date=day(-9))
		self.assertFalse(frappe.db.get_value(DOCTYPE, cheque.name, "party_payment_entry"))

	def test_references_refused(self):
		invoice = make_purchase_invoice(1000, posting_date=day(-20))
		cheque = make_pdc_cheque(amount=1000, issue_date=day(-10), due_date=day(10), invoice=invoice)
		with hooks(for_party("Supplier", {"allow_references": False})):
			with self.assertRaisesRegex(frappe.ValidationError, "cannot be allocated to references"):
				cheque.deliver(handover_date=day(-9))

	def test_charges_refused(self):
		cheque = make_pdc_cheque(amount=1000, issue_date=day(-10), due_date=day(10))
		with hooks(for_party("Supplier", {"allow_deductions": False})):
			with self.assertRaisesRegex(frappe.ValidationError, "must be posted on their own voucher"):
				posting.create_payment_entry(
					source_doctype=DOCTYPE,
					source_name=cheque.name,
					event="test_hook_charges",
					payment_type="Pay",
					company=COMPANY,
					posting_date=day(-9),
					party_type="Supplier",
					party=SUPPLIER,
					amount=1000,
					bank_account_head=self.data.egp.bank_gl,
					reference_no="HOOK-1",
					reference_date=day(-9),
					charges=5,
					charges_account=self.data.egp.bank_charges,
				)

	def test_conflicting_party_accounts_throw(self):
		first = for_party("Customer", {"party_account": self.alt_debtors})
		second = for_party("Customer", {"party_account": self.data.receivable})
		with hooks(first, second):
			with self.assertRaisesRegex(frappe.ValidationError, "Conflicting party accounts"):
				posting.get_party_rules(
					source_doctype=DOCTYPE,
					source_name="x",
					company=COMPANY,
					party_type="Customer",
					party=CUSTOMER,
					action="payment",
				)
		# the same account twice is no conflict; allow_* are ANDed, undo_by_cancel ORed, fields merged
		third = for_party(
			"Customer", {"party_account": self.alt_debtors, "fields": {"a": 1}, "undo_by_cancel": 1}
		)
		fourth = for_party("Customer", {"allow_journal_entry": False, "fields": {"b": 2}})
		with hooks(first, third, fourth):
			merged = posting.get_party_rules(
				source_doctype=DOCTYPE,
				source_name="x",
				company=COMPANY,
				party_type="Customer",
				party=CUSTOMER,
				action="journal",
			)
		self.assertEqual(merged.party_account, self.alt_debtors)
		self.assertEqual(merged.fields, {"a": 1, "b": 2})
		self.assertTrue(merged.undo_by_cancel)
		self.assertFalse(merged.allow_journal_entry)
		self.assertTrue(merged.allow_references)

	# ------------------------------------------------------------------ undo and journal entries

	def test_must_cancel_guards_reverse_voucher(self):
		cheque = self.delivered()
		pe = cheque.party_payment_entry
		self.assertFalse(posting.must_cancel("Payment Entry", pe))
		with hooks(for_party("Supplier", {"undo_by_cancel": True})):
			self.assertTrue(posting.must_cancel("Payment Entry", pe))
			with self.assertRaisesRegex(frappe.ValidationError, "can only be undone by cancelling it"):
				posting.reverse_voucher("Payment Entry", pe, day(-5), "test_hook_reversal")
			with self.assertRaisesRegex(frappe.ValidationError, "can only be undone by cancelling it"):
				posting.make_reversal("Payment Entry", pe, day(-5), "test_hook_reversal")

	def test_bounce_cancels_when_rules_say_so(self):
		"""On Bounce = Reverse, but the party may only be undone by cancelling: its PE is cancelled."""
		cheque = self.delivered()
		pe = cheque.party_payment_entry
		with hooks(for_party("Supplier", {"undo_by_cancel": True})):
			cheque.bounce(bounce_date=day(-5), reason="hook")
		cheque.reload()
		self.assertEqual((cheque.operational_status, cheque.accounting_status), ("Bounced", "Reversed"))
		self.assertEqual(frappe.db.get_value("Payment Entry", pe, "docstatus"), 2)
		self.assertFalse(cheque.reversal_journal)
		self.assertIn("Voucher Cancelled", [row.event for row in cheque.status_log])

	def test_journal_row_refused(self):
		cheque = make_pdc_cheque(amount=1000, issue_date=day(-10), due_date=day(10))
		with hooks(for_party("Supplier", {"allow_journal_entry": False})):
			with self.assertRaisesRegex(frappe.ValidationError, "cannot be posted by a Journal Entry"):
				posting.create_journal_entry(
					source_doctype=DOCTYPE,
					source_name=cheque.name,
					event="test_hook_journal",
					company=COMPANY,
					posting_date=day(-9),
					accounts=[
						{
							"account": self.data.payable,
							"party_type": "Supplier",
							"party": SUPPLIER,
							"debit_in_account_currency": 10,
						},
						{"account": self.data.egp.bank_gl, "credit_in_account_currency": 10},
					],
				)

	def test_bounce_charges_refused_by_rules(self):
		cheque = self.received()
		with hooks(for_party("Customer", {"allow_journal_entry": False})):
			with self.assertRaisesRegex(frappe.ValidationError, "Bounce charges cannot be charged to"):
				cheque.bounce(bounce_date=day(-5), reason="hook", charges=50)
		cheque.reload()
		self.assertEqual(cheque.operational_status, "Received")

	def test_existing_voucher_skips_hooks(self):
		cheque = make_pdc_cheque(amount=1000, issue_date=day(-10), due_date=day(10))
		hook = MagicMock(return_value=None)
		kwargs = dict(
			source_doctype=DOCTYPE,
			source_name=cheque.name,
			event="test_hook_idempotent",
			payment_type="Pay",
			company=COMPANY,
			posting_date=day(-9),
			party_type="Supplier",
			party=SUPPLIER,
			amount=1000,
			bank_account_head=self.data.egp.pdc_payable,
			reference_no=cheque.cheque_no,
			reference_date=day(-9),
		)
		with hooks(hook):
			first = posting.create_payment_entry(**kwargs)
			self.assertTrue(hook.called)
			hook.reset_mock()
			second = posting.create_payment_entry(**kwargs)
		self.assertEqual(first, second)
		hook.assert_not_called()

	# ------------------------------------------------------------------ custody bridge without custody

	def test_bridge_inert_without_custody(self):
		ctx = frappe._dict(
			source_doctype=DOCTYPE,
			source_name="x",
			company=COMPANY,
			party_type="Custodian",
			party="x",
			action="payment",
			payment_type="Pay",
		)
		with patch.object(rules, "is_installed", return_value=False):
			self.assertIsNone(rules.party_rules(ctx))
		self.assertIsNone(rules.party_rules(frappe._dict(ctx, party_type="Supplier")))

		cheque = make_pdc_cheque(amount=1000, issue_date=day(-10), due_date=day(10))
		with patch.object(documents, "is_installed", return_value=False):
			self.assertIsNone(documents.validate(cheque))
			new = frappe.get_doc(
				{
					"doctype": DOCTYPE,
					"direction": "Outgoing",
					"company": COMPANY,
					"party_type": "Supplier",
					"party": SUPPLIER,
					"amount": 1000,
					"source_doctype": "Custody Request",
					"source_name": "CR-0001",
				}
			)
			with self.assertRaisesRegex(frappe.ValidationError, "The Custody app is not installed."):
				documents.validate(new)

	# ------------------------------------------------------------------ "Created From"

	def test_source_link_generic(self):
		with self.assertRaisesRegex(frappe.ValidationError, "cannot be created from"):
			make_pdc_cheque(
				amount=1000,
				issue_date=day(-10),
				due_date=day(10),
				source_doctype="Customer",
				source_name=CUSTOMER,
			)

		other = ensure_customer(OTHER_CUSTOMER)
		from treasury.utils import document_sources

		with patch.object(document_sources, "registered", return_value=("Customer",)):
			cheque = make_pdc_cheque(
				amount=1000,
				issue_date=day(-10),
				due_date=day(10),
				source_doctype="Customer",
				source_name=CUSTOMER,
			)
			self.assertEqual((cheque.source_doctype, cheque.source_name), ("Customer", CUSTOMER))
			cheque.source_name = other
			with self.assertRaisesRegex(frappe.ValidationError, "Created From cannot be changed."):
				cheque.save()
			cheque.reload()
			with self.assertRaisesRegex(frappe.ValidationError, "needs both"):
				make_pdc_cheque(amount=1000, issue_date=day(-10), due_date=day(10), source_doctype="Customer")
		# a saved link stays valid when its app is gone (the cheque can still be worked)
		cheque.deliver(handover_date=day(-9))
		values = frappe.db.get_value(
			DOCTYPE, cheque.name, ["operational_status", "source_name"], as_dict=True
		)
		self.assertEqual((values.operational_status, values.source_name), ("Delivered", CUSTOMER))
