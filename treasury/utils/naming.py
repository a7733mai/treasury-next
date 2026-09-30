"""Naming series of the Treasury DocTypes (spec 7h §2.4 / §3.5), read only: the "Naming Series" section of
Treasury Company Settings documents them. Treasury adds no naming mechanism of its own:

- the options of a `naming_series` field (and so its default, the first option) are changed with Frappe's
  Document Naming Settings, which stores them as Property Setters;
- PDC Cheque, Cheque Batch and Security Cheque force their series by direction in `before_insert`
  (`NAMING_SERIES` of their controllers), so re-ordering their options changes nothing;
- per-bank (or any per-condition) naming is Frappe's Document Naming Rule: rules are evaluated before the
  series (no Treasury controller defines `autoname`), the highest priority first, and a rule's counter is one
  integer that does not restart each year.

Payment Entries of the Treasury vouchers take the series of their Modes of Payment row (per bank and method).
"""

import frappe
from frappe.model.naming import get_default_naming_series

from treasury.pdc.doctype.cheque_batch import cheque_batch
from treasury.pdc.doctype.pdc_cheque import pdc_cheque
from treasury.security_cheques.doctype.security_cheque import security_cheque

SERIES_FIELD = "naming_series"
RULE = "Document Naming Rule"
RULE_CONDITION = "Document Naming Rule Condition"
SERIES_DOCTYPES = (  # (doctype, direction field or None, bank field)
	("PDC Cheque", "direction", "bank_account"),
	("Cheque Batch", "direction", "bank_account"),
	("Security Cheque", "direction", "bank_account"),
	("Cheque Book", None, "bank_account"),
	("Bank Transfer", None, "bank_account"),
	("Bank Charge", None, "bank_account"),
	("Bank Facility", None, "default_bank_account"),
	("Facility Drawdown", None, "bank_account"),
	("Facility Repayment", None, "bank_account"),
	("Letter of Guarantee", None, "bank_account"),
	("Letter of Credit", None, "bank_account"),
	("Cheque Discounting", None, "bank_account"),
)
FORCED_SERIES = {  # the controllers' NAMING_SERIES dicts (single source)
	"PDC Cheque": pdc_cheque.NAMING_SERIES,
	"Cheque Batch": cheque_batch.NAMING_SERIES,
	"Security Cheque": security_cheque.NAMING_SERIES,
}


def get_series_overview() -> list[frappe._dict]:
	"""One row per Treasury DocType with a naming series: {doctype, series_field, options, default (a series,
	or {direction: series} when forced by direction), forced_by, bank_field, rules}. Two queries for the rules
	(the metas are cached)."""
	rules = get_rules([dt for dt, _direction, _bank in SERIES_DOCTYPES])
	rows = []
	for doctype, direction_field, bank_field in SERIES_DOCTYPES:
		options = [o for o in frappe.get_meta(doctype).get_naming_series_options() if o]
		forced = FORCED_SERIES.get(doctype)
		rows.append(
			frappe._dict(
				doctype=doctype,
				series_field=SERIES_FIELD,
				options=options,
				default=dict(forced) if forced else get_default_naming_series(doctype),
				forced_by=direction_field,
				bank_field=bank_field,
				rules=rules.get(doctype, []),
			)
		)
	return rows


def get_rules(doctypes: list[str]) -> dict[str, list[frappe._dict]]:
	"""{doctype: [{name, prefix, prefix_digits, priority, disabled, conditions, counter}]}, highest priority
	first; `conditions` = "field condition value; …" in row order."""
	rules = frappe.get_all(
		RULE,
		filters={"document_type": ["in", doctypes]},
		fields=["name", "document_type", "prefix", "prefix_digits", "priority", "disabled", "counter"],
		order_by="priority desc, name asc",
	)
	if not rules:
		return {}
	conditions: dict[str, list[str]] = {}
	for row in frappe.get_all(
		RULE_CONDITION,
		filters={"parenttype": RULE, "parent": ["in", [r.name for r in rules]]},
		fields=["parent", "field", "condition", "value"],
		order_by="parent asc, idx asc",
	):
		conditions.setdefault(row.parent, []).append(f"{row.field} {row.condition} {row.value or ''}".strip())
	result: dict[str, list] = {}
	for rule in rules:
		result.setdefault(rule.document_type, []).append(
			frappe._dict(
				name=rule.name,
				prefix=rule.prefix,
				prefix_digits=rule.prefix_digits,
				priority=rule.priority,
				disabled=rule.disabled,
				conditions="; ".join(conditions.get(rule.name, [])),
				counter=rule.counter,
			)
		)
	return result
