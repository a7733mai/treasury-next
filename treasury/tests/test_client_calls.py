"""Form scripts must call controller actions on the document.

Frappe's `frm.call({method: "x"})` without `doc` resolves `x` as a *module-level* function
(`<doctype module>.x`), not as the whitelisted method of the open document, so every action button
failed in the browser with "Failed to get method ... has no attribute 'x'" while the Python tests
(which call the controller directly) passed. This test scans every form script of the app.
"""

import json
import re
from pathlib import Path

import frappe
from frappe.model.base_document import get_controller
from frappe.tests import UnitTestCase

APP_PATH = Path(frappe.get_app_path("treasury"))
CALL_START = re.compile(r"frm\.call\(\s*\{")
LITERAL_METHOD = re.compile(r"""method:\s*["']([a-z_]+)["']""")
ACTION_HELPER = re.compile(r"""_action\(\s*frm\s*,\s*["']([a-z_]+)["']""")
# dotted server paths called by the plain scripts of public/js
DOTTED_METHOD = re.compile(r"""method:\s*["'](treasury(?:\.\w+)+)["']""")
XCALL_METHOD = re.compile(r"""frappe\.xcall\(\s*["'](treasury(?:\.\w+)+)["']""")


def form_scripts():
	for path in APP_PATH.rglob("doctype/*/*.js"):
		if path.stem == path.parent.name:  # <doctype>.js, not list/tree scripts
			yield path


def call_blocks(source: str):
	"""Yield the text of each `frm.call({ ... })` object literal (balanced braces)."""
	for match in CALL_START.finditer(source):
		depth, start = 0, match.end() - 1
		for index in range(start, len(source)):
			if source[index] == "{":
				depth += 1
			elif source[index] == "}":
				depth -= 1
				if depth == 0:
					yield source[start : index + 1]
					break


def _doctype_name(path: Path) -> str:
	with open(path.with_suffix(".json"), encoding="utf-8") as f:
		return json.load(f)["name"]


class TestClientCalls(UnitTestCase):
	def test_frm_call_passes_the_document(self):
		offenders = []
		for path in form_scripts():
			for block in call_blocks(path.read_text(encoding="utf-8")):
				if "doc: frm.doc" not in block:
					offenders.append(f"{path.relative_to(APP_PATH)}: {block[:80]}")
		self.assertEqual(offenders, [])

	def test_called_actions_exist_and_are_whitelisted(self):
		missing = []
		checked = 0
		for path in form_scripts():
			source = path.read_text(encoding="utf-8")
			names = set(ACTION_HELPER.findall(source))
			for block in call_blocks(source):
				names.update(LITERAL_METHOD.findall(block))
			if not names:
				continue
			controller = get_controller(_doctype_name(path))
			for name in sorted(names):
				checked += 1
				method = getattr(controller, name, None)
				if not method or method not in frappe.whitelisted:
					missing.append(f"{controller.__name__}.{name}")
		self.assertEqual(missing, [])
		self.assertGreater(checked, 20)  # the scan really sees the action buttons

	def test_public_scripts_call_whitelisted_paths(self):
		"""Phase 7c: the plain scripts of public/js (loaded by `doctype_js`) call existing whitelisted methods."""
		missing, checked = [], 0
		for path in sorted((APP_PATH / "public" / "js").glob("*.js")):
			source = path.read_text(encoding="utf-8")
			for pattern in (DOTTED_METHOD, XCALL_METHOD):
				for dotted in pattern.findall(source):
					checked += 1
					try:
						method = frappe.get_attr(dotted)
					except Exception:
						method = None
					if not method or method not in frappe.whitelisted:
						missing.append(f"{path.name}: {dotted}")
		self.assertEqual(missing, [])
		self.assertGreaterEqual(checked, 5)

	def test_doctype_js_files_exist(self):
		"""Every `doctype_js` hook path (a string or a list of them) is a file of the app."""
		missing, checked = [], 0
		for doctype, paths in frappe.get_hooks("doctype_js", app_name="treasury").items():
			for path in paths if isinstance(paths, list | tuple) else [paths]:
				checked += 1
				if not (APP_PATH / path).is_file():
					missing.append(f"{doctype}: {path}")
		self.assertEqual(missing, [])
		self.assertGreater(checked, 10)

	def test_state_changing_methods_reject_get(self):
		"""The Phase 7a actions change data: POST only (Frappe checks the CSRF token only on POST)."""
		from types import SimpleNamespace

		from frappe.handler import is_valid_http_method

		from treasury.treasury_setup.doctype.treasury_company_settings.treasury_company_settings import (
			TreasuryCompanySettings,
		)
		from treasury.utils.party import create_payee

		methods = (
			TreasuryCompanySettings.generate_modes_of_payment,
			TreasuryCompanySettings.add_series_to_payment_entry,
			create_payee,
		)
		from treasury.bank_transfers.doctype.bank_transfer.bank_transfer import BankTransfer

		methods += (
			BankTransfer.send_now,
			BankTransfer.reschedule,
			BankTransfer.mark_completed,
			BankTransfer.mark_returned,
		)
		# Phase 7d
		from treasury.bank_transfers.doctype.bank_charge.bank_charge import BankCharge
		from treasury.pdc.doctype.pdc_bank_clearing.pdc_bank_clearing import PDCBankClearing

		methods += (BankCharge.reconcile_bank_transaction, PDCBankClearing.clear_selected)
		previous = getattr(frappe.local, "request", None)
		# Phase 7e: the actions guarded by the low balance check stay POST only
		methods += self.low_balance_actions()
		try:
			for method in methods:
				with self.subTest(method=method.__qualname__):
					self.assertIn(method, frappe.whitelisted)
					self.assertEqual(frappe.allowed_http_methods_for_whitelisted_func[method], ["POST"])
					frappe.local.request = SimpleNamespace(method="GET")
					self.assertRaises(frappe.PermissionError, is_valid_http_method, method)
					frappe.local.request = SimpleNamespace(method="POST")
					is_valid_http_method(method)
		finally:
			frappe.local.request = previous

	@staticmethod
	def low_balance_actions() -> tuple:
		from treasury.bank_transfers.doctype.bank_transfer.bank_transfer import BankTransfer
		from treasury.pdc.doctype.cheque_batch.cheque_batch import ChequeBatch
		from treasury.pdc.doctype.pdc_cheque.pdc_cheque import PDCCheque
		from treasury.security_cheques.doctype.security_cheque.security_cheque import SecurityCheque

		return (
			PDCCheque.deliver,
			ChequeBatch.deliver_all,
			BankTransfer.send_now,
			BankTransfer.reschedule,
			SecurityCheque.encash,
		)

	def test_low_balance_actions_accept_the_override(self):
		"""Phase 7e: the guarded actions take `allow_low_balance` (the override the form confirms)."""
		import inspect

		for method in self.low_balance_actions():
			with self.subTest(method=method.__qualname__):
				self.assertIn("allow_low_balance", inspect.signature(method).parameters)
				self.assertIn(method, frappe.whitelisted)

	def test_low_balance_card_accepts_get(self):
		"""Phase 7e: the number card method only reads (GET is fine)."""
		from types import SimpleNamespace

		from frappe.handler import is_valid_http_method

		from treasury.balance_alerts.dashboard import get_low_balance_card

		previous = getattr(frappe.local, "request", None)
		try:
			self.assertIn(get_low_balance_card, frappe.whitelisted)
			frappe.local.request = SimpleNamespace(method="GET")
			is_valid_http_method(get_low_balance_card)
		finally:
			frappe.local.request = previous

	def test_read_only_helpers_accept_get(self):
		"""Phase 7b: the Bank Transfer form helpers only read (a GET is fine)."""
		from types import SimpleNamespace

		from frappe.handler import is_valid_http_method

		from treasury.bank_transfers.api import get_fee_preview, get_transfer_settings, make_bank_transfer
		from treasury.bank_transfers.doctype.bank_charge.bank_charge import get_category_defaults, make_refund
		from treasury.pdc.api import make_cheque_batch, make_pdc_cheque
		from treasury.security_cheques.api import make_security_cheque
		from treasury.utils.source_documents import get_journal_parties

		previous = getattr(frappe.local, "request", None)
		try:
			for method in (
				make_bank_transfer,
				get_fee_preview,
				get_transfer_settings,
				# Phase 7c mappers: they return an unsaved document, like frappe.model.mapper.make_mapped_doc
				make_pdc_cheque,
				make_cheque_batch,
				make_security_cheque,
				get_journal_parties,
				# Phase 7d: an unsaved refund, the defaults of a category
				make_refund,
				get_category_defaults,
			):
				with self.subTest(method=method.__qualname__):
					self.assertIn(method, frappe.whitelisted)
					frappe.local.request = SimpleNamespace(method="GET")
					is_valid_http_method(method)
		finally:
			frappe.local.request = previous
