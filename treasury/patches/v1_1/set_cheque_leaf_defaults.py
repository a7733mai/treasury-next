"""Phase 7f: cheque leaf register and cheque printing.

- Cancelled leaves rows saved before get `kind` "Cancelled"; a row left by a cancelled outgoing PDC Cheque of
  its book gets that cheque as its source (only when exactly one such cheque carries the number).
- PDC Cheques get `print_count` 0, Treasury Company Settings `cheque_reprint_requires_manager` 1.

Idempotent: only empty values are set. No Cheque Layout is created: banks keep printing with their PDC Account
Mapping format (or "PDC Cheque Print") until a layout exists.
"""

import frappe
from frappe.query_builder.functions import IfNull

LEAF = "Cheque Book Cancelled Leaf"
CHEQUE = "PDC Cheque"


def execute():
	leaf = frappe.qb.DocType(LEAF)
	frappe.qb.update(leaf).set(leaf.kind, "Cancelled").where(IfNull(leaf.kind, "") == "").run()
	set_leaf_sources()

	cheque = frappe.qb.DocType(CHEQUE)
	frappe.qb.update(cheque).set(cheque.print_count, 0).where(cheque.print_count.isnull()).run()

	settings = frappe.qb.DocType("Treasury Company Settings")
	frappe.qb.update(settings).set(settings.cheque_reprint_requires_manager, 1).where(
		settings.cheque_reprint_requires_manager.isnull()
	).run()


def set_leaf_sources():
	rows = frappe.get_all(
		LEAF,
		filters={"parenttype": "Cheque Book", "source_name": ["is", "not set"]},
		fields=["name", "parent", "cheque_no"],
	)
	if not rows:
		return
	cheques: dict[tuple[str, str], list[str]] = {}
	for cheque in frappe.get_all(
		CHEQUE,
		filters={
			"direction": "Outgoing",
			"operational_status": "Cancelled",
			"cheque_book": ["in", sorted({r.parent for r in rows})],
		},
		fields=["name", "cheque_book", "cheque_no"],
	):
		cheques.setdefault((cheque.cheque_book, _number(cheque.cheque_no)), []).append(cheque.name)

	leaf = frappe.qb.DocType(LEAF)
	for row in rows:
		matches = cheques.get((row.parent, _number(row.cheque_no)), [])
		if len(matches) != 1:
			continue
		frappe.qb.update(leaf).set(leaf.source_doctype, CHEQUE).set(leaf.source_name, matches[0]).set(
			leaf.reason_type, "Cheque Cancelled"
		).where(leaf.name == row.name).run()


def _number(cheque_no) -> str:
	"""A leaf number without its zero padding (rows and cheques may be padded differently)."""
	value = str(cheque_no or "").strip()
	return str(int(value)) if value.isdigit() else value
