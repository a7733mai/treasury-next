"""Auto-debited installments (Facility Drawdown repayment mode "Auto Debit", spec 7g §3.6).

The bank debits each installment from the drawdown's bank account on its due date. Treasury Company Settings
`auto_debit_posting` says when the repayment is recorded:
- "On Due Date": the daily job (`post_due_auto_debits`) submits a Facility Repayment (settlement "Auto Debit")
  for every unpaid installment due up to today, dated its due date (a missed day is caught up on the right
  dates); the PDC Bank Clearing tool then only reconciles the statement line with its voucher.
- "On Bank Statement Match": nothing is posted by the job; the PDC Bank Clearing tool proposes the open
  installment for the statement debit of the same amount and records the repayment on the statement date
  (`repay_installment` with the Bank Transaction), cleared and reconciled.

The debit has already happened at the bank: the low balance guard never blocks these repayments (it evaluates
the company's alerts afterwards). A debit of another amount (extra charges) is recorded by hand.
"""

import frappe
from frappe import _
from frappe.utils import cint, flt, fmt_money, getdate, today

from treasury.accounting import posting
from treasury.bank_facilities import accounting, repayment
from treasury.utils.context import SYSTEM_USER, elevated

DRAWDOWN = "Facility Drawdown"
SCHEDULE = "Facility Repayment Schedule"
REPAYMENT = "Facility Repayment"
AUTO_DEBIT = "Auto Debit"
ON_DUE_DATE = "On Due Date"
ON_STATEMENT = "On Bank Statement Match"
SAVEPOINT = "treasury_auto_debit"


def posting_mode(company: str) -> str:
	return frappe.db.get_value("Treasury Company Settings", company, "auto_debit_posting") or ON_DUE_DATE


def post_due_auto_debits(as_of=None) -> dict:
	"""Daily job: record the auto-debited installments due up to `as_of` (default today) of the companies
	posting them on the due date. Each drawdown runs in its own savepoint and is committed on its own; a
	failing one is rolled back and logged (once per drawdown and day). Returns {"posted": [repayments],
	"failed": [drawdowns]}."""
	as_of = getdate(as_of or today())
	result = {"posted": [], "failed": []}
	companies = [
		row.company
		for row in frappe.get_all("Treasury Company Settings", fields=["company", "auto_debit_posting"])
		if (row.auto_debit_posting or ON_DUE_DATE) == ON_DUE_DATE
	]
	if not companies:
		return result
	posted_companies = set()
	for drawdown in frappe.get_all(
		DRAWDOWN,
		filters={
			"company": ["in", companies],
			"docstatus": 1,
			"repayment_mode": AUTO_DEBIT,
			"repayment_status": "Open",
		},
		fields=["name", "company"],
		order_by="drawdown_date asc, name asc",
	):
		frappe.db.savepoint(SAVEPOINT)
		try:
			with elevated(actor=SYSTEM_USER):
				names = _post_drawdown(drawdown.name, as_of)
		except Exception:
			frappe.db.rollback(save_point=SAVEPOINT)
			frappe.clear_messages()
			_log_failure(drawdown.name)
			result["failed"].append(drawdown.name)
		else:
			frappe.db.release_savepoint(SAVEPOINT)
			result["posted"] += names
			if names:
				posted_companies.add(drawdown.company)
		_commit()
	if posted_companies:
		from treasury.balance_alerts import scheduler as alerts

		for company in sorted(posted_companies):
			alerts.enqueue_evaluation(company)
	return result


def _post_drawdown(name: str, as_of) -> list[str]:
	"""The unpaid installments of one drawdown due up to `as_of`, oldest first, each on its due date."""
	posted = []
	for row in _due_rows(name, as_of):
		if not _principal_disbursed(name, row):
			# Istisna'a: the installment's principal is not disbursed yet (a stage is pending); it is posted by
			# the first run after the disbursement instead of failing every day
			break
		before = frappe.db.get_value(SCHEDULE, row.name, "repayment")
		repayment_name = repay_installment(name, cint(row.installment_no), row.due_date, system=True)
		if repayment_name and repayment_name != before:
			posted.append(repayment_name)
	return posted


def _principal_disbursed(drawdown: str, row) -> bool:
	"""False for an Istisna'a installment whose unpaid principal exceeds the disbursed principal outstanding
	(repayment.validate_disbursed_principal would refuse it)."""
	values = frappe.db.get_value(DRAWDOWN, drawdown, ["limit_type", "liability_account"], as_dict=True)
	if values.limit_type != repayment.ISTISNA or not repayment.unpaid_principal(row):
		return True
	from treasury.bank_facilities.utilisation import get_drawdown_outstanding

	source = {"name": drawdown, "liability_account": values.liability_account}
	outstanding = flt(get_drawdown_outstanding([source]).get(drawdown), 2)
	return flt(repayment.unpaid_principal(row), 2) <= outstanding


def _due_rows(drawdown: str, as_of) -> list[frappe._dict]:
	rows = frappe.get_all(
		SCHEDULE,
		filters={
			"parenttype": DRAWDOWN,
			"parent": drawdown,
			"is_prepayment": 0,
			"due_date": ["<=", getdate(as_of)],
			"status": ["!=", repayment.PAID],
		},
		fields=[
			"name",
			"installment_no",
			"due_date",
			"principal",
			"interest",
			"paid_principal",
			"paid_interest",
		],
		order_by="due_date asc, installment_no asc",
	)
	return [r for r in rows if repayment.unpaid_principal(r) or repayment.unpaid_interest(r)]


def repay_installment(
	drawdown: str,
	installment_no: int,
	date,
	*,
	bank_transaction=None,
	system=False,
	expected_amount: float | None = None,
) -> str:
	"""The Facility Repayment (settlement Auto Debit) of one installment's unpaid principal and interest, dated
	`date`. Idempotent: an installment already paid returns its repayment (the daily job). `system`: the daily
	job (no user permission check); otherwise the user's own create / submit permission (the PDC Bank Clearing
	tool). With a `bank_transaction` (the statement debit), an installment paid meanwhile is refused (the
	statement line would stay unmatched) and the unpaid total must be `expected_amount` (the line's open amount),
	both checked under the drawdown lock."""
	repayment.lock_drawdown(drawdown)
	doc = frappe.get_doc(DRAWDOWN, drawdown)
	row = next(
		(
			r
			for r in doc.repayment_schedule
			if not r.is_prepayment and cint(r.installment_no) == cint(installment_no)
		),
		None,
	)
	if not row:
		frappe.throw(
			_("Installment {0} does not exist on drawdown {1}.").format(installment_no, frappe.bold(drawdown))
		)
	unpaid = flt(repayment.unpaid_principal(row) + repayment.unpaid_interest(row), doc.precision("amount"))
	if unpaid <= 0:
		if bank_transaction:
			frappe.throw(
				_("Installment {0} of drawdown {1} is already paid by {2}.").format(
					row.installment_no, frappe.bold(drawdown), frappe.bold(row.repayment or "-")
				)
			)
		return row.repayment
	if expected_amount is not None and unpaid != flt(expected_amount, doc.precision("amount")):
		frappe.throw(
			_("The statement amount {0} does not match the unpaid installment {1} ({2}).").format(
				fmt_money(expected_amount, currency=doc.currency),
				row.installment_no,
				fmt_money(unpaid, currency=doc.currency),
			)
		)
	values = {
		"doctype": REPAYMENT,
		"drawdown": drawdown,
		"repayment_date": getdate(date),
		"bank_account": doc.bank_account,
		"settlement": accounting.SETTLED_BY_AUTO_DEBIT,
		"bank_transaction": bank_transaction,
		"allocation_mode": repayment.SELECTED_ROW,
		"installment_no": row.installment_no,
		"principal_paid": repayment.unpaid_principal(row),
		"interest_paid": repayment.unpaid_interest(row),
		"penalty": 0,
		"remarks": _("Auto debit of installment {0}").format(row.installment_no),
	}
	new = frappe.get_doc(values)
	new.flags.treasury_facility_service = True
	new.flags.treasury_system_repayment = bool(system or bank_transaction)
	if system:
		new.flags.ignore_permissions = True
	new.insert()
	new.submit()
	if bank_transaction:
		from treasury.utils.bank_reconciliation import reconcile_voucher

		new.reload()
		if new.payment_entry:
			voucher = ("Payment Entry", new.payment_entry)
		else:
			voucher = ("Journal Entry", new.journal_entry)
		reconcile_voucher(bank_transaction, *voucher)
		posting.set_clearance_date(
			*voucher, frappe.db.get_value("Bank Transaction", bank_transaction, "date")
		)
	return new.name


def get_open_installments(
	company: str, bank_account: str, from_date=None, to_date=None
) -> list[frappe._dict]:
	"""The auto-debit installments the PDC Bank Clearing tool may match with a statement debit of the bank
	account: (a) the Auto Debit repayments posted and not reconciled yet ("reconcile"), (b) with "On Bank
	Statement Match", the unpaid installments of the Auto Debit drawdowns ("post"). Only for a user who may read
	Facility Drawdowns (the drawdowns are read with the user's permissions)."""
	if not frappe.has_permission(DRAWDOWN, "read"):
		return []
	drawdowns = {
		d.name: d
		for d in frappe.get_list(
			DRAWDOWN,
			filters={
				"company": company,
				"bank_account": bank_account,
				"docstatus": 1,
				"repayment_mode": AUTO_DEBIT,
			},
			fields=["name", "facility", "currency", "lender_name"],
			limit_page_length=0,
		)
	}
	if not drawdowns:
		return []
	contracts = {
		f.name: f.contract_no
		for f in frappe.get_all(
			"Bank Facility",
			filters={"name": ["in", sorted({d.facility for d in drawdowns.values()})]},
			fields=["name", "contract_no"],
		)
	}
	result = []
	posted = frappe.get_all(
		REPAYMENT,
		filters={
			"drawdown": ["in", list(drawdowns)],
			"docstatus": 1,
			"settlement": accounting.SETTLED_BY_AUTO_DEBIT,
			"bank_transaction": ["is", "not set"],
			"bank_account": bank_account,
		},
		fields=["name", "drawdown", "repayment_date", "total_paid", "payment_entry", "journal_entry"],
	)
	reconciled = _reconciled_vouchers([r.payment_entry or r.journal_entry for r in posted])
	installments = {
		a.parent: a.installment_no
		for a in frappe.get_all(
			"Facility Repayment Allocation",
			filters={"parenttype": REPAYMENT, "parent": ["in", [r.name for r in posted] or [""]]},
			fields=["parent", "installment_no"],
		)
	}
	for rep in posted:
		voucher = rep.payment_entry or rep.journal_entry
		if not voucher or voucher in reconciled:
			continue
		drawdown = drawdowns[rep.drawdown]
		result.append(
			frappe._dict(
				kind="reconcile",
				drawdown=rep.drawdown,
				repayment=rep.name,
				installment_no=cint(installments.get(rep.name)),
				date=getdate(rep.repayment_date),
				amount=flt(rep.total_paid),
				currency=drawdown.currency,
				lender_name=drawdown.lender_name,
				contract_no=contracts.get(drawdown.facility),
				voucher_type="Payment Entry" if rep.payment_entry else "Journal Entry",
				voucher_no=voucher,
			)
		)
	if posting_mode(company) == ON_STATEMENT:
		rows = frappe.get_all(
			SCHEDULE,
			filters={
				"parenttype": DRAWDOWN,
				"parent": ["in", list(drawdowns)],
				"is_prepayment": 0,
				"status": ["!=", repayment.PAID],
			},
			fields=[
				"parent",
				"installment_no",
				"due_date",
				"principal",
				"interest",
				"paid_principal",
				"paid_interest",
			],
			order_by="due_date asc",
		)
		for row in rows:
			unpaid = flt(repayment.unpaid_principal(row) + repayment.unpaid_interest(row), 2)
			# an installment overdue before From Date may still be debited: only a later one is left out
			if unpaid <= 0 or (to_date and getdate(row.due_date) > getdate(to_date)):
				continue
			drawdown = drawdowns[row.parent]
			result.append(
				frappe._dict(
					kind="post",
					drawdown=row.parent,
					repayment=None,
					installment_no=cint(row.installment_no),
					date=getdate(row.due_date),
					amount=unpaid,
					currency=drawdown.currency,
					lender_name=drawdown.lender_name,
					contract_no=contracts.get(drawdown.facility),
				)
			)
	return result


def _reconciled_vouchers(vouchers: list[str]) -> set[str]:
	"""The `vouchers` already matched with a Bank Transaction (a Bank Transaction Payments row)."""
	vouchers = [v for v in vouchers if v]
	if not vouchers:
		return set()
	return set(
		frappe.get_all(
			"Bank Transaction Payments",
			filters={
				"parenttype": "Bank Transaction",
				"payment_document": ["in", ["Payment Entry", "Journal Entry"]],
				"payment_entry": ["in", vouchers],
			},
			pluck="payment_entry",
		)
	)


def _commit():
	"""Keep each drawdown's result even if the job dies later (tests run in one rolled back transaction)."""
	if not frappe.flags.in_test:
		frappe.db.commit()  # nosemgrep: allowed in scheduler jobs


def _log_failure(name: str):
	"""Error Log of a failing drawdown, at most once per day (it may fail every day, e.g. closed period)."""
	title = _("Facility auto debit failed for drawdown {0}").format(name)
	if frappe.db.exists(
		"Error Log",
		{"reference_doctype": DRAWDOWN, "reference_name": name, "method": title, "creation": [">=", today()]},
	):
		return
	frappe.log_error(title=title, reference_doctype=DRAWDOWN, reference_name=name)
