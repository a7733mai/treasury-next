"""Phase 7f patch: cancelled leaves rows saved before 7f get their kind and source (spec 7f §6, §7.6)."""

import frappe
from frappe.tests import IntegrationTestCase

from treasury.patches.v1_1 import set_cheque_leaf_defaults
from treasury.pdc.doctype.cheque_book.cheque_book import cancel_leaf
from treasury.tests.utils import (
	DEFAULT_PDC_SETTINGS,
	TreasuryTestMixin,
	make_cheque_book,
	make_pdc_cheque,
	unique_leaf_start,
)

LEAF = "Cheque Book Cancelled Leaf"
SAVEPOINT = "treasury_patch_null"


class TestPatchChequeLeafDefaults(TreasuryTestMixin, IntegrationTestCase):
	def setUp(self):
		super().setUp()
		self.use_company_settings(**DEFAULT_PDC_SETTINGS)

	def rows(self, book) -> dict:
		return {
			row.cheque_no: row
			for row in frappe.get_all(
				LEAF,
				filters={"parent": book},
				fields=["name", "cheque_no", "kind", "reason_type", "source_doctype", "source_name"],
			)
		}

	def as_before_7f(self, book):
		"""The rows as version 1.0 saved them: no kind, reason type or source."""
		for row in self.rows(book).values():
			frappe.db.set_value(
				LEAF,
				row.name,
				{"kind": None, "reason_type": None, "source_doctype": None, "source_name": None},
				update_modified=False,
			)

	@staticmethod
	def set_null(doctype: str, fieldname: str, name: str) -> bool:
		"""Write NULL into a column; False when the database refuses it (a NOT NULL integer column)."""
		table = frappe.qb.DocType(doctype)
		frappe.db.savepoint(SAVEPOINT)
		try:
			frappe.qb.update(table).set(table[fieldname], None).where(table.name == name).run()
		except Exception:
			frappe.db.rollback(save_point=SAVEPOINT)
			return False
		return True

	def test_patch(self):
		start = unique_leaf_start(10)
		book = make_cheque_book(start, start + 9, leaf_digits=8)
		cheque = make_pdc_cheque(cheque_book=book.name)
		cheque.cancel_cheque(reason="Wrong amount")
		cancel_leaf(book.name, str(start + 1).zfill(8), "Spoilt")
		self.as_before_7f(book.name)
		# an Int column is usually NOT NULL (then the patch step is a no-op): NULL is written when possible
		self.set_null("PDC Cheque", "print_count", cheque.name)

		for _run in range(2):  # idempotent
			set_cheque_leaf_defaults.execute()
			rows = self.rows(book.name)
			cancelled = rows[cheque.cheque_no]
			self.assertEqual(
				(cancelled.kind, cancelled.reason_type, cancelled.source_doctype, cancelled.source_name),
				("Cancelled", "Cheque Cancelled", "PDC Cheque", cheque.name),
			)
			spoilt = rows[str(start + 1).zfill(8)]
			self.assertEqual((spoilt.kind, spoilt.reason_type, spoilt.source_name), ("Cancelled", None, None))
			self.assertEqual(frappe.db.get_value("PDC Cheque", cheque.name, "print_count"), 0)

	def test_ambiguous_source_is_left_empty(self):
		"""Two cancelled cheques with the leaf's number: the source cannot be told, it stays empty."""
		start = unique_leaf_start(10)
		book = make_cheque_book(start, start + 9, leaf_digits=8)
		first = make_pdc_cheque(cheque_book=book.name)
		first.cancel_cheque(reason="Wrong amount")
		# a second cancelled cheque of the book with the same number, as data saved before 7f: validation now
		# refuses a hand-typed cancelled leaf, so the number is written directly
		second = make_pdc_cheque(cheque_no=str(unique_leaf_start(1)).zfill(8))
		frappe.db.set_value(
			"PDC Cheque",
			second.name,
			{"cheque_no": first.cheque_no, "cheque_book": book.name, "operational_status": "Cancelled"},
		)
		self.as_before_7f(book.name)
		set_cheque_leaf_defaults.execute()
		row = self.rows(book.name)[first.cheque_no]
		self.assertEqual((row.kind, row.source_name), ("Cancelled", None))
