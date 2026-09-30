# Copyright (c) 2026, Ahmed Abdelmageed and contributors
# For license information, please see license.txt

"""Bank Charge Category (spec 7d §2.1): the expense account (and cost center) of the charges per company, and
the statement keywords that make PDC Bank Clearing propose a Bank Charge of the category."""

import frappe
from frappe import _
from frappe.model.document import Document

from treasury.bank_transfers.charge_defaults import company_charge_accounts
from treasury.treasury_setup.doctype.treasury_company_settings.treasury_company_settings import (
	validate_expense_account,
)

DOCTYPE = "Bank Charge Category"
MIN_KEYWORD_LENGTH = 3


class BankChargeCategory(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from treasury.treasury_setup.doctype.bank_charge_category_account.bank_charge_category_account import (
			BankChargeCategoryAccount,
		)

		accounts: DF.Table[BankChargeCategoryAccount]
		auto_select_proposals: DF.Check
		category_name: DF.Data
		default_description: DF.SmallText | None
		disabled: DF.Check
		keywords: DF.SmallText | None
		keywords_preview: DF.HTML | None
	# end: auto-generated types

	def validate(self):
		self.validate_accounts()
		self.keywords = clean_keywords(self.keywords)

	def validate_accounts(self):
		seen = set()
		for row in self.get("accounts"):
			if row.company in seen:
				frappe.throw(
					_("Row #{0}: Company {1} already has an account.").format(
						row.idx, frappe.bold(row.company)
					)
				)
			seen.add(row.company)
			label = _(row.meta.get_label("expense_account"))
			validate_expense_account(row.company, row.expense_account, label, row.idx)
			if row.cost_center:
				details = frappe.db.get_value(
					"Cost Center", row.cost_center, ["company", "is_group"], as_dict=True
				)
				if not details or details.company != row.company or details.is_group:
					frappe.throw(
						_("Row #{0}: Cost Center {1} does not belong to company {2}.").format(
							row.idx, frappe.bold(row.cost_center), frappe.bold(row.company)
						)
					)


def clean_keywords(text: str | None) -> str | None:
	"""The keywords one per line: trimmed (inner spaces single), without empty lines and case-insensitive
	duplicates; a keyword shorter than MIN_KEYWORD_LENGTH is rejected."""
	kept, seen = [], set()
	for line in (text or "").splitlines():
		keyword = " ".join(line.split())
		if not keyword:
			continue
		if len(keyword) < MIN_KEYWORD_LENGTH:
			frappe.throw(
				_('Keyword "{0}" is too short (at least 3 characters).').format(
					frappe.utils.escape_html(keyword)
				)
			)
		if keyword.casefold() in seen:
			continue
		seen.add(keyword.casefold())
		kept.append(keyword)
	return "\n".join(kept) or None


def parse_keywords(text: str | None) -> list[str]:
	"""The keywords normalised for matching: lower-case, single spaces, no empty or duplicate entries."""
	return list(dict.fromkeys(normalise(line) for line in (text or "").splitlines() if normalise(line)))


def normalise(text: str | None) -> str:
	"""Lower-case text with single spaces (keywords and statement texts are compared in this form)."""
	return " ".join((text or "").casefold().split())


@frappe.whitelist(methods=["GET"])
def get_company_charge_account(company: str) -> str | None:
	"""The default Expense Account of a new account row (form script, design 7d P10): the company's Bank
	Charges Account when its PDC Accounts rows have exactly one (the rule of the default categories)."""
	if not (frappe.has_permission(DOCTYPE, "create") or frappe.has_permission(DOCTYPE, "write")):
		frappe.throw(_("You are not permitted to change Bank Charge Categories."), frappe.PermissionError)
	if not company:
		return None
	frappe.has_permission("Company", "read", doc=company, throw=True)
	return company_charge_accounts().get(company)


def get_account_row(category: str, company: str) -> frappe._dict | None:
	"""{expense_account, cost_center} of `category` for `company`, or None."""
	if not (category and company):
		return None
	rows = frappe.get_all(
		"Bank Charge Category Account",
		filters={"parenttype": "Bank Charge Category", "parent": category, "company": company},
		fields=["expense_account", "cost_center"],
		limit=1,
	)
	return rows[0] if rows else None
