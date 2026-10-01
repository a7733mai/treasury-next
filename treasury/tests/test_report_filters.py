"""Report scripts must never let a report run without its required filters (Phase 7h UAT).

A Script Report runs as soon as it opens. When the script has no default for a filter the Python requires
(the company, a date), the first run fails with a server error ("Company is required.", HTTP 417) instead
of the desk's "Please set filters". Every report whose Python validates the company must declare a
`company` filter defaulting to the user's default Company and marked `reqd`, and every `reqd` filter needs a
default unless the user must choose it (listed below).

All desk scripts share one global scope: a second top-level `const`/`let`/`class` of the same name throws
a SyntaxError and the whole script is lost (a report then opens without any filter and runs without the
company). The top-level names of the app's scripts must be unique.
"""

import re
from pathlib import Path

import frappe
from frappe.tests import UnitTestCase

APP_PATH = Path(frappe.get_app_path("treasury"))
REPORT_SCRIPT = re.compile(r"frappe\.query_reports\[")
FILTERS_START = re.compile(r"\bfilters:\s*\[")
FIELDNAME = re.compile(r"""\bfieldname:\s*["'](\w+)["']""")
REQD = re.compile(r"\breqd:\s*(?:1|true)\b")
DEFAULT = re.compile(r"\bdefault:\s*\S")
COMPANY_DEFAULT = re.compile(r"""\bdefault:\s*frappe\.defaults\.get_user_default\(\s*["']Company["']\s*\)""")
COMPANY_REQUIRED = re.compile(r"validate_company\(\s*filters\.company")
TOP_LEVEL_NAME = re.compile(r"^(?:const|let|var|class|function|async function)\s+([A-Za-z_$][\w$]*)", re.M)
# frappe.datetime.<x>(true) returns a JS Date, not a moment
AS_DATE = re.compile(r"frappe\.datetime\.(?:now_datetime|now_time|system_datetime|now_date)\(\s*true\s*\)")
MOMENT_CALL = r"\.(?:hours|minutes|seconds|format|add|subtract|diff|isBefore|isAfter)\("
ASSIGNED_TO = re.compile(r"(?:const|let|var)\s+(\w+)\s*=\s*$")
# a required filter the user must choose: the report shows "Please set filters" until then
NO_DEFAULT_NEEDED = {("projected_party_statement", "party")}
# Phase 8 (spec 8 §4.3): the Cheque Timing filter of the cheque reports, its options and translated labels
# report -> the filter it follows (None: no position required)
TIMING_REPORTS = {
	"pdc_register": "accounting_status",
	"pdc_maturity": "bank_account",
	"cheque_leaf_register": None,
}
TIMING_FILTER_PARTS = (
	re.compile(r'\blabel:\s*__\(\s*"Cheque Timing"\s*\)'),
	re.compile(r'\bfieldtype:\s*"Select"'),
	re.compile(
		r"""\boptions:\s*\[\s*\{\s*value:\s*"",\s*label:\s*""\s*\},\s*"""
		r"""\{\s*value:\s*"Current-dated",\s*label:\s*__\(\s*"Current-dated"\s*\)\s*\},\s*"""
		r"""\{\s*value:\s*"Post-dated",\s*label:\s*__\(\s*"Post-dated"\s*\)\s*\},?\s*\]"""
	),
)


def report_dirs():
	for script in sorted(APP_PATH.rglob("report/*/*.js")):
		if script.stem == script.parent.name:
			yield script.parent


def _skip_string(source: str, index: int) -> int:
	"""Index just after the string literal (', " or `) starting at `index`."""
	quote, index = source[index], index + 1
	while index < len(source) and source[index] != quote:
		index += 2 if source[index] == "\\" else 1
	return index + 1


def _skip_comment(source: str, index: int) -> int:
	if source.startswith("//", index):
		end = source.find("\n", index)
		return len(source) if end == -1 else end
	end = source.find("*/", index + 2)
	return len(source) if end == -1 else end + 2


def filter_objects(source: str) -> list[str]:
	"""The text of each object literal of the report's `filters: [...]` array (strings and comments aware)."""
	start = REPORT_SCRIPT.search(source)
	match = start and FILTERS_START.search(source, start.end())
	if not match:
		return []
	objects, depth, index, object_start = [], 0, match.end(), None
	while index < len(source):
		char = source[index]
		if char in "'\"`":
			index = _skip_string(source, index)
			continue
		if source.startswith("//", index) or source.startswith("/*", index):
			index = _skip_comment(source, index)
			continue
		if char in "{[(":
			if depth == 0 and char == "{":
				object_start = index
			depth += 1
		elif char in "}])":
			if depth == 0:  # the closing bracket of the filters array
				break
			depth -= 1
			if depth == 0 and char == "}":
				objects.append(source[object_start : index + 1])
		index += 1
	return objects


def report_filters(report_dir: Path) -> dict[str, str]:
	source = (report_dir / f"{report_dir.name}.js").read_text(encoding="utf-8")
	filters = {}
	for obj in filter_objects(source):
		if fieldname := FIELDNAME.search(obj):
			filters[fieldname.group(1)] = obj
	return filters


class TestReportFilters(UnitTestCase):
	def test_the_filter_parser_sees_every_report(self):
		dirs = list(report_dirs())
		self.assertGreaterEqual(len(dirs), 20)
		for report_dir in dirs:
			with self.subTest(report=report_dir.name):
				self.assertTrue(report_filters(report_dir), "no filters parsed")

	def test_company_filter_defaults_to_the_user_company(self):
		offenders, checked = [], 0
		for report_dir in report_dirs():
			python = report_dir / f"{report_dir.name}.py"
			if not python.is_file() or not COMPANY_REQUIRED.search(python.read_text(encoding="utf-8")):
				continue
			checked += 1
			company = report_filters(report_dir).get("company")
			if not company:
				offenders.append(f"{report_dir.name}: no company filter")
			elif not (REQD.search(company) and COMPANY_DEFAULT.search(company)):
				offenders.append(f"{report_dir.name}: company not reqd / not the user's default Company")
		self.assertEqual(offenders, [])
		self.assertGreaterEqual(checked, 20)

	def test_required_filters_have_a_default(self):
		"""A `reqd` filter without a default blocks (or fails) the first run of the report."""
		offenders = []
		for report_dir in report_dirs():
			for fieldname, obj in report_filters(report_dir).items():
				if REQD.search(obj) and not DEFAULT.search(obj):
					if (report_dir.name, fieldname) not in NO_DEFAULT_NEEDED:
						offenders.append(f"{report_dir.name}.{fieldname}")
		self.assertEqual(offenders, [])

	def test_top_level_script_names_are_unique(self):
		seen: dict[str, str] = {}
		duplicates = []
		for path in sorted(APP_PATH.rglob("*.js")):
			relative = str(path.relative_to(APP_PATH))
			for name in TOP_LEVEL_NAME.findall(path.read_text(encoding="utf-8")):
				if name in seen and seen[name] != relative:
					duplicates.append(f"{name}: {seen[name]} and {relative}")
				seen.setdefault(name, relative)
		self.assertEqual(duplicates, [])
		self.assertGreater(len(seen), 100)

	def test_cheque_timing_filters(self):
		"""The Cheque Timing filter (empty / Current-dated / Post-dated, labels through `__()`) sits where the
		spec puts it, and the stored English value is translated when shown."""
		for name, after in TIMING_REPORTS.items():
			with self.subTest(report=name):
				report_dir = next(d for d in report_dirs() if d.name == name)
				filters = report_filters(report_dir)
				self.assertIn("cheque_timing", filters)
				for part in TIMING_FILTER_PARTS:
					self.assertRegex(filters["cheque_timing"], part)
				if after:
					fieldnames = list(filters)
					self.assertEqual(fieldnames.index("cheque_timing"), fieldnames.index(after) + 1)

		reports = APP_PATH / "pdc" / "report"
		register = (reports / "pdc_register" / "pdc_register.js").read_text(encoding="utf-8")
		register = register.split("formatter:", 1)[1]
		translated = re.search(r"\[([^\]]*)\]\.includes\(column\.fieldname\)\s*\)\s*\{([^}]*)\}", register)
		self.assertTrue(translated, "the formatter's translated fields")
		self.assertIn('"cheque_timing"', translated.group(1))
		self.assertIn("__(value)", translated.group(2))
		leaf = (reports / "cheque_leaf_register" / "cheque_leaf_register.js").read_text(encoding="utf-8")
		self.assertRegex(leaf, r'case "cheque_timing":\s*return value \? esc\(__\(value\)\) : "";')

	def test_frappe_date_objects_are_not_used_as_moments(self):
		"""`frappe.datetime.now_datetime(true)` & co. return a JS Date: `.hours()` / `.format()` do not exist
		(Treasury Settings failed on every refresh with "now.hours is not a function")."""
		offenders = []
		for path in sorted(APP_PATH.rglob("*.js")):
			source = path.read_text(encoding="utf-8")
			for match in AS_DATE.finditer(source):
				line_start = source.rfind("\n", 0, match.start()) + 1
				assigned = ASSIGNED_TO.search(source[line_start : match.start()])  # `const now = ...`
				rest = source[match.end() :]
				if re.match(MOMENT_CALL, rest):
					offenders.append(f"{path.name}: {match.group(0)} used as a moment")
				elif assigned and re.search(rf"\b{assigned.group(1)}{MOMENT_CALL}", rest):
					offenders.append(f"{path.name}: {assigned.group(1)} used as a moment")
		self.assertEqual(offenders, [])
