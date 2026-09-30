"""Letter of Credit lifecycle (PLAN.md §8), after its submit (= the opening).

Open (submit): the amount is checked against the facility's Letter of Credit sub-limit and total limit
(facility row locked); Journal Entries: cash margin Dr Margin / Cr Bank and commission Dr Commission / Cr Bank.

Actions (whitelisted methods of the controller; Treasury Manager, write permission):
- Amend (Opened / Amended): a new amount and / or a later expiry date, and amendment charges (Dr Charges /
  Cr Bank). An increase is checked against the available limit; the cash margin follows the new amount (top-up
  Dr Margin / Cr Bank, or release Dr Bank / Cr Margin).
- Receive Documents: the documents amount (up to the amount plus the tolerance) becomes the open amount;
  documents charges Dr Charges / Cr Bank. Documents above the open amount (within the tolerance) are checked
  against the available limit, like an amendment increase.
- Accept (Usance): the maturity date (default: acceptance + usance days) — the expected outflow of the Bank
  Position Forecast (`get_flows`: documents amount less the margin held, on the LC's bank account).
- Settle (Sight: after the documents; Usance: after the acceptance): the whole margin held returns to the
  bank (Dr Bank / Cr Margin, at the settlement rate) and a Payment Entry pays the supplier from the bank
  (optionally allocated to a Purchase Invoice). ERPNext's Payment Entry allocates the invoice and books the
  exchange gain / loss of a foreign currency invoice; a Journal Entry would not (a Journal Entry row that
  references an invoice is forced to the invoice's rate). Both vouchers are cleared on the settlement date
  (the bank statement shows one net debit, amount less margin: see the README). The credit closes: the
  amount not drawn (credit amount less settled amount) is recorded as the Unused Amount and no longer counts
  against the facility; the margin held was already returned in full. With "Finance by Trust Receipt" the
  bank lends the settlement less the margin held: a Trust Receipt drawdown is created and submitted in the same
  transaction (treasury.bank_facilities.trust_receipt, spec 7g §3.9).
- Release Margin (Expired): the margin of an expired, unused credit returns to the bank.
- Expiry (daily job): Opened / Amended credits past their effective expiry date become Expired (no
  accounting). Credits with documents received never expire: they are settled.

Utilisation (hook `treasury_facility_utilisation_sources`): the open amount of the Opened / Amended /
Documents Received / Accepted credits (on a past date: from the status log).
"""

import erpnext
import frappe
from frappe import _
from frappe.utils import add_days, cint, flt, formatdate, getdate

from treasury.accounting import payment_modes, posting
from treasury.bank_facilities import accounting, instruments

DOCTYPE = "Letter of Credit"
LIMIT_TYPE = "Letter of Credit"

DRAFT = "Draft"
OPENED = "Opened"
AMENDED = "Amended"
DOCUMENTS_RECEIVED = "Documents Received"
ACCEPTED = "Accepted"
SETTLED = "Settled"
EXPIRED = "Expired"
CANCELLED = "Cancelled"
SIGHT = "Sight"
USANCE = "Usance"
# Credits the bank is still committed to: they count against the facility.
OPEN_STATUSES = (OPENED, AMENDED, DOCUMENTS_RECEIVED, ACCEPTED)
# Before any documents: the terms may still be amended, and the credit may expire.
AMENDABLE_STATUSES = (OPENED, AMENDED)

EVENT_MARGIN = "lc_margin"
EVENT_COMMISSION = "lc_commission"
EVENT_MARGIN_TOPUP = "lc_margin_topup"
EVENT_MARGIN_RELEASE = "lc_margin_release"
EVENT_AMENDMENT_CHARGES = "lc_amendment_charges"
EVENT_DOCUMENT_CHARGES = "lc_document_charges"
EVENT_SETTLEMENT = "lc_settlement"
TR_SAVEPOINT = "treasury_lc_trust_receipt"


# ---------------------------------------------------------------------------
# Open / cancel (controller hooks)
# ---------------------------------------------------------------------------


def open_credit(doc):
	"""on_submit: margin and commission Journal Entries, status Opened."""
	values = {
		"current_amount": doc.amount,
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
		"Opened",
		OPENED,
		doc.issue_date,
		("Journal Entry", margin_journal or commission_journal),
		values=values,
	)


def cancel(doc):
	cancel_trust_receipt(doc)
	accounting.cancel_vouchers(DOCTYPE, doc.name)
	doc.open_amount = 0
	instruments.log_now(doc, "Cancelled", CANCELLED, values={"open_amount": 0})


def cancel_trust_receipt(doc):
	"""The Trust Receipt drawdown of the credit is cancelled first (it has no repayments yet); the credit's
	cancellation (a Treasury Manager's) authorises it."""
	name = doc.get("trust_receipt_drawdown")
	if not name or frappe.db.get_value("Facility Drawdown", name, "docstatus") != 1:
		return
	if frappe.db.exists("Facility Repayment", {"drawdown": name, "docstatus": 1}):
		frappe.throw(
			_("Cancel the repayments of Trust Receipt {0} first.").format(frappe.bold(name)),
			title=_("Not Allowed"),
		)
	drawdown = frappe.get_doc("Facility Drawdown", name)
	drawdown.flags.ignore_permissions = True
	# the credit linking it (`trust_receipt_drawdown`) is the document being cancelled
	drawdown.flags.ignore_links = True
	drawdown.cancel()


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


def amend(
	name: str,
	amendment_date=None,
	new_amount=None,
	new_expiry_date=None,
	charges=0,
	exchange_rate=None,
	remarks=None,
) -> dict:
	doc = instruments.begin(DOCTYPE, name)
	instruments.require_status(doc, AMENDABLE_STATUSES)
	date = instruments.action_date(amendment_date)
	instruments.not_before(doc, date, "issue_date")
	precision = doc.precision("amount")
	current = flt(doc.current_amount, precision)
	new_amount = flt(new_amount, precision) if new_amount not in (None, "") else current
	new_expiry = getdate(new_expiry_date) if new_expiry_date else getdate(doc.effective_expiry_date)
	charges = flt(charges, precision)
	if new_amount <= 0:
		frappe.throw(_("Amount must be greater than zero."))
	if new_expiry < getdate(doc.effective_expiry_date):
		frappe.throw(_("The new Expiry Date cannot be before the current one."))
	if new_expiry < date:
		frappe.throw(_("The new Expiry Date cannot be before the amendment date."))
	if charges < 0:
		frappe.throw(_("The charges cannot be negative."))
	if new_amount == current and new_expiry == getdate(doc.effective_expiry_date) and not charges:
		frappe.throw(_("Nothing to amend: enter a new amount, a new expiry date or charges."))

	if new_amount > current:
		facility = instruments.lock_facility(doc.facility)
		instruments.validate_facility_open(facility, date, _(DOCTYPE))
		instruments.check_available(
			facility, LIMIT_TYPE, flt(new_amount - current, precision), doc.currency, _("The increase")
		)
	rate = instruments.event_rate(doc, date, exchange_rate)
	journals = []
	target = flt(new_amount * flt(doc.cash_margin_percent) / 100, precision)
	delta = flt(target - flt(doc.margin_held), precision)
	if delta > 0:
		journals.append(
			instruments.post_margin(
				doc,
				EVENT_MARGIN_TOPUP,
				date,
				delta,
				rate=rate,
				cycle=instruments.next_cycle(doc, EVENT_MARGIN_TOPUP),
			)
		)
	elif delta < 0:
		journals.append(
			instruments.post_margin(
				doc,
				EVENT_MARGIN_RELEASE,
				date,
				-delta,
				release=True,
				rate=rate,
				cycle=instruments.next_cycle(doc, EVENT_MARGIN_RELEASE),
			)
		)
	if charges:
		journals.append(
			instruments.post_bank_expense(
				doc,
				EVENT_AMENDMENT_CHARGES,
				date,
				charges,
				_charges_account(doc),
				_("Amendment charges of {0} {1}").format(_(DOCTYPE), doc.name),
				rate=rate,
				cycle=instruments.next_cycle(doc, EVENT_AMENDMENT_CHARGES),
			)
		)
	changes = []
	if new_amount != current:
		changes.append(f"{_('Amount', context='Treasury')}: {current} → {new_amount}")
	if new_expiry != getdate(doc.effective_expiry_date):
		changes.append(
			f"{_('Expiry Date', context='Treasury')}: "
			f"{formatdate(doc.effective_expiry_date)} → {formatdate(new_expiry)}"
		)
	doc.current_amount = new_amount
	doc.open_amount = new_amount
	doc.effective_expiry_date = new_expiry
	doc.margin_held = target
	note = "; ".join(changes) + (f": {remarks}" if remarks else "")
	instruments.log(doc, "Amended", AMENDED, date, ("Journal Entry", journals[0] if journals else None), note)
	return instruments.save(doc)


def receive_documents(
	name: str, documents_date=None, documents_amount=None, charges=0, exchange_rate=None, remarks=None
) -> dict:
	doc = instruments.begin(DOCTYPE, name)
	instruments.require_status(doc, AMENDABLE_STATUSES, DOCUMENTS_RECEIVED)
	date = instruments.action_date(documents_date)
	instruments.not_before(doc, date, "issue_date")
	precision = doc.precision("amount")
	amount = (
		flt(documents_amount, precision) if documents_amount not in (None, "") else flt(doc.current_amount)
	)
	ceiling = flt(flt(doc.current_amount) * (1 + flt(doc.tolerance_percent) / 100), precision)
	if not 0 < amount <= ceiling:
		frappe.throw(
			_(
				"The documents amount must be greater than zero and at most {0} (amount plus tolerance)."
			).format(ceiling)
		)
	charges = flt(charges, precision)
	if charges < 0:
		frappe.throw(_("The charges cannot be negative."))
	increase = flt(amount - flt(doc.open_amount), precision)
	if increase > 0:
		# documents within the tolerance above the credit amount: the bank's commitment grows
		facility = instruments.lock_facility(doc.facility)
		instruments.validate_facility_open(facility, date, _(DOCTYPE))
		instruments.check_available(facility, LIMIT_TYPE, increase, doc.currency, _("The tolerance increase"))
	journal = None
	if charges:
		journal = instruments.post_bank_expense(
			doc,
			EVENT_DOCUMENT_CHARGES,
			date,
			charges,
			_charges_account(doc),
			_("Documents charges of {0} {1}").format(_(DOCTYPE), doc.name),
			rate=instruments.event_rate(doc, date, exchange_rate),
		)
	doc.documents_date = date
	doc.documents_amount = amount
	doc.open_amount = amount
	instruments.log(doc, "Documents Received", DOCUMENTS_RECEIVED, date, ("Journal Entry", journal), remarks)
	return instruments.save(doc)


def accept(name: str, acceptance_date=None, maturity_date=None, remarks=None) -> dict:
	doc = instruments.begin(DOCTYPE, name)
	if doc.lc_type != USANCE:
		frappe.throw(_("Only a Usance Letter of Credit is accepted; settle a Sight credit directly."))
	instruments.require_status(doc, (DOCUMENTS_RECEIVED,), ACCEPTED)
	date = instruments.action_date(acceptance_date)
	instruments.not_before(doc, date, "issue_date")
	maturity = getdate(maturity_date) if maturity_date else getdate(add_days(date, cint(doc.usance_days)))
	if maturity <= date:
		frappe.throw(_("The Maturity Date must be after the Acceptance Date."))
	doc.acceptance_date = date
	doc.maturity_date = maturity
	instruments.log(doc, "Accepted", ACCEPTED, date, remarks=f"{_('Maturity Date')}: {formatdate(maturity)}")
	return instruments.save(doc)


def settle(
	name: str,
	settlement_date=None,
	amount=None,
	exchange_rate=None,
	purchase_invoice: str | None = None,
	remarks=None,
	finance_by_trust_receipt=0,
	tr_tenor_months=None,
	tr_first_repayment_date=None,
	tr_interest_rate=None,
) -> dict:
	"""Pay the supplier: see the module docstring."""
	from treasury.bank_facilities import trust_receipt

	doc = instruments.begin(DOCTYPE, name)
	allowed = (ACCEPTED,) if doc.lc_type == USANCE else (DOCUMENTS_RECEIVED,)
	instruments.require_status(doc, allowed, SETTLED)
	date = instruments.action_date(settlement_date)
	instruments.not_before(doc, date, "issue_date")
	precision = doc.precision("amount")
	amount = flt(amount, precision) if amount not in (None, "") else flt(doc.documents_amount, precision)
	if not 0 < amount <= flt(doc.documents_amount, precision):
		frappe.throw(
			_("The settlement amount must be greater than zero and at most the documents amount {0}.").format(
				flt(doc.documents_amount, precision)
			)
		)
	rate = instruments.event_rate(doc, date, exchange_rate)
	if not cint(finance_by_trust_receipt):
		return _settle(doc, date, amount, rate, purchase_invoice, remarks)
	tr_amount, tr_tenor = trust_receipt.validate_financing(doc, amount, tr_tenor_months)
	# atomic: a Trust Receipt refused (e.g. above the available limit) undoes the whole settlement
	frappe.db.savepoint(TR_SAVEPOINT)
	try:
		# the credit is closed (open amount 0) before the Trust Receipt is checked against the facility
		_settle(doc, date, amount, rate, purchase_invoice, remarks)
		drawdown = trust_receipt.create_from_lc(
			doc, date, tr_amount, rate, tr_tenor, tr_first_repayment_date or None, tr_interest_rate
		)
		vouchers = frappe.db.get_value(
			"Facility Drawdown", drawdown, ["payment_entry", "journal_entry"], as_dict=True
		)
		if vouchers.payment_entry:
			voucher = ("Payment Entry", vouchers.payment_entry)
		else:
			voucher = ("Journal Entry", vouchers.journal_entry)
		doc.reload()
		doc.trust_receipt_drawdown = drawdown
		instruments.log(doc, "Trust Receipt Drawdown", None, date, voucher, drawdown)
		result = instruments.save(doc)
	except Exception:
		frappe.db.rollback(save_point=TR_SAVEPOINT)
		raise
	frappe.db.release_savepoint(TR_SAVEPOINT)
	return {**result, "trust_receipt_drawdown": drawdown}


def _settle(doc, date, amount: float, rate: float, purchase_invoice: str | None, remarks) -> dict:
	"""The margin release, the supplier's Payment Entry and the credit's closing (see `settle`)."""
	precision = doc.precision("amount")
	references = _invoice_reference(doc, purchase_invoice, amount, rate)

	margin_journal = None
	if flt(doc.margin_held) > 0:
		margin_journal = instruments.post_margin(
			doc,
			EVENT_MARGIN_RELEASE,
			date,
			doc.margin_held,
			release=True,
			rate=rate,
			cycle=instruments.next_cycle(doc, EVENT_MARGIN_RELEASE),
		)
	payment_entry = posting.create_payment_entry(
		source_doctype=DOCTYPE,
		source_name=doc.name,
		event=EVENT_SETTLEMENT,
		payment_type="Pay",
		company=doc.company,
		posting_date=date,
		party_type="Supplier",
		party=doc.supplier,
		amount=amount,
		bank_account_head=accounting.bank_gl_account(doc.bank_account),
		reference_no=doc.lc_no or doc.name,
		reference_date=date,
		currency=doc.currency,
		exchange_rate=rate,
		references=references,
		remarks=_("Settlement of {0} {1}").format(_(DOCTYPE), doc.name),
		clearance_date=date,
		details={
			**payment_modes.resolve(doc.company, doc.bank_account, "Bank Transfer"),
			"bank_account": doc.bank_account,
			"cost_center": erpnext.get_default_cost_center(doc.company),
		},
	)
	# the credit closes with the settlement: what was not drawn is no longer committed by the bank
	unused = max(flt(flt(doc.current_amount) - amount, precision), 0)
	doc.settlement_date = date
	doc.settled_amount = amount
	doc.unused_amount = unused
	doc.payment_entry = payment_entry
	doc.margin_held = 0
	doc.open_amount = 0
	if margin_journal:
		instruments.log(doc, "Margin Released", None, date, ("Journal Entry", margin_journal))
	note = _("Unused amount {0} released from the facility").format(unused) if unused else None
	if remarks:
		note = f"{note}: {remarks}" if note else remarks
	instruments.log(doc, "Settled", SETTLED, date, ("Payment Entry", payment_entry), note)
	return instruments.save(doc)


def release_margin(name: str, release_date=None, exchange_rate=None, remarks=None) -> dict:
	"""The margin of an expired credit returns to the bank."""
	doc = instruments.begin(DOCTYPE, name)
	instruments.require_status(doc, (EXPIRED,))
	if flt(doc.margin_held) <= 0:
		frappe.throw(_("{0} {1} holds no cash margin.").format(_(DOCTYPE), frappe.bold(doc.name)))
	date = instruments.action_date(release_date)
	instruments.not_before(doc, date, "issue_date")
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
	instruments.log(doc, "Margin Released", None, date, ("Journal Entry", journal), remarks)
	return instruments.save(doc)


def expire(name: str, as_of) -> bool:
	"""System action of the daily job (no accounting); valid through the effective expiry date."""
	doc = instruments.begin(DOCTYPE, name, system=True)
	if doc.status not in AMENDABLE_STATUSES or getdate(doc.effective_expiry_date) >= getdate(as_of):
		return False
	doc.open_amount = 0
	instruments.log(doc, "Expired", EXPIRED, add_days(doc.effective_expiry_date, 1))
	instruments.save(doc)
	return True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _charges_account(doc) -> str:
	account = doc.charges_account or doc.commission_account
	if not account:
		frappe.throw(
			_("Set the Charges Account (or the Commission Account) of {0}.").format(frappe.bold(doc.name))
		)
	return account


def _invoice_reference(doc, purchase_invoice: str | None, amount: float, rate: float) -> list[dict]:
	"""The allocation of the settlement to a Purchase Invoice of the supplier, in the party account currency
	(posting trims it to the invoice's outstanding; the rest stays an advance)."""
	if not purchase_invoice:
		return []
	invoice = frappe.db.get_value(
		"Purchase Invoice", purchase_invoice, ["supplier", "company", "docstatus"], as_dict=True
	)
	if (
		not invoice
		or invoice.docstatus != 1
		or invoice.company != doc.company
		or invoice.supplier != doc.supplier
	):
		frappe.throw(
			_("Purchase Invoice {0} must be a submitted invoice of supplier {1} in company {2}.").format(
				frappe.bold(purchase_invoice), frappe.bold(doc.supplier), frappe.bold(doc.company)
			)
		)
	party_account = posting.get_party_account("Supplier", doc.supplier, doc.company)
	party_currency = frappe.get_cached_value("Account", party_account, "account_currency")
	allocated = amount if party_currency == doc.currency else flt(amount * rate, accounting._precision())
	return [
		{
			"reference_doctype": "Purchase Invoice",
			"reference_name": purchase_invoice,
			"allocated_amount": allocated,
		}
	]


# ---------------------------------------------------------------------------
# Utilisation, forecast
# ---------------------------------------------------------------------------


def get_lc_utilisation(facility, as_on=None) -> list[dict]:
	"""Hook `treasury_facility_utilisation_sources` (see the module docstring)."""
	amount = sum(instruments.open_amounts(DOCTYPE, facility.name, OPEN_STATUSES, as_on).values())
	return [{"limit_type": LIMIT_TYPE, "amount": flt(amount)}] if amount else []


def get_flows(filters) -> list[dict]:
	"""Hook `treasury_forecast_sources`: accepted Usance credits pay the supplier on their maturity date from
	their bank account (the documents amount less the margin held, which the bank uses first). Sight credits
	are settled when the documents arrive, whose date is not known in advance: they are not forecast."""
	from treasury.bank_facilities.forecast import _rate
	from treasury.pdc.report_utils import get_precision, report_today

	filters = frappe._dict(filters)
	if not filters.bank_accounts or not frappe.has_permission(DOCTYPE, "read"):
		return []
	conditions = {
		"company": filters.company,
		"docstatus": 1,
		"status": ACCEPTED,
		"bank_account": ["in", list(filters.bank_accounts)],
	}
	if filters.to_date:
		conditions["maturity_date"] = ["<=", getdate(filters.to_date)]
	precision = get_precision()
	today = report_today()
	rates: dict[str, float] = {}
	flows = []
	for row in frappe.get_list(
		DOCTYPE,
		filters=conditions,
		fields=[
			"name",
			"company",
			"currency",
			"bank_account",
			"supplier",
			"open_amount",
			"margin_held",
			"maturity_date",
		],
	):
		amount = flt(flt(row.open_amount) - flt(row.margin_held), precision)
		if amount <= 0:
			continue
		flows.append(
			{
				"date": getdate(row.maturity_date),
				"direction": "out",
				"bank_account": row.bank_account,
				"amount": flt(amount * _rate(row.currency, row.company, today, rates), precision),
				"amount_in_account_currency": amount,
				"reference_doctype": DOCTYPE,
				"reference_name": row.name,
				"party": row.supplier,
				"description": _("Letter of Credit maturity"),
			}
		)
	return flows
