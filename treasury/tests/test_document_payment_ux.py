"""Phase 7c UX (design spec 7c): what the forms rely on from the server.

- P5: the refusals of the mappers carry the title "Treasury"; titled messages ("Not Permitted") keep theirs.
- P13: an account refusal raises `TreasuryPartyAccountError`, which the Treasury buttons recognise by name.
- P9 / P12 / §3.1: the Cheque Batch "Created From" and settings section layout.
- P11: the Cheque Batch Receipt prints the document the batch settles ("Against").
"""

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import today

from treasury.bank_transfers.api import make_bank_transfer as map_transfer
from treasury.pdc.api import make_cheque_batch as map_batch
from treasury.pdc.api import make_pdc_cheque as map_cheque
from treasury.security_cheques.api import make_security_cheque as map_security
from treasury.tests.utils import (
	COMPANY,
	PE_SETTINGS,
	SUPPLIER,
	TreasuryTestMixin,
	acc,
	ensure_account,
	ensure_fiscal_year,
	ensure_user,
	make_party_journal_entry,
	make_purchase_invoice,
	make_sales_invoice,
	settings_for_documents,
	unique_cheque_no,
)
from treasury.utils import source_documents

NO_ROLES_USER = "treasury-7c-ux-noroles@example.com"


class TestDocumentPaymentUX(TreasuryTestMixin, IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_fiscal_year(COMPANY, today())

	def setUp(self):
		super().setUp()
		self.use_company_settings(
			**PE_SETTINGS,
			**settings_for_documents(default_outgoing_cheque_bank_account=self.data.egp.bank_account),
		)
		frappe.local.message_log = []

	def refusal_title(self) -> str | None:
		refusals = [m for m in frappe.local.message_log if m.get("raise_exception")]
		return refusals[-1].get("title") if refusals else None

	def test_refusals_are_titled_treasury(self):
		draft = make_purchase_invoice(100, submit=False)
		for mapper in (map_cheque, map_batch, map_transfer):
			with self.subTest(mapper=mapper.__module__ + "." + mapper.__name__):
				frappe.local.message_log = []
				with self.assertRaisesRegex(frappe.ValidationError, "nothing outstanding"):
					mapper("Purchase Invoice", draft.name)
				self.assertEqual(self.refusal_title(), frappe._("Treasury"))

		frappe.local.message_log = []
		with self.assertRaisesRegex(frappe.ValidationError, "cannot be created from"):
			map_security("Item", "X")
		self.assertEqual(self.refusal_title(), frappe._("Treasury"))

	def test_permission_refusals_keep_their_title(self):
		ensure_user(NO_ROLES_USER, [])
		pi = make_purchase_invoice(100)
		with self.set_user(NO_ROLES_USER):
			frappe.local.message_log = []
			self.assertRaises(frappe.PermissionError, map_cheque, "Purchase Invoice", pi.name)
		self.assertNotEqual(self.refusal_title(), frappe._("Treasury"))

	def test_mapper_success_leaves_titles_alone(self):
		"""No refusal: the context manager changes nothing (and the result is the unsaved document)."""
		values = map_cheque("Purchase Invoice", make_purchase_invoice(300).name)
		self.assertEqual((values["doctype"], values.get("__islocal")), ("PDC Cheque", 1))
		self.assertIsNone(self.refusal_title())

	def test_account_refusal_type(self):
		"""The client appends "Use Create ▸ Payment" under this refusal, matched by the exception name."""
		other_payable = ensure_account("Treasury Creditors 7c", "Accounts Payable", "Payable")
		je = make_party_journal_entry(
			"Supplier", SUPPLIER, 700, acc("Cost of Goods Sold"), party_account=other_payable
		)
		self.assertTrue(issubclass(source_documents.TreasuryPartyAccountError, frappe.ValidationError))
		self.assertEqual(source_documents.TreasuryPartyAccountError.__name__, "TreasuryPartyAccountError")
		for mapper in (map_cheque, map_batch, map_transfer):
			with self.subTest(mapper=mapper.__module__ + "." + mapper.__name__):
				frappe.local.message_log = []
				with self.assertRaisesRegex(source_documents.TreasuryPartyAccountError, "uses account"):
					mapper("Journal Entry", je.name)
				self.assertEqual(self.refusal_title(), frappe._("Treasury"))

	def test_batch_created_from_layout(self):
		meta = frappe.get_meta("Cheque Batch")
		self.assertTrue(meta.get_field("source_doctype").hidden)
		self.assertTrue(meta.get_field("source_name").read_only)
		self.assertIn("source_name", [f.strip() for f in (meta.search_fields or "").split(",")])
		self.assertEqual(
			meta.get_field("generator_section").collapsible_depends_on, "eval:!(doc.cheques||[]).length"
		)

		settings = frappe.get_meta("Treasury Company Settings").get_field("documents_section")
		self.assertTrue(settings.collapsible)
		self.assertEqual(settings.collapsible_depends_on, "eval:!doc.default_outgoing_cheque_bank_account")

	def test_batch_receipt_prints_the_source(self):
		self.use_company_settings(default_installment_count=2)
		si = make_sales_invoice(2000)
		doc = frappe.get_doc(map_batch("Sales Invoice", si.name))
		doc.gen_first_cheque_no = unique_cheque_no() + "1"
		doc.generate_rows()
		doc.insert()

		html = frappe.get_print("Cheque Batch", doc.name, print_format="Cheque Batch Receipt")
		self.assertIn('class="pdc-source"', html)
		self.assertIn(si.name, html)

		# without a source: no "Against" row
		doc.db_set({"source_doctype": None, "source_name": None})
		html = frappe.get_print("Cheque Batch", doc.name, print_format="Cheque Batch Receipt")
		self.assertNotIn('class="pdc-source"', html)
