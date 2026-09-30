"""PDC Cheque / Cheque Batch mappers for the Treasury buttons of the ERPNext documents (spec 7c §3.3).

Read only, like Frappe's `make_mapped_doc`: they return a new, unsaved document (`__islocal`) that the client
opens with `frappe.model.sync` + `set_route`; nothing is written until the user saves it through the normal
insert path, which runs every validation. A cheque leaf is never allocated here: `PDCCheque.before_insert`
allocates it on save (and a batch's leaves on submit).
"""

import erpnext
import frappe
from erpnext.accounts.doctype.account.account import get_account_currency
from erpnext.accounts.doctype.bank_account.bank_account import get_party_bank_account
from frappe import _
from frappe.contacts.doctype.contact.contact import get_default_contact
from frappe.utils import cint, flt, today

from treasury.accounting import payment_modes, posting
from treasury.pdc import batch
from treasury.pdc.doctype.cheque_batch.cheque_batch import NAMING_SERIES as BATCH_NAMING_SERIES
from treasury.pdc.doctype.pdc_cheque.pdc_cheque import NAMING_SERIES as CHEQUE_NAMING_SERIES
from treasury.utils import source_documents as sd
from treasury.utils.party import can_read_party, get_party_name

CHEQUE = "PDC Cheque"
BATCH = "Cheque Batch"
DEFAULT_INSTALLMENTS = 12
DEFAULT_FREQUENCY = "Monthly"


@frappe.whitelist()
def make_pdc_cheque(
	source_doctype: str,
	source_name: str,
	company: str | None = None,
	party_type: str | None = None,
	party: str | None = None,
) -> dict:
	"""A new PDC Cheque (not inserted) settling the rest of an invoice / order / Journal Entry / hrms document,
	or for a party master (no amount). `company`: for a party master (default: the user's default company).
	`party_type` / `party`: the party row of a Journal Entry with several open parties."""
	source, info, party_currency = _prepare(CHEQUE, source_doctype, source_name, company, party_type, party)
	doc = frappe.new_doc(CHEQUE)
	doc.naming_series = CHEQUE_NAMING_SERIES[info.direction]
	_set_header(doc, source, info, party_currency)
	if info.direction == sd.OUTGOING:
		doc.cheque_book = sd.default_cheque_book(info.company, doc.bank_account)
	doc.cheque_no = None
	doc.original_due_date = sd.default_due_date(source)
	if info.kind != sd.PARTY:
		doc.amount, outstanding = _amounts(doc, source, info, party_currency)
		doc.append(
			"references",
			{
				"reference_doctype": source.doctype,
				"reference_name": source.name,
				"allocated_amount": outstanding,
			},
		)
	return sd.mark_new(doc)


@frappe.whitelist()
def make_cheque_batch(
	source_doctype: str,
	source_name: str,
	company: str | None = None,
	party_type: str | None = None,
	party: str | None = None,
) -> dict:
	"""A new Cheque Batch (not inserted) of installment cheques for a source (see `make_pdc_cheque`).

	The installments come from the settings (count, frequency) and split the source's outstanding; the batch
	remembers its source ("Created From") and Generate Cheques allocates the cheques to it. An outgoing batch
	drawn on a cheque book gets its rows right away; otherwise the user types the first cheque number and
	clicks Generate Cheques."""
	source, info, party_currency = _prepare(BATCH, source_doctype, source_name, company, party_type, party)
	settings = posting.get_company_settings(info.company)
	count = min(cint(settings.get("default_installment_count")) or DEFAULT_INSTALLMENTS, batch.MAX_ROWS)
	doc = frappe.new_doc(BATCH)
	doc.naming_series = BATCH_NAMING_SERIES[info.direction]
	_set_header(doc, source, info, party_currency)
	doc.purpose = "Installments"
	if info.direction == sd.OUTGOING:
		doc.cheque_book = sd.default_cheque_book(info.company, doc.bank_account, count)
	doc.gen_count = count
	doc.gen_frequency = settings.get("default_installment_frequency") or DEFAULT_FREQUENCY
	doc.gen_first_due_date = sd.default_due_date(source)
	doc.gen_amount_mode = batch.SPLIT_TOTAL
	if info.kind == sd.PARTY:
		doc.allocation_method = batch.ADVANCE
	else:
		doc.gen_total = _amounts(doc, source, info, party_currency)[0]
		doc.source_doctype, doc.source_name = source.doctype, source.name
		doc.allocation_method = batch.MANUAL
		if doc.cheque_book and flt(doc.gen_total) > 0:
			# the batch numbers its own cheques: the rows (allocated to the source) come ready
			batch.generate_rows(doc)
	return sd.mark_new(doc)


def _prepare(target: str, source_doctype, source_name, company, party_type, party):
	"""(source, party info, party account currency) after the permission and payability checks."""
	source = sd.get_source(source_doctype, source_name, _(target))
	info = sd.resolve(source, party_type, party)
	if info.kind == sd.PARTY:
		info.company = company or frappe.defaults.get_user_default("Company")
	sd.check_create_permission(target, info.company)
	sd.check_payable(source, info)
	# the settings exist (else a clear "missing settings" message before anything else)
	posting.get_company_settings(info.company)
	party_account = posting.get_party_account(info.party_type, info.party, info.company)
	if not party_account:
		frappe.throw(_("No party account found for {0} {1}.").format(_(info.party_type), frappe.bold(info.party)))
	return source, info, get_account_currency(party_account)


def _set_header(doc, source, info, party_currency: str):
	"""Company, direction, party, bank (currency, rate, mode of payment), issue date, party details and the
	accounting details of the source."""
	doc.company = info.company
	doc.direction = info.direction
	doc.party_type = info.party_type
	doc.party = info.party
	doc.party_name = get_party_name(info.party_type, info.party)
	doc.issue_date = today()
	doc.bank_account = sd.default_cheque_bank(info.company, info.direction, party_currency)
	doc.currency = sd.bank_currency(doc.bank_account) or erpnext.get_company_currency(info.company)
	doc.exchange_rate = posting.get_cheque_exchange_rate(doc.currency, info.company, today())
	if doc.bank_account:
		method = payment_modes.CHEQUE_METHOD[info.direction]
		doc.mode_of_payment = payment_modes.resolve(info.company, doc.bank_account, method).mode_of_payment
	if can_read_party(info.party_type, info.party):
		doc.party_bank_account = get_party_bank_account(info.party_type, info.party)
	doc.contact_person = source.get("contact_person") or (
		get_default_contact(info.party_type, info.party) if info.party_type != "Employee" else None
	)
	sd.copy_accounting(source, doc)


def _amounts(doc, source, info, party_currency: str) -> tuple[float | None, float]:
	"""(amount in the document currency, outstanding in the party account currency) left of the source; also
	sets the remarks. No amount without a bank when the party is not in the company currency: the user picks a
	bank in the party's currency, the allocation is known already."""
	precision = frappe.get_precision(CHEQUE, "amount") or 2
	outstanding = sd.available_outstanding(source, info, party_currency, precision)
	label = _("Payment of {0} {1}") if info.direction == sd.OUTGOING else _("Receipt of {0} {1}")
	doc.remarks = label.format(_(source.doctype), source.name)
	if not doc.bank_account and party_currency != erpnext.get_company_currency(info.company):
		return None, outstanding
	amount = sd.to_document_currency(
		outstanding,
		party_currency,
		doc.currency if doc.bank_account else None,
		info.company,
		doc.exchange_rate,
		doc.bank_account,
		source,
		precision,
	)
	return amount, outstanding
