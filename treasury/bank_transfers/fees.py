"""Bank Transfer fees and limits (Treasury Company Settings: Transfer Fee Rules, Transfer Limits).

Scope: SWIFT is International; otherwise the transfer stays at the Same Bank when both sides are at one Bank
(the Bank links, else the bank codes of the IBANs / SWIFT codes), and goes to an Other Bank when they differ
or nothing tells (conservative: the fee applies). Bank codes: an IBAN carries the bank code in characters
5-8 (e.g. QA58DOHB... = DOHB); IBANs are compared only within one country, an IBAN with a SWIFT code only for
Qatar (QA), where the SWIFT code starts with the same bank code; two SWIFT codes by their first four letters.

A fee rule is chosen per (bank account, method, direction): the exact scope before "Any", an exact charge
bearer before an empty one, then the lowest row. Amounts are in the bank account currency.
"""

import frappe
from frappe import _
from frappe.query_builder.functions import Sum
from frappe.utils import flt, fmt_money, formatdate, getdate

DOCTYPE = "Bank Transfer"
SETTINGS = "Treasury Company Settings"
RULE_DOCTYPE = "Bank Transfer Fee Rule"
LIMIT_DOCTYPE = "Bank Transfer Limit"

DIRECTION = {"Party Payment": "Outgoing", "Internal Transfer": "Outgoing", "Party Receipt": "Incoming"}
SAME, OTHER, INTERNATIONAL, ANY = "Same Bank", "Other Bank", "International", "Any"
SWIFT = "SWIFT"
OUR, SHA, BEN = "OUR", "SHA", "BEN"
# The country whose SWIFT codes start with the bank code of its IBANs.
IBAN_SWIFT_COUNTRIES = ("QA",)
CANCELLED = "Cancelled"

RULE_FIELDS = [
	"idx",
	"scope",
	"charge_bearer",
	"amount_from",
	"amount_to",
	"fixed_fee",
	"fee_percent",
	"min_fee",
	"max_fee",
	"correspondent_fee",
	"expense_account",
]


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------


def iban_bank_code(iban: str | None) -> str | None:
	"""The bank code of an IBAN (characters 5-8), e.g. DOHB of QA58DOHB...; None if it is not an IBAN."""
	value = "".join((iban or "").split()).upper()
	if len(value) >= 8 and value[:2].isalpha():
		return value[4:8]
	return None


def detect_scope(doc) -> str:
	"""Same Bank / Other Bank / International of a transfer (or of a `frappe._dict` with its fields)."""
	if doc.get("method") == SWIFT:
		return INTERNATIONAL
	ours = _bank_account_side(doc.get("bank_account"))
	if doc.get("transfer_type") == "Internal Transfer":
		other = _bank_account_side(doc.get("to_bank_account"))
	else:
		other = _beneficiary_side(doc)
	if ours.bank and other.bank:
		return SAME if ours.bank == other.bank else OTHER
	same = _same_bank_code(ours, other)
	return SAME if same else OTHER


def _side(bank: str | None, iban: str | None, swift: str | None) -> frappe._dict:
	iban = "".join((iban or "").split()).upper()
	swift = "".join((swift or "").split()).upper()
	return frappe._dict(
		bank=bank,
		iban_code=iban_bank_code(iban),
		iban_country=iban[:2] if iban_bank_code(iban) else None,
		swift_code=swift[:4] if len(swift) >= 4 else None,
		swift_country=swift[4:6] if len(swift) >= 6 else None,
	)


def _bank_swift(bank: str | None) -> str | None:
	return frappe.db.get_value("Bank", bank, "swift_number") if bank else None


def _bank_account_side(bank_account: str | None) -> frappe._dict:
	values = frappe._dict()
	if bank_account:
		values = frappe.db.get_value("Bank Account", bank_account, ["bank", "iban"], as_dict=True) or values
	return _side(values.get("bank"), values.get("iban"), _bank_swift(values.get("bank")))


def _beneficiary_side(doc) -> frappe._dict:
	account = frappe._dict()
	if doc.get("party_bank_account"):
		account = (
			frappe.db.get_value("Bank Account", doc.party_bank_account, ["bank", "iban"], as_dict=True) or account
		)
	bank = doc.get("beneficiary_bank") or account.get("bank")
	return _side(
		bank, doc.get("beneficiary_iban") or account.get("iban"), doc.get("beneficiary_swift") or _bank_swift(bank)
	)


def _same_bank_code(ours: frappe._dict, other: frappe._dict) -> bool:
	"""Whether the bank codes say both sides are at one bank (False when nothing can be compared)."""
	if ours.iban_code and other.iban_code:
		return ours.iban_country == other.iban_country and ours.iban_code == other.iban_code
	for iban_side, swift_side in ((ours, other), (other, ours)):
		if iban_side.iban_code and swift_side.swift_code:
			return (
				iban_side.iban_country in IBAN_SWIFT_COUNTRIES
				and swift_side.swift_country == iban_side.iban_country
				and iban_side.iban_code == swift_side.swift_code
			)
	if ours.swift_code and other.swift_code:
		return ours.swift_code == other.swift_code
	return False


# ---------------------------------------------------------------------------
# Fee rules
# ---------------------------------------------------------------------------


def find_rule(
	company: str,
	bank_account: str,
	method: str,
	direction: str,
	scope: str,
	amount: float,
	charge_bearer: str | None = None,
) -> frappe._dict | None:
	"""The Transfer Fee Rule of the transfer (see the module docstring), or None."""
	if not (company and bank_account and method and direction):
		return None
	amount = flt(amount)
	rows = frappe.get_all(
		RULE_DOCTYPE,
		filters={
			"parenttype": SETTINGS,
			"parent": company,
			"bank_account": bank_account,
			"method": method,
			"direction": direction,
			"scope": ["in", [scope, ANY]],
		},
		fields=RULE_FIELDS,
		order_by="idx asc",
	)
	candidates = [
		row
		for row in rows
		if (row.charge_bearer or "") in ("", charge_bearer or "")
		and flt(row.amount_from) <= amount
		and (not flt(row.amount_to) or amount <= flt(row.amount_to))
	]
	candidates.sort(key=lambda row: (row.scope != scope, not row.charge_bearer, row.idx))
	return candidates[0] if candidates else None


def compute_fee(
	company: str,
	bank_account: str,
	method: str,
	direction: str,
	scope: str,
	amount: float,
	charge_bearer: str | None = None,
) -> frappe._dict:
	"""{fee (correspondent fee included), correspondent_fee, account, rule_idx, basis} in the bank currency."""
	precision = frappe.get_precision(DOCTYPE, "computed_fee") or 2
	result = frappe._dict(fee=0.0, correspondent_fee=0.0, account=None, rule_idx=None, basis="")
	if method == SWIFT and charge_bearer == BEN:
		# the bank deducts its fee from the amount sent: our books show the full amount and no fee
		result.basis = _("Charges borne by the beneficiary (BEN).")
		return result
	rule = find_rule(company, bank_account, method, direction, scope, amount, charge_bearer)
	if not rule:
		result.basis = _("No fee rule matched.")
		return result

	fee = flt(rule.fixed_fee) + flt(amount) * flt(rule.fee_percent) / 100
	if flt(rule.min_fee) and fee < flt(rule.min_fee):
		fee = flt(rule.min_fee)
	if flt(rule.max_fee) and fee > flt(rule.max_fee):
		fee = flt(rule.max_fee)
	correspondent = flt(rule.correspondent_fee) if method == SWIFT and charge_bearer == OUR else 0.0
	result.update(
		fee=flt(fee + correspondent, precision),
		correspondent_fee=flt(correspondent, precision),
		account=rule.expense_account,
		rule_idx=rule.idx,
		basis=_("Rule #{0}: {1} fixed + {2}% (min {3}, max {4}) + correspondent {5}").format(
			rule.idx,
			_number(rule.fixed_fee, precision),
			_number(rule.fee_percent, 4),
			_number(rule.min_fee, precision),
			_number(rule.max_fee, precision),
			_number(correspondent, precision),
		),
	)
	return result


def _number(value, precision: int) -> str:
	return frappe.format_value(flt(value, precision), {"fieldtype": "Float", "precision": precision})


# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------


def get_limit(company: str, bank_account: str, method: str) -> frappe._dict | None:
	rows = frappe.get_all(
		LIMIT_DOCTYPE,
		filters={"parenttype": SETTINGS, "parent": company, "bank_account": bank_account, "method": method},
		fields=["per_transaction_limit", "daily_limit"],
		order_by="idx asc",
		limit=1,
	)
	return rows[0] if rows else None


def check_limits(doc, execution_date=None) -> list[str]:
	"""Messages for the per-transaction and daily limits of (bank account, method) the transfer exceeds on
	its execution date (or `execution_date`, e.g. a reschedule); empty when it is within its limits."""
	limit = get_limit(doc.company, doc.bank_account, doc.method)
	if not limit:
		return []
	messages = []
	amount, currency = flt(doc.amount), doc.currency
	method = _(doc.method, context=DOCTYPE)
	bank_account = frappe.bold(doc.bank_account)
	per_transaction = flt(limit.per_transaction_limit)
	if per_transaction > 0 and amount > per_transaction:
		messages.append(
			_("Amount {0} exceeds the per-transaction {1} limit {2} of Bank Account {3}.").format(
				fmt_money(amount, currency=currency),
				method,
				fmt_money(per_transaction, currency=currency),
				bank_account,
			)
		)
	daily = flt(limit.daily_limit)
	if daily > 0:
		date = getdate(execution_date or doc.execution_date)
		used = used_on(doc, date)
		if used + amount > daily:
			messages.append(
				_("Transfers of {0} on {1} would reach {2}, above the daily {3} limit {4} of Bank Account {5}.").format(
					fmt_money(used, currency=currency),
					formatdate(date),
					fmt_money(used + amount, currency=currency),
					method,
					fmt_money(daily, currency=currency),
					bank_account,
				)
			)
	return messages


def used_on(doc, date) -> float:
	"""Amount of the other submitted, not cancelled transfers of the bank account and method on `date`."""
	transfer = frappe.qb.DocType(DOCTYPE)
	result = (
		frappe.qb.from_(transfer)
		.select(Sum(transfer.amount))
		.where(
			(transfer.docstatus == 1)
			& (transfer.bank_account == doc.bank_account)
			& (transfer.method == doc.method)
			& (transfer.execution_date == getdate(date))
			& (transfer.status != CANCELLED)
			& (transfer.name != (doc.get("name") or ""))
		)
	).run()
	return flt(result[0][0]) if result else 0.0
