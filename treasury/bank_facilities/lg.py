"""Letter of Guarantee lifecycle (PLAN.md §8), after its submit (= the issue).

Issue (submit): the open amount (the guaranteed amount) is checked against the facility's Letter of Guarantee
sub-limit and total limit (facility row locked); Journal Entries: cash margin Dr Margin / Cr Bank and
commission Dr Commission / Cr Bank. The guaranteed amount itself is off-balance sheet and never booked.

Actions (whitelisted methods of the controller; Treasury Manager, write permission):
- Extend: a new (later) expiry date; an extra commission Dr Commission / Cr Bank.
- Reduce: a lower open amount; the cash margin is released pro rata: Dr Bank / Cr Margin.
- Release: the bank returns the guarantee; the whole remaining margin is released: Dr Bank / Cr Margin.
- Claim (liquidation by the beneficiary): the claim is debited to the account the user chooses (an expense /
  claim account, or the beneficiary's party account); the margin pays first (Cr Margin), the bank funds the
  rest (Cr the Liability Account of the Letter of Guarantee sub-limit — a facility liability that counts in
  the utilisation until repaid). Any margin left over returns to the bank (Dr Bank). One Journal Entry.
- Repay Claim: the company repays the bank-funded part: Dr Liability / Cr Bank.
- Expiry (daily job): Issued / Extended / Reduced guarantees past their effective expiry date become Expired.
  No accounting: releasing the margin stays a manual action (the facility digest lists expired guarantees
  whose margin is still held).

Utilisation (hook `treasury_facility_utilisation_sources`): the open amount of the Issued / Extended / Reduced
guarantees (on a past date: from the status log), plus the bank-funded claims not repaid (the GL balance of
the Liability Account in the Journal Entries of the guarantee).
"""

import frappe
from frappe import _
from frappe.utils import add_days, flt, formatdate, getdate

import erpnext
from treasury.accounting import posting
from treasury.bank_facilities import accounting, instruments
from treasury.bank_facilities.utilisation import get_source_balances

DOCTYPE = "Letter of Guarantee"
LIMIT_TYPE = "Letter of Guarantee"

DRAFT = "Draft"
ISSUED = "Issued"
EXTENDED = "Extended"
REDUCED = "Reduced"
RELEASED = "Released"
CLAIMED = "Claimed"
EXPIRED = "Expired"
CANCELLED = "Cancelled"
# Guarantees the bank is still committed to: they count against the facility.
OPEN_STATUSES = (ISSUED, EXTENDED, REDUCED)

EVENT_MARGIN = "lg_margin"
EVENT_COMMISSION = "lg_commission"
EVENT_EXTENSION_COMMISSION = "lg_extension_commission"
EVENT_MARGIN_RELEASE = "lg_margin_release"
EVENT_CLAIM = "lg_claim"
EVENT_CLAIM_REPAYMENT = "lg_claim_repayment"


# ---------------------------------------------------------------------------
# Issue / cancel (controller hooks)
# ---------------------------------------------------------------------------


def issue(doc):
	"""on_submit: margin and commission Journal Entries, status Issued."""
	values = {
		"open_amount": doc.amount,
		"effective_expiry_date": doc.expiry_date,
		"margin_held": doc.cash_margin_amount,
	}
	margin_journal = commission_journal = None
	if flt(doc.cash_margin_amount) > 0:
		margin_journal = instruments.post_margin(doc, EVENT_MARGIN, doc.issue_date, doc.cash_margin_amount)
	if flt(doc.commission_amount) > 0:
		commission_journal = instruments.post_bank_expense(
			doc,
			EVENT_COMMISSION,
			doc.issue_date,
			doc.commission_amount,
			doc.commission_account,
			_("Commission of {0} {1}").format(_(DOCTYPE), doc.name),
		)
	doc.update(values)
	values.update(margin_journal=margin_journal, commission_journal=commission_journal)
	instruments.log_now(
		doc,
		"Issued",
		ISSUED,
		doc.issue_date,
		("Journal Entry", margin_journal or commission_journal),
		values=values,
	)


def cancel(doc):
	"""on_cancel: every voucher of the guarantee is cancelled (newest first)."""
	accounting.cancel_vouchers(DOCTYPE, doc.name)
	doc.open_amount = 0
	instruments.log_now(doc, "Cancelled", CANCELLED, values={"open_amount": 0})


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


def extend(name: str, new_expiry_date, extension_date=None, extra_commission=0, remarks=None) -> dict:
	doc = instruments.begin(DOCTYPE, name)
	instruments.require_status(doc, (*OPEN_STATUSES, EXPIRED))
	date = instruments.action_date(extension_date)
	instruments.not_before(doc, date, "issue_date")
	if not new_expiry_date:
		frappe.throw(_("Enter the new Expiry Date."))
	new_expiry = getdate(new_expiry_date)
	if new_expiry <= getdate(doc.effective_expiry_date):
		frappe.throw(
			_("The new Expiry Date ({0}) must be after the current one ({1}).").format(
				formatdate(new_expiry), formatdate(doc.effective_expiry_date)
			)
		)
	if new_expiry < date:
		frappe.throw(_("The new Expiry Date cannot be before the extension date."))
	commission = flt(extra_commission, doc.precision("commission_amount"))
	if commission < 0:
		frappe.throw(_("The commission cannot be negative."))

	journal = None
	if commission:
		if not doc.commission_account:
			frappe.throw(_("Commission Account is required for the commission."))
		journal = instruments.post_bank_expense(
			doc,
			EVENT_EXTENSION_COMMISSION,
			date,
			commission,
			doc.commission_account,
			_("Extension commission of {0} {1}").format(_(DOCTYPE), doc.name),
			rate=instruments.event_rate(doc, date),
			cycle=instruments.next_cycle(doc, EVENT_EXTENSION_COMMISSION),
		)
	old_expiry = doc.effective_expiry_date
	doc.effective_expiry_date = new_expiry
	note = f"{formatdate(old_expiry)} → {formatdate(new_expiry)}" + (f": {remarks}" if remarks else "")
	instruments.log(doc, "Extended", EXTENDED, date, ("Journal Entry", journal), note)
	return instruments.save(doc)


def reduce(name: str, new_amount, reduction_date=None, exchange_rate=None, remarks=None) -> dict:
	"""A lower guaranteed amount; the margin held is released in proportion to the reduction."""
	doc = instruments.begin(DOCTYPE, name)
	instruments.require_status(doc, OPEN_STATUSES)
	date = instruments.action_date(reduction_date)
	instruments.not_before(doc, date, "issue_date")
	precision = doc.precision("amount")
	new_amount = flt(new_amount, precision)
	current = flt(doc.open_amount, precision)
	if not 0 < new_amount < current:
		frappe.throw(
			_("The new amount must be greater than zero and less than the open amount {0}.").format(current)
		)
	release = flt(flt(doc.margin_held) * (current - new_amount) / current, precision)
	journal = None
	if release > 0:
		journal = instruments.post_margin(
			doc,
			EVENT_MARGIN_RELEASE,
			date,
			release,
			release=True,
			rate=instruments.event_rate(doc, date, exchange_rate),
			cycle=instruments.next_cycle(doc, EVENT_MARGIN_RELEASE),
		)
	doc.margin_held = flt(flt(doc.margin_held) - release, precision)
	doc.open_amount = new_amount
	note = f"{current} → {new_amount}" + (f": {remarks}" if remarks else "")
	instruments.log(doc, "Reduced", REDUCED, date, ("Journal Entry", journal), note)
	return instruments.save(doc)


def release(name: str, release_date=None, exchange_rate=None, remarks=None) -> dict:
	"""The guarantee is returned: nothing open any more, the whole margin held comes back to the bank."""
	doc = instruments.begin(DOCTYPE, name)
	instruments.require_status(doc, (*OPEN_STATUSES, EXPIRED), RELEASED)
	date = instruments.action_date(release_date)
	instruments.not_before(doc, date, "issue_date")
	journal = None
	if flt(doc.margin_held) > 0:
		journal = instruments.post_margin(
			doc,
			EVENT_MARGIN_RELEASE,
			date,
			doc.margin_held,
			release=True,
			rate=instruments.event_rate(doc, date, exchange_rate),
			cycle=instruments.next_cycle(doc, EVENT_MARGIN_RELEASE),
		)
	doc.margin_held = 0
	doc.open_amount = 0
	doc.release_date = date
	instruments.log(doc, "Released", RELEASED, date, ("Journal Entry", journal), remarks)
	return instruments.save(doc)


def claim(
	name: str,
	amount,
	counter_account: str,
	claim_date=None,
	party_type: str | None = None,
	party: str | None = None,
	exchange_rate=None,
	remarks=None,
) -> dict:
	"""The beneficiary liquidates the guarantee for `amount` (at most the open amount): see the module doc."""
	doc = instruments.begin(DOCTYPE, name)
	instruments.require_status(doc, (*OPEN_STATUSES, EXPIRED), CLAIMED)
	date = instruments.action_date(claim_date)
	instruments.not_before(doc, date, "issue_date")
	precision = doc.precision("amount")
	amount = flt(amount, precision)
	if not 0 < amount <= flt(doc.open_amount, precision):
		frappe.throw(
			_("The claim amount must be greater than zero and at most the open amount {0}.").format(
				flt(doc.open_amount, precision)
			)
		)
	if not counter_account:
		frappe.throw(_("Select the account to debit with the claim."))

	rate = instruments.event_rate(doc, date, exchange_rate)
	margin = flt(doc.margin_held, precision)
	from_margin = min(margin, amount)
	from_bank = flt(amount - from_margin, precision)
	leftover = flt(margin - from_margin, precision)
	liability = _claim_liability_account(doc) if from_bank else None

	company, currency = doc.company, doc.currency
	rows = [_counter_row(doc, counter_account, amount, rate, party_type, party)]
	if margin:
		rows.append(accounting._row(company, currency, doc.margin_account, doc.exchange_rate, credit=margin))
	if from_bank:
		rows.append(accounting._row(company, currency, liability, rate, credit=from_bank))
	bank_gl = accounting.bank_gl_account(doc.bank_account)
	if leftover:
		rows.append(accounting._row(company, currency, bank_gl, rate, debit=leftover))
	journal = posting.create_journal_entry(
		source_doctype=DOCTYPE,
		source_name=doc.name,
		event=EVENT_CLAIM,
		company=company,
		posting_date=date,
		accounts=rows,
		cheque_no=doc.lg_no or doc.name,
		cheque_date=date,
		clearance_date=date if leftover else None,
		remarks=_("Claim of {0} {1}").format(_(DOCTYPE), doc.name),
		balance_exchange_difference=True,
	)
	doc.update(
		{
			"claim_date": date,
			"claim_amount": amount,
			"claim_account": counter_account,
			"claim_outstanding": from_bank,
			"margin_held": 0,
			"open_amount": 0,
		}
	)
	instruments.log(doc, "Claimed", CLAIMED, date, ("Journal Entry", journal), remarks)
	return instruments.save(doc)


def repay_claim(name: str, amount=None, repayment_date=None, exchange_rate=None, remarks=None) -> dict:
	"""Repay (part of) the bank-funded claim: Dr Liability / Cr Bank."""
	doc = instruments.begin(DOCTYPE, name)
	instruments.require_status(doc, (CLAIMED,))
	precision = doc.precision("amount")
	outstanding = flt(doc.claim_outstanding, precision)
	if outstanding <= 0:
		frappe.throw(_("Nothing of the claim of {0} is owed to the bank.").format(frappe.bold(doc.name)))
	amount = flt(amount, precision) if amount not in (None, "") else outstanding
	if not 0 < amount <= outstanding:
		frappe.throw(
			_("The amount must be greater than zero and at most the amount owed {0}.").format(outstanding)
		)
	date = instruments.action_date(repayment_date)
	instruments.not_before(doc, date, "issue_date")
	rate = instruments.event_rate(doc, date, exchange_rate)
	company, currency = doc.company, doc.currency
	journal = posting.create_journal_entry(
		source_doctype=DOCTYPE,
		source_name=doc.name,
		event=EVENT_CLAIM_REPAYMENT,
		company=company,
		posting_date=date,
		accounts=[
			accounting._row(company, currency, _claim_liability_account(doc), rate, debit=amount),
			accounting._row(company, currency, accounting.bank_gl_account(doc.bank_account), rate, credit=amount),
		],
		cheque_no=doc.lg_no or doc.name,
		cheque_date=date,
		clearance_date=date,
		remarks=_("Repayment of the claim of {0} {1}").format(_(DOCTYPE), doc.name),
		voucher_type="Bank Entry",
		cycle=instruments.next_cycle(doc, EVENT_CLAIM_REPAYMENT),
	)
	doc.claim_outstanding = flt(outstanding - amount, precision)
	instruments.log(doc, "Claim Repaid", None, date, ("Journal Entry", journal), remarks)
	return instruments.save(doc)


def expire(name: str, as_of) -> bool:
	"""System action of the daily job (no accounting). The guarantee is valid through its effective expiry
	date: it expires the day after."""
	doc = instruments.begin(DOCTYPE, name, system=True)
	if doc.status not in OPEN_STATUSES or getdate(doc.effective_expiry_date) >= getdate(as_of):
		return False
	instruments.log(doc, "Expired", EXPIRED, add_days(doc.effective_expiry_date, 1))
	instruments.save(doc)
	return True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _claim_liability_account(doc) -> str:
	facility = frappe.get_doc("Bank Facility", doc.facility)
	limit = instruments.get_limit(facility, LIMIT_TYPE)
	if not limit or not limit.liability_account:
		frappe.throw(
			_(
				"Set the Liability Account of the {0} sub-limit of Bank Facility {1}: the bank-funded part of a claim is booked on it."
			).format(_(LIMIT_TYPE, context="Bank Facility"), frappe.bold(doc.facility)),
			title=_("Missing Settings"),
		)
	return limit.liability_account


def _counter_row(doc, account: str, amount: float, rate: float, party_type=None, party=None) -> dict:
	"""Debit of the claim account: in the guarantee currency, or converted when it is in the company currency."""
	company_currency = erpnext.get_company_currency(doc.company)
	details = instruments.validate_account(doc.company, account, _("Claim Account"), None, None, allow_party=True)
	if details.account_currency not in (doc.currency, company_currency):
		frappe.throw(
			_("The Claim Account must be in {0} or in the company currency {1}.").format(
				doc.currency, company_currency
			)
		)
	if details.account_type in ("Payable", "Receivable"):
		if party_type not in ("Customer", "Supplier") or not party:
			frappe.throw(_("Select the party of the Claim Account {0}.").format(frappe.bold(account)))
		if not frappe.db.exists(party_type, party):
			frappe.throw(_("{0} {1} does not exist").format(_(party_type), party))
	elif party_type or party:
		frappe.throw(_("A party can only be given with a Payable or Receivable Claim Account."))

	if details.account_currency == doc.currency:
		row = accounting._row(doc.company, doc.currency, account, rate, debit=amount)
	else:
		row = {
			"account": account,
			"debit_in_account_currency": flt(flt(amount) * flt(rate), accounting._precision()),
		}
	if details.root_type in ("Expense", "Income"):
		row["cost_center"] = erpnext.get_default_cost_center(doc.company)
	if details.account_type in ("Payable", "Receivable"):
		row.update(party_type=party_type, party=party)
	return row


# ---------------------------------------------------------------------------
# Utilisation, alerts, reports
# ---------------------------------------------------------------------------


def get_lg_utilisation(facility, as_on=None) -> list[dict]:
	"""Hook `treasury_facility_utilisation_sources` (see the module docstring)."""
	open_amounts = instruments.open_amounts(DOCTYPE, facility.name, OPEN_STATUSES, as_on)
	amount = sum(open_amounts.values())
	# bank-funded claims not repaid yet: the liability balance on the vouchers of the facility's guarantees
	guarantees = frappe.get_all(DOCTYPE, filters={"facility": facility.name, "docstatus": 1}, pluck="name")
	limit = instruments.get_limit(facility, LIMIT_TYPE)
	if guarantees and limit and limit.liability_account:
		balances = get_source_balances(
			[(DOCTYPE, name) for name in guarantees], [limit.liability_account], as_on, credit_minus_debit=True
		)
		amount += sum(balances.values())
	if not amount:
		return []
	return [{"limit_type": LIMIT_TYPE, "amount": flt(amount)}]


def get_expiring(as_of, days: int) -> list[frappe._dict]:
	"""Open guarantees whose effective expiry date is within `days` of `as_of`, and expired guarantees whose
	cash margin is still held (release is manual)."""
	fields = [
		"name",
		"company",
		"facility",
		"bank",
		"lg_no",
		"beneficiary_name",
		"currency",
		"open_amount",
		"amount",
		"margin_held",
		"effective_expiry_date",
		"status",
	]
	expiring = frappe.get_all(
		DOCTYPE,
		filters={
			"docstatus": 1,
			"status": ["in", OPEN_STATUSES],
			"effective_expiry_date": ["<=", add_days(getdate(as_of), days)],
		},
		fields=fields,
		order_by="effective_expiry_date asc, name asc",
	)
	expired = frappe.get_all(
		DOCTYPE,
		filters={"docstatus": 1, "status": EXPIRED, "margin_held": [">", 0]},
		fields=fields,
		order_by="effective_expiry_date asc, name asc",
	)
	return expiring + expired
