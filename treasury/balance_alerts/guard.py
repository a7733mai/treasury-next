"""Entry-time low balance guard (spec 7e §3.5).

A guarded action (cheque delivery, transfer submit / Send Now / Reschedule, facility repayment, bank charge)
calls `begin` before it changes anything and `finish` after it wrote its changes, in the same transaction:

- `begin` locks the involved Bank Accounts in name order (the 7b row lock, taken after the document's own row)
  and evaluates their projected positions (pre-state);
- `finish` evaluates them again (post-state). A breach is a day whose projected closing is below the minimum
  balance *and* lower than before the action: an action that does not lower a day never blocks. Then, per
  the company's On Low Projected Balance setting: Warn shows a message; Block raises LowBalanceError, unless a
  Treasury Manager confirmed the override (`allow_low_balance`), which is recorded in a Treasury Alert Log of
  type Override and returned for the document's own log. Ignore checks nothing.

After every guarded action on a company with thresholds, whatever the mode, the company is evaluated again
after the commit (alerts only, treasury.balance_alerts.scheduler.evaluate_company). Evaluations run elevated:
the user must not be blocked by, nor miss, flows they cannot read.

Batch operations use `batch_guard`: one check around the whole operation (the per-cheque guards are skipped).
"""

from contextlib import contextmanager

import frappe
from frappe import _
from frappe.utils import cint, fmt_money, formatdate, getdate, strip_html, today

from treasury.balance_alerts.positions import epsilon, evaluate, get_thresholds, horizon, with_gl_siblings
from treasury.bank_transfers import fees
from treasury.pdc.report_utils import get_precision, report_today
from treasury.utils.context import elevated, real_user

OVERRIDE_ROLES = ("Treasury Manager", "System Manager")
SETTINGS = "Treasury Company Settings"
IGNORE, WARN, BLOCK = "Ignore", "Warn", "Block"
# Set while a batch-level guard is active: the guards of the single actions inside it are skipped.
FLAG = "treasury_balance_guard"
SAVEPOINT = "treasury_balance_guard"
LINES_PER_BANK = 3


class LowBalanceError(frappe.ValidationError):
	pass


def has_override_role(user: str | None = None) -> bool:
	return bool(set(OVERRIDE_ROLES) & set(frappe.get_roles(user or real_user())))


def check_override_flag(allow_override):
	"""Only a Treasury Manager (or System Manager) may ask for the override."""
	if cint(allow_override) and not has_override_role():
		frappe.throw(
			_("Only a Treasury Manager can override the low balance block."),
			frappe.PermissionError,
			title=_("Not Permitted"),
		)


def reset_override_flag(doc):
	"""`validate` of a draft with the `allow_low_balance` field: only a Treasury Manager (or System Manager)
	may store it. The field is permlevel 1 (written by those roles only); this also covers saves that skip the
	permission checks, so a flag stored by someone else never overrides a Block when a manager submits."""
	if doc.docstatus == 0 and cint(doc.get("allow_low_balance")) and not has_override_role():
		doc.allow_low_balance = 0


def _scheduler():
	# imported late: the scheduler reaches the PDC modules, which import this guard
	from treasury.balance_alerts import scheduler

	return scheduler


def _notify():
	from treasury.balance_alerts import notify

	return notify


def begin(company: str, bank_accounts: list[str], *, system: bool = False) -> frappe._dict | None:
	"""Lock the banks and take the pre-state; None when nothing is to be checked (see the module)."""
	if system or frappe.flags.get(FLAG):
		return None
	banks = sorted({b for b in bank_accounts or () if b})
	if not company or not banks:
		return None
	mode = frappe.db.get_value(SETTINGS, company, "on_low_balance")
	thresholds = get_thresholds(company) if mode else {}
	if not thresholds:
		return None
	if mode == IGNORE:
		_scheduler().enqueue_evaluation(company)
		return None
	banks = with_gl_siblings(company, banks)
	if not any(b in thresholds for b in banks):
		_scheduler().enqueue_evaluation(company)
		return None
	for bank in banks:  # name order, after the document's own row
		fees.lock_bank_account(bank)
	day = getdate(report_today())
	with elevated():
		pre = evaluate(company, banks, today=day)
	return frappe._dict(company=company, banks=banks, mode=mode, pre=pre, today=day)


def breaches(pre: list, post: list) -> list[frappe._dict]:
	"""Days below the minimum that the action lowered: [{position, day, pre_closing}] in bank, date order."""
	eps = epsilon(get_precision())
	before = {p.account: {d.date: d.closing for d in p.days} for p in pre}
	found = []
	for position in post:
		if position.minimum is None:
			continue
		previous = before.get(position.account, {})
		for day in position.days:
			pre_closing = previous.get(day.date)
			if day.closing < position.minimum and (pre_closing is None or day.closing < pre_closing - eps):
				found.append(frappe._dict(position=position, day=day, pre_closing=pre_closing))
	return found


def _grouped(found: list) -> list[tuple[list, int]]:
	"""The breaches per bank, at most LINES_PER_BANK each: [(shown, number not shown)] in bank order."""
	groups: dict[str, list] = {}
	for item in found:
		groups.setdefault(item.position.bank_account, []).append(item)
	return [(items[:LINES_PER_BANK], max(len(items) - LINES_PER_BANK, 0)) for items in groups.values()]


def _line_values(item, wrap) -> tuple:
	position, day, currency = item.position, item.day, item.position.currency
	pre = fmt_money(item.pre_closing, currency=currency) if item.pre_closing is not None else "-"
	return (
		wrap(position.bank_account, bold=True),
		formatdate(day.date),
		wrap(fmt_money(day.closing, currency=currency), bold=True),
		wrap(pre),
		wrap(fmt_money(position.minimum, currency=currency)),
	)


def message_lines(doctype: str, name: str, found: list) -> list[str]:
	"""The plain text of the message (the override remarks): a head line, then up to LINES_PER_BANK lines per
	bank `{bank}: {date} projected {post} (before: {pre}), minimum {min}.` and `+{n} more day(s)`."""
	lines = [_("Low projected balance after {0} {1}:").format(_(doctype), name)]
	for shown, more in _grouped(found):
		for item in shown:
			lines.append(
				_("{0}: {1} projected {2} (before: {3}), minimum {4}.").format(
					*_line_values(item, lambda value, bold=False: value)
				)
			)
		if more:
			lines.append(_("+{0} more day(s)").format(more))
	return lines


FSI, PDI = chr(0x2068), chr(0x2069)  # first strong isolate / pop directional isolate


def isolate(text: str) -> str:
	"""Bidi-isolate a name or amount (the <bdi> of plain text)."""
	return f"{FSI}{text}{PDI}"


def message_html(doctype: str, name: str, found: list, snapshot) -> str:
	"""The message shown to the user (design 7e §4.1): escaped, every name and amount isolated for RTL, and a
	link to the forecast of the first breached bank. Frappe's message sanitizer drops <bdi>, so the isolation uses
	the Unicode FSI / PDI characters instead (same effect, survives `clean_html`)."""
	esc = frappe.utils.escape_html

	def wrap(value, bold=False):
		text = isolate(esc(value))
		return f"<b>{text}</b>" if bold else text

	# each argument is a closed tag of its own: a translation may reorder the placeholders
	head = _("Low projected balance after {0} {1}:").format(
		f"<b>{esc(_(doctype))}</b>", f"<b>{isolate(esc(name))}</b>"
	)
	items = []
	for shown, more in _grouped(found):
		for item in shown:
			items.append(
				"<li>{}</li>".format(
					_("{0}: {1} projected {2} (before: {3}), minimum {4}.").format(*_line_values(item, wrap))
				)
			)
		if more:
			items.append(f'<li class="text-muted">{esc(_("+{0} more day(s)").format(more))}</li>')
	bank = found[0].position.bank_account
	route = _notify().forecast_route(snapshot.company, snapshot.today, horizon(), bank)
	link = f'<p><a href="{esc(route)}">{esc(_("Open in Bank Position Forecast"))}</a></p>'
	return f"<p>{head}</p><ul>{''.join(items)}</ul>{link}"


def latest_override(doctype: str, name: str) -> frappe._dict | None:
	"""{user, creation} of the latest low balance override of a document (the form's audit line, design 7e
	P17); read without permission checks, only the user and the time are returned."""
	rows = frappe.get_all(
		"Treasury Alert Log",
		filters={"alert_type": "Override", "reference_doctype": doctype, "reference_name": name},
		fields=["user", "creation"],
		order_by="creation desc",
		limit=1,
	)
	return rows[0] if rows else None


def finish(
	snapshot, *, doctype: str, name: str, allow_override=False, block_as_warn: bool = False
) -> str | None:
	"""Check the post-state against the pre-state (see the module). Returns the override text (plain) when a
	Treasury Manager overrode a Block, else None."""
	if not snapshot:
		return None
	with elevated():
		post = evaluate(snapshot.company, snapshot.banks, today=snapshot.today)
	found = breaches(snapshot.pre, post)
	text = None
	if found:
		message = message_html(doctype, name, found, snapshot)
		title = _("Low Projected Balance")
		if snapshot.mode != BLOCK or block_as_warn:
			frappe.msgprint(message, title=title, indicator="orange")
		elif cint(allow_override) and has_override_role():
			# the remarks are prose in the acting user's language (design 7e §4.5)
			text = strip_html("\n".join(message_lines(doctype, name, found)))
			_record_override(snapshot, doctype, name, found[0], text)
			recorded = _("Low balance override recorded: {0}").format(f"{_(doctype)} {name}")
			frappe.msgprint(
				f"<p><b>{frappe.utils.escape_html(recorded)}</b></p>{message}",
				title=title,
				indicator="orange",
			)
		else:
			hint = (
				_("Confirm the override to continue.")
				if has_override_role()
				else _("A Treasury Manager can override this block.")
			)
			hint = f'<p class="text-muted">{frappe.utils.escape_html(hint)}</p>'
			frappe.throw(message + hint, LowBalanceError, title=title)
	_scheduler().enqueue_evaluation(snapshot.company)
	return text


def _record_override(snapshot, doctype: str, name: str, first, text: str):
	position, day = first.position, first.day
	_scheduler().insert_log(
		alert_type=_scheduler().OVERRIDE,
		level="Critical",
		company=snapshot.company,
		bank_account=position.bank_account,
		alert_date=getdate(today()),
		projected_date=day.date,
		balance=day.closing,
		threshold=position.minimum,
		currency=position.currency,
		user=real_user(),
		reference_doctype=doctype,
		reference_name=name,
		subject=_("Low balance override: {0} {1}").format(_(doctype), name),
		message=text,
	)


@contextmanager
def batch_guard(company: str, banks: list[str], allow_override, doctype: str, name: str):
	"""One guard around a batch operation; yields a holder whose `text` is the override text afterwards.
	A Block (or any error) rolls back everything the operation did."""
	snapshot = begin(company, banks)
	holder = frappe._dict(text=None)
	if snapshot:
		frappe.db.savepoint(SAVEPOINT)
	previous = frappe.flags.get(FLAG)
	frappe.flags[FLAG] = True
	try:
		yield holder
	except Exception:
		frappe.flags[FLAG] = previous
		if snapshot:
			frappe.db.rollback(save_point=SAVEPOINT)
		raise
	frappe.flags[FLAG] = previous
	try:
		holder.text = finish(snapshot, doctype=doctype, name=name, allow_override=allow_override)
	except Exception:
		frappe.db.rollback(save_point=SAVEPOINT)
		raise
	if snapshot:
		frappe.db.release_savepoint(SAVEPOINT)
