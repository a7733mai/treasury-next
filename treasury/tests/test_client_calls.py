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
		previous = getattr(frappe.local, "request", None)
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

	def test_read_only_helpers_accept_get(self):
		"""Phase 7b: the Bank Transfer form helpers only read (a GET is fine)."""
		from types import SimpleNamespace

		from frappe.handler import is_valid_http_method

		from treasury.bank_transfers.api import get_fee_preview, get_transfer_settings, make_bank_transfer

		previous = getattr(frappe.local, "request", None)
		try:
			for method in (make_bank_transfer, get_fee_preview, get_transfer_settings):
				with self.subTest(method=method.__qualname__):
					self.assertIn(method, frappe.whitelisted)
					frappe.local.request = SimpleNamespace(method="GET")
					is_valid_http_method(method)
		finally:
			frappe.local.request = previous
