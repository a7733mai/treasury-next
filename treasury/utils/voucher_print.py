"""Context of the bilingual cash / bank vouchers (Treasury Receipt / Payment / Journal Voucher print formats).

The vouchers print Arabic and English side by side whatever the print language, so the fixed wording lives in
`LABELS` as (Arabic, English) pairs instead of the translation catalogue. Read-only: nothing is posted,
changed or saved (exposed to the print formats through hooks.py `jinja`).
"""

import frappe
from frappe.utils import flt, fmt_money, get_fullname

from treasury.utils.amount_in_words import treasury_amount_in_words

LABELS = {
	"receipt": ("سند قبض", "Receipt Voucher"),
	"payment": ("سند صرف", "Payment Voucher"),
	"transfer": ("سند تحويل", "Transfer Voucher"),
	"journal": ("سند قيد يومية", "Journal Voucher"),
	"cash_receipt": ("سند قبض نقدي", "Cash Receipt Voucher"),
	"cash_payment": ("سند صرف نقدي", "Cash Payment Voucher"),
	"bank_receipt": ("سند قبض بنكي", "Bank Receipt Voucher"),
	"bank_payment": ("سند صرف بنكي", "Bank Payment Voucher"),
	"number": ("رقم السند", "Voucher No."),
	"date": ("التاريخ", "Date"),
	"received_from": ("استلمنا من", "Received from"),
	"paid_to": ("صرفنا إلى", "Paid to"),
	"party": ("الطرف", "Party"),
	"amount": ("المبلغ", "Amount"),
	"in_words": ("المبلغ كتابة", "Amount in words"),
	"mode": ("طريقة الدفع", "Mode of payment"),
	"cash_account": ("حساب الصندوق", "Cash account"),
	"bank_account": ("الحساب البنكي", "Bank account"),
	"account": ("الحساب", "Account"),
	"cheque_no": ("رقم الشيك / المرجع", "Cheque / reference no."),
	"cheque_date": ("تاريخ الشيك / المرجع", "Cheque / reference date"),
	"bank": ("البنك", "Bank"),
	"project": ("المشروع", "Project"),
	"cost_center": ("مركز التكلفة", "Cost center"),
	"narration": ("البيان", "Narration"),
	"references": ("المستندات المسددة", "Settled documents"),
	"reference": ("المستند", "Document"),
	"type": ("النوع", "Type"),
	"allocated": ("المبلغ المسدد", "Allocated"),
	"debit": ("مدين", "Debit"),
	"credit": ("دائن", "Credit"),
	"total": ("الإجمالي", "Total"),
	"voucher_type": ("نوع القيد", "Entry type"),
	"line_party": ("الطرف", "Party"),
	"line_remark": ("ملاحظات", "Remarks"),
	"prepared_by": ("أعدّه", "Prepared by"),
	"checked_by": ("راجعه", "Checked by"),
	"approved_by": ("اعتمده", "Approved by"),
	"received_by": ("المستلم", "Received by"),
	"name": ("الاسم", "Name"),
	"signature": ("التوقيع", "Signature"),
	"draft": ("مسودة", "Draft"),
	"cancelled": ("ملغى", "Cancelled"),
	"cash": ("نقداً", "Cash"),
	"bank_mode": ("بنك", "Bank"),
	"other": ("أخرى", "Other"),
}


def english_amount_in_words(amount, currency: str | None) -> str:
	"""Frappe's `money_in_words`, always in English (the voucher is bilingual, the print language may be Arabic)."""
	from frappe.utils import money_in_words

	previous = getattr(frappe.local, "lang", None)
	frappe.local.lang = "en"
	try:
		return money_in_words(flt(amount), currency or None)
	finally:
		frappe.local.lang = previous


def arabic_amount_in_words(amount, currency: str | None) -> str:
	"""The amount in Arabic words ("فقط ... لا غير"), from the shared num2words based helper."""
	return treasury_amount_in_words(flt(amount), currency or None, "ar")


def _mode_kind(mode_of_payment: str | None, account: str | None) -> str:
	"""`cash` / `bank` / `other`: the Mode of Payment type, else the type of the cash / bank GL account."""
	mode_type = frappe.get_cached_value("Mode of Payment", mode_of_payment, "type") if mode_of_payment else None
	account_type = frappe.get_cached_value("Account", account, "account_type") if account else None
	for value in (mode_type, account_type):
		if value in ("Cash", "Bank"):
			return value.lower()
	return "other"


def _bank_of(bank_account: str | None) -> str | None:
	return frappe.get_cached_value("Bank Account", bank_account, "bank") if bank_account else None


def _money(value, currency: str | None) -> str:
	return fmt_money(flt(value), currency=currency)


def _common(doc, kind: str, amount, currency) -> dict:
	return {
		"labels": {key: list(pair) for key, pair in LABELS.items()},
		"kind": kind,
		"currency": currency,
		"amount": flt(amount),
		"amount_formatted": _money(amount, currency),
		"words_ar": arabic_amount_in_words(amount, currency),
		"words_en": english_amount_in_words(amount, currency),
		"prepared_by": get_fullname(doc.owner) if doc.get("owner") else "",
		"docstatus": doc.docstatus,
		"project": doc.get("project"),
	}


def treasury_voucher_context(doc) -> dict:
	"""Everything a voucher format prints for a Payment Entry or a Journal Entry (see the module docstring)."""
	if doc.doctype == "Payment Entry":
		return _payment_entry(doc)
	if doc.doctype == "Journal Entry":
		return _journal_entry(doc)
	return {"labels": {key: list(pair) for key, pair in LABELS.items()}, "kind": "unsupported"}


def _payment_entry(doc) -> dict:
	receive = doc.payment_type == "Receive"
	pay = doc.payment_type == "Pay"
	cash_side = doc.paid_to if receive else doc.paid_from
	currency = (doc.paid_to_account_currency if receive else doc.paid_from_account_currency) or None
	amount = doc.received_amount if receive else doc.paid_amount
	mode = _mode_kind(doc.mode_of_payment, cash_side)
	flow = "receipt" if receive else "payment" if pay else "transfer"
	title_key = f"{mode}_{flow}" if mode in ("cash", "bank") and flow != "transfer" else flow
	mode_label = LABELS["cash"] if mode == "cash" else LABELS["bank_mode"] if mode == "bank" else LABELS["other"]
	account_label = (
		LABELS["cash_account"] if mode == "cash" else LABELS["bank_account"] if mode == "bank" else LABELS["account"]
	)
	context = _common(doc, flow, amount, currency)
	context.update(
		{
			"title": LABELS[title_key],
			"mode": mode,
			"mode_label": mode_label,
			"mode_of_payment": doc.mode_of_payment,
			"account": cash_side,
			"account_label": account_label,
			"party_label": LABELS["received_from"] if receive else LABELS["paid_to"] if pay else LABELS["party"],
			"party": doc.get("party_name") or doc.get("party") or (doc.paid_from if receive else doc.paid_to),
			"party_type": doc.get("party_type"),
			"party_id": doc.get("party"),
			"cheque_no": doc.get("reference_no"),
			"cheque_date": doc.get("reference_date"),
			"bank": _bank_of(doc.get("bank_account")) or _bank_of(doc.get("party_bank_account")),
			"cost_center": doc.get("cost_center"),
			"narration": doc.get("remarks"),
			"references": [
				{
					"type": row.reference_doctype,
					"name": row.reference_name,
					"allocated": _money(row.allocated_amount, currency),
				}
				for row in (doc.get("references") or [])
			],
		}
	)
	return context


def _journal_entry(doc) -> dict:
	company_currency = frappe.get_cached_value("Company", doc.company, "default_currency")
	accounts = doc.get("accounts") or []
	cash_bank = [row.account for row in accounts if _mode_kind(None, row.account) != "other"]
	mode = _mode_kind(None, cash_bank[0]) if cash_bank else "other"
	context = _common(doc, "journal", flt(doc.get("total_debit")), company_currency)
	context.update(
		{
			"title": LABELS["journal"],
			"mode": mode,
			"voucher_type": doc.get("voucher_type"),
			"cheque_no": doc.get("cheque_no"),
			"cheque_date": doc.get("cheque_date"),
			"narration": doc.get("user_remark"),
			"cost_center": ", ".join(dict.fromkeys(r.cost_center for r in accounts if r.cost_center)),
			"project": ", ".join(dict.fromkeys(r.project for r in accounts if r.project)) or None,
			"bank": None,
			"lines": [
				{
					"account": row.account,
					"party": row.party or "",
					"cost_center": row.cost_center or "",
					"debit": _money(row.debit, company_currency) if flt(row.debit) else "",
					"credit": _money(row.credit, company_currency) if flt(row.credit) else "",
					"remark": row.user_remark or "",
				}
				for row in accounts
			],
			"total_debit": _money(doc.get("total_debit"), company_currency),
			"total_credit": _money(doc.get("total_credit"), company_currency),
		}
	)
	return context
