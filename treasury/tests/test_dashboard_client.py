"""Phase 7h review fixes in the plain client scripts (no node at runtime, so they are checked as text):

- the Treasury Dashboard hands frappe-charts only escaped texts (labels, dataset names, markers, tooltips are
  written as innerHTML by the chart library);
- the single-bank drill-down note follows the effective banks (GL siblings included);
- the Setup Checklist action is hidden with its mobile menu entry;
- the Bounced and Postponed Cheques report refreshes on every company change;
- the settings Setup Checklist "Fix" scrolls to its field on another form, and "Document Naming Settings"
  selects the doctype without leaving a stale `frappe.route_options`.
"""

import re
from pathlib import Path

import frappe
from frappe.tests import UnitTestCase

APP_PATH = Path(frappe.get_app_path("treasury"))
DASHBOARD_JS = APP_PATH / "pdc" / "page" / "treasury_dashboard" / "treasury_dashboard.js"
BOUNCED_JS = (
	APP_PATH / "pdc" / "report" / "bounced_and_postponed_cheques" / "bounced_and_postponed_cheques.js"
)
SETTINGS_JS = (
	APP_PATH
	/ "treasury_setup"
	/ "doctype"
	/ "treasury_company_settings"
	/ "treasury_company_settings.js"
)


def method_body(source: str, signature: str) -> str:
	"""The text of the method / function starting with `signature` up to its closing brace (balanced)."""
	start = source.index(signature)
	brace = source.index("{", start)
	depth = 0
	for index in range(brace, len(source)):
		if source[index] == "{":
			depth += 1
		elif source[index] == "}":
			depth -= 1
			if depth == 0:
				return source[brace : index + 1]
	raise AssertionError(f"unbalanced body of {signature}")


class TestDashboardClient(UnitTestCase):
	def setUp(self):
		self.dashboard = DASHBOARD_JS.read_text(encoding="utf-8")

	def test_charts_get_escaped_texts_only(self):
		self.assertEqual(self.dashboard.count("new frappe.Chart("), 1)
		mount = method_body(self.dashboard, "mount_charts() {")
		self.assertIn("data: this.safe_chart_data(options.data)", mount)
		self.assertIn("tooltipOptions: this.safe_tooltip(options)", mount)
		self.assertNotIn("data: options.data", mount)

		safe = method_body(self.dashboard, "safe_chart_data(data) {")
		for escaped in (
			"labels: (data.labels || []).map((label) => this.esc(label))",
			"name: this.esc(d.name)",
			"label: this.esc(m.label)",
		):
			self.assertIn(escaped, safe)
		tooltip = method_body(self.dashboard, "safe_tooltip(options) {")
		self.assertIn("formatTooltipX: (label) => this.esc(", tooltip)
		self.assertIn("formatTooltipY: (value) => this.esc(", tooltip)

	def test_multi_bank_note_follows_the_effective_banks(self):
		note = method_body(self.dashboard, "multi_bank_note(route) {")
		self.assertIn("this.data.bank_accounts", note)
		self.assertNotIn("this.filters()", note)

	def test_setup_button_hidden_with_its_mobile_entry(self):
		button = method_body(self.dashboard, "set_setup_button(setup) {")
		self.assertIn("setup && setup.route", button)
		self.assertIn("this.setup_menu_item().toggleClass(\"hidden\", !show)", button)
		self.assertIn("this.page.menu", method_body(self.dashboard, "setup_menu_item() {"))

	def test_bounced_report_refreshes_on_company_change(self):
		source = BOUNCED_JS.read_text(encoding="utf-8")
		company = source[source.index('fieldname: "company"') : source.index('fieldname: "bank_account"')]
		# an on_change replaces the report's own refresh: clear the bank if set, else refresh
		self.assertRegex(
			company,
			re.compile(
				r'if \(frappe\.query_report\.get_filter_value\("bank_account"\)\) \{\s*'
				r'frappe\.query_report\.set_filter_value\("bank_account", ""\);\s*'
				r"\} else \{\s*frappe\.query_report\.refresh\(\);\s*\}"
			),
		)

	def test_settings_fix_and_naming_links(self):
		source = SETTINGS_JS.read_text(encoding="utf-8")
		fix = method_body(source, "function tcs_fix(frm, item) {")
		self.assertIn("scroll_to: action.field", fix)
		self.assertNotIn("transaction_type: row.doctype", source)
		opener = method_body(source, "function tcs_open_naming_settings(doctype) {")
		self.assertIn('frappe.ui.form.on("Document Naming Settings", "refresh"', opener)
		self.assertIn("treasury.tcs_naming_doctype = null", opener)
