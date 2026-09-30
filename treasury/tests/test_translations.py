"""The Arabic catalogue (locale/ar.po) is valid, complete and never overrides Frappe / ERPNext wording.

- ar.po parses (header, quoting, escapes), has no duplicate (msgctxt, msgid) pair, no empty translation and
  keeps the numbered placeholders of every message.
- Every literal `_("...")` of the Python code and `__("...")` of the client scripts has a translation: ours
  (with the context of the call, or without context, which Frappe falls back to), or Frappe / ERPNext's own.
- Generic words that Frappe / ERPNext already translate are never translated by us without a context: an entry
  without msgctxt is loaded site-wide after the core apps and would replace their wording everywhere.
- Every field / section / tab label of every app DocType has an entry under its DocType as msgctxt (Frappe
  translates labels and Select options with `__(label, null, df.parent)`), or a plain entry for an
  app-specific label; generic Select options have an entry under their DocType too.
- The treasury terms (Amount, Due Date, Issue Date ...), whose core translation is wrong for a treasury, are
  never translated without a context in the Python code, the client scripts and the Jinja / report templates,
  and have an entry under msgctxt "Treasury".
- Phase 8a: the English catalogue (locale/en.po) shows "Cheque(s)" for every user-visible string that names
  "PDC" (internal names stay "PDC ..."), only overrides strings of the app, and every override has an Arabic
  entry; the Arabic wording never says "PDC", and "آجل" only where post-dated / usance is meant.
"""

import ast
import json
import re
from pathlib import Path

import frappe
from frappe.tests import IntegrationTestCase

APP_DIR = Path(__file__).resolve().parents[1]
PO_FILE = APP_DIR / "locale" / "ar.po"
EN_PO_FILE = APP_DIR / "locale" / "en.po"
CORE_APPS = ("frappe", "erpnext")

# Phase 8: the screens say "Cheque(s)"; "PDC" survives only in internal names, which en.po / ar.po translate.
PDC_WORD = re.compile(r"\bPDC\b")
POST_DATED_WORD = "آجل"
# (msgctxt, msgid) of the Arabic entries where "آجل" really means usance or post-dated
POST_DATED_ALLOWED = frozenset(
	{
		("Letter of Credit", "Usance"),
		(
			None,
			"An import Letter of Credit opened by the bank under a Bank Facility: cash margin and commission on "
			"opening, amendments, documents, acceptance (usance) and settlement to the supplier.",
		),
		(
			None,
			"Usance: the date the bank pays the supplier (expected outflow in the Bank Position Forecast).",
		),
		(None, "Only a Usance Letter of Credit is accepted; settle a Sight credit directly."),
		(None, "Usance Days are required for a Usance Letter of Credit."),
		(
			None,
			"Auto Debit: the bank debits each installment (Treasury Company Settings: Auto Debit Posting). PDC to "
			"Lender: one post-dated cheque per installment (Generate Installment Cheques).",
		),
		(
			None,
			"Each installment is paid by a post-dated cheque to {0}. The cheque is posted on its date as the "
			"repayment; nothing is posted at delivery.",
		),
		# Phase 8b: the Cheque Timing option and its field description
		(None, "Post-dated"),
		(
			None,
			"Current-dated: due on its issue date. Post-dated: due after its issue date. Set from the dates on "
			"save.",
		),
	}
)
SOURCE_SUFFIXES = (".py", ".js", ".html", ".json")
SIDEBAR_FILE = APP_DIR / "workspace_sidebar" / "treasury.json"
WORKSPACE_FILE = APP_DIR / "pdc" / "workspace" / "treasury" / "treasury.json"

# Generic words translated by Frappe / ERPNext: we may only translate them with a context (msgctxt), and code
# using them without context relies on the core translation.
GENERIC_CORE_WORDS = frozenset(
	{
		"Accounts",
		"Actions",
		"Active",
		"Allocated Amount",
		"Amount",
		"Asset",
		"Bank",
		"Bank Account",
		"Cancelled",
		"Clear",
		"Closed",
		"Company",
		"Contact Person",
		"Create",
		"Credit",
		"Currency",
		"Customer",
		"Date",
		"Debit",
		"Deposit",
		"Description",
		"Disabled",
		"Draft",
		"Due Date",
		"Employee",
		"Error",
		"Event",
		"Exchange Rate",
		"Expense",
		"Expired",
		"Expiry Date",
		"From Date",
		"General",
		"Journal Entry",
		"Leaves",
		"Liability",
		"Manual",
		"More Information",
		"Not Allowed",
		"Not Permitted",
		"Other",
		"Outstanding Amount",
		"Overdue",
		"Paid",
		"Partially Paid",
		"Party",
		"Party Name",
		"Party Type",
		"Payment Entry",
		"Posting Date",
		"Print",
		"Purchase Invoice",
		"Reason",
		"Receive",
		"Reference Name",
		"Reference Type",
		"References",
		"Remarks",
		"Replace",
		"Series",
		"Start Date",
		"Status",
		"Submitted",
		"Supplier",
		"To Date",
		"Total",
		"Total Amount",
		"Unpaid",
		"User",
		"Voucher No",
		"Voucher Type",
		"Warning",
	}
)

# Financial words whose Frappe / ERPNext translation is wrong for a treasury ("Amount" = quantity, "Due Date",
# "Issue Date" = the date of a lawsuit, "Total" = total excluding tax, "Outstanding" = pending ...). App code
# always passes a context: TREASURY_CONTEXT, a DocType name or "Treasury Report".
TREASURY_CONTEXT = "Treasury"
TREASURY_TERMS = frozenset(
	{
		"Amount",
		"Balance",
		"Bank",
		"Bank Account",
		"Charges",
		"Closing",
		"Closing Balance",
		"Custodian",
		"Custody",
		"Date",
		"Due Date",
		"Exchange Rate",
		"Expiry Date",
		"Interest Rate",
		"Issue Date",
		"Limit",
		"Net",
		"Opening",
		"Opening Balance",
		"Outstanding",
		"Overdue",
		"Party",
		"Period",
		"Posting Date",
		"Rate",
		"Reference",
		"Reference Name",
		"Release Date",
		"Remarks",
		"Status",
		"Total",
	}
)
GENERIC_WORDS = GENERIC_CORE_WORDS | TREASURY_TERMS

# A context argument that cannot be resolved statically (an attribute, a variable of another module...).
ANY_CONTEXT = object()

PLACEHOLDER = re.compile(r"\{\d*\}")
JS_STRING = r"""(?P<{name}q>["'])(?P<{name}>(?:\\.|(?!(?P={name}q))[^\\\n])*)(?P={name}q)"""
JS_CALL = re.compile(r"(?<![\w.$])__\(\s*" + JS_STRING.format(name="msg"))
JS_NO_CONTEXT = re.compile(r"\s*\)")
JS_CONTEXT = re.compile(
	r"\s*,\s*(?:null|undefined|\[[^\[\]]*\]|\{[^{}]*\}|[\w.$]+)\s*,\s*(?:"
	+ JS_STRING.format(name="ctx")
	+ r"|(?P<ctxname>[A-Za-z_$][\w$]*))\s*\)"
)
JS_CONSTANT = re.compile(
	r"(?:const|let|var)\s+(?P<name>[A-Za-z_$][\w$]*)\s*=\s*" + JS_STRING.format(name="value")
)
JS_ESCAPE = re.compile(r"\\(.)")
# Jinja `_("...")` (print formats, emails) and microtemplate `__("...")` (report print templates)
TEMPLATE_CALL = re.compile(r"(?<![\w.$])_{1,2}\(\s*" + JS_STRING.format(name="msg"))
JINJA_CONTEXT = re.compile(r"\s*,\s*context\s*=\s*" + JS_STRING.format(name="ctx") + r"\s*\)")


# ---------------------------------------------------------------------------------------------- parsing


def unquote(value: str, where: str) -> str:
	value = value.strip()
	if len(value) < 2 or not (value.startswith('"') and value.endswith('"')):
		raise ValueError(f"{where}: expected a quoted string, got {value!r}")
	return ast.literal_eval(value)


def parse_po(path: Path) -> list[dict]:
	"""Entries of a .po file as dicts (msgctxt, msgid, msgstr, line); raises ValueError on invalid syntax."""
	entries, entry, field = [], {}, None
	for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
		where = f"{path.name}:{lineno}"
		line = raw.strip()
		if not line or line.startswith("#"):
			continue
		if line.startswith('"'):
			if field is None:
				raise ValueError(f"{where}: continuation line outside an entry")
			entry[field] += unquote(line, where)
			continue

		keyword, _sep, value = line.partition(" ")
		if keyword not in ("msgctxt", "msgid", "msgstr"):
			raise ValueError(f"{where}: unexpected keyword {keyword!r}")
		starts_entry = keyword == "msgctxt" or (keyword == "msgid" and "msgid" in entry)
		if starts_entry and entry:
			entries.append(entry)
			entry = {}
		if keyword in entry:
			raise ValueError(f"{where}: {keyword} appears twice in the same entry")
		if keyword == "msgstr" and "msgid" not in entry:
			raise ValueError(f"{where}: msgstr without msgid")
		entry[keyword] = unquote(value, where)
		entry.setdefault("line", lineno)
		field = keyword
	if entry:
		entries.append(entry)

	for entry in entries:
		if "msgid" not in entry or "msgstr" not in entry:
			raise ValueError(f"{path.name}:{entry['line']}: incomplete entry")
	return entries


def core_msgids() -> set[str]:
	"""Every message translated to Arabic by the core apps (empty when their catalogues are not available)."""
	try:
		from babel.messages.pofile import read_po
	except ImportError:
		return set()

	msgids = set()
	for app in CORE_APPS:
		try:
			path = Path(frappe.get_app_path(app, "locale", "ar.po"))
		except Exception:
			continue
		if not path.exists():
			continue
		with open(path, "rb") as f:
			catalog = read_po(f)
		for message in catalog:
			msgid = message.id if isinstance(message.id, str) else message.id[0]
			if msgid and message.string:
				msgids.add(msgid)
	return msgids


# ------------------------------------------------------------------------------------------- extraction


def source_files(suffix: str) -> list[Path]:
	return sorted(
		path
		for path in APP_DIR.rglob(f"*{suffix}")
		if "tests" not in path.relative_to(APP_DIR).parts
		and not path.name.startswith("test_")
		and "node_modules" not in path.parts
	)


def is_translate_call(func: ast.expr) -> bool:
	if isinstance(func, ast.Name):
		return func.id == "_"
	return (
		isinstance(func, ast.Attribute)
		and func.attr == "_"
		and isinstance(func.value, ast.Name)
		and func.value.id == "frappe"
	)


def python_messages(path: Path):
	"""(msgid, context, line) of every `_("literal", ...)` call; context None, a string or ANY_CONTEXT."""
	tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
	constants = {
		target.id: node.value.value
		for node in tree.body
		if isinstance(node, ast.Assign)
		and isinstance(node.value, ast.Constant)
		and isinstance(node.value.value, str)
		for target in node.targets
		if isinstance(target, ast.Name)
	}

	for node in ast.walk(tree):
		if not (isinstance(node, ast.Call) and is_translate_call(node.func) and node.args):
			continue
		first = node.args[0]
		if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
			continue
		context = next((kw.value for kw in node.keywords if kw.arg == "context"), None)
		if context is None and len(node.args) > 2:
			context = node.args[2]

		if context is None or (isinstance(context, ast.Constant) and context.value is None):
			resolved = None
		elif isinstance(context, ast.Constant) and isinstance(context.value, str):
			resolved = context.value
		elif isinstance(context, ast.Name) and context.id in constants:
			resolved = constants[context.id]
		else:
			resolved = ANY_CONTEXT
		yield first.value, resolved, node.lineno


def js_unescape(value: str) -> str:
	return JS_ESCAPE.sub(lambda m: {"n": "\n", "t": "\t"}.get(m.group(1), m.group(1)), value)


def js_messages(path: Path):
	"""(msgid, context, line) of every `__("literal", ...)` call of a client script."""
	text = path.read_text(encoding="utf-8")
	constants = {m.group("name"): js_unescape(m.group("value")) for m in JS_CONSTANT.finditer(text)}

	for match in JS_CALL.finditer(text):
		msgid = js_unescape(match.group("msg"))
		line = text.count("\n", 0, match.start()) + 1
		rest = text[match.end() :]
		if JS_NO_CONTEXT.match(rest):
			yield msgid, None, line
			continue
		context = JS_CONTEXT.match(rest)
		if context and context.group("ctx") is not None:
			yield msgid, js_unescape(context.group("ctx")), line
		elif context and context.group("ctxname") in constants:
			yield msgid, constants[context.group("ctxname")], line
		elif context and context.group("ctxname") in ("null", "undefined"):
			yield msgid, None, line
		else:
			# replacements too complex for a regex: accept the message under any context
			yield msgid, ANY_CONTEXT, line


def template_messages(path: Path):
	"""(msgid, context, line) of every literal `_("...")` / `__("...")` call of a Jinja or report template."""
	text = path.read_text(encoding="utf-8")
	for match in TEMPLATE_CALL.finditer(text):
		msgid = js_unescape(match.group("msg"))
		line = text.count("\n", 0, match.start()) + 1
		rest = text[match.end() :]
		if JS_NO_CONTEXT.match(rest):
			yield msgid, None, line
		elif context := (JINJA_CONTEXT.match(rest) or JS_CONTEXT.match(rest)):
			if context.group("ctx") is not None:
				yield msgid, js_unescape(context.group("ctx")), line
			elif context.groupdict().get("ctxname") in ("null", "undefined"):
				yield msgid, None, line
			else:
				yield msgid, ANY_CONTEXT, line
		else:
			yield msgid, ANY_CONTEXT, line


MESSAGE_EXTRACTORS = ((".py", python_messages), (".js", js_messages), (".html", template_messages))


def app_doctypes():
	"""(name, definition) of every DocType of the app."""
	for path in sorted(APP_DIR.glob("*/doctype/*/*.json")):
		definition = json.loads(path.read_text(encoding="utf-8"))
		if definition.get("doctype") == "DocType":
			yield definition["name"], definition


def read_json(path: Path) -> dict:
	return json.loads(path.read_text(encoding="utf-8"))


def source_texts() -> list[str]:
	"""The text of every app source file (code, templates, JSON records), tests and catalogues excluded."""
	texts = []
	for suffix in SOURCE_SUFFIXES:
		for path in sorted(APP_DIR.rglob(f"*{suffix}")):
			parts = path.relative_to(APP_DIR).parts
			if {"tests", "locale", "node_modules"} & set(parts) or path.name.startswith("test_"):
				continue
			texts.append(path.read_text(encoding="utf-8"))
	return texts


def workspace_labels():
	"""(label, where) of the sidebar items and of the workspace links, shortcuts, number cards and charts."""
	for item in read_json(SIDEBAR_FILE)["items"]:
		yield item["label"], "sidebar"
	workspace = read_json(WORKSPACE_FILE)
	for key in ("links", "shortcuts", "number_cards", "charts"):
		for row in workspace.get(key, []):
			yield row["label"], f"workspace {key}"


def visible_strings():
	"""(text, where) of every string of the app a user can see: translated literals of the code and templates,
	DocType names / labels / descriptions / Select options, report, print format, number card and chart names,
	number card labels, module names and the workspace / sidebar labels."""
	for suffix, extract in MESSAGE_EXTRACTORS:
		for path in source_files(suffix):
			for msgid, _context, line in extract(path):
				yield msgid, f"{path.relative_to(APP_DIR)}:{line}"

	for doctype, definition in app_doctypes():
		yield doctype, f"DocType {doctype}"
		if definition.get("description"):
			yield definition["description"], f"DocType {doctype}"
		for df in definition.get("fields", []):
			where = f"{doctype}.{df['fieldname']}"
			for key in ("label", "description"):
				if df.get(key):
					yield df[key], where
			if df.get("fieldtype") == "Select" and df["fieldname"] != "naming_series":
				for option in (df.get("options") or "").split("\n"):
					if option:
						yield option, where

	for kind in ("report", "print_format", "number_card", "dashboard_chart"):
		for path in sorted(APP_DIR.glob(f"*/{kind}/*/*.json")):
			definition = read_json(path)
			yield definition["name"], str(path.relative_to(APP_DIR))
			if kind == "number_card" and definition.get("label"):
				yield definition["label"], str(path.relative_to(APP_DIR))

	for module in (APP_DIR / "modules.txt").read_text(encoding="utf-8").splitlines():
		if module.strip():
			yield module.strip(), "modules.txt"

	yield from workspace_labels()


# ---------------------------------------------------------------------------------------------- tests


class TestTranslations(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.entries = parse_po(PO_FILE)
		cls.header = cls.entries[0]
		cls.messages = [e for e in cls.entries if e["msgid"]]
		cls.contexts_by_msgid = {}
		for entry in cls.messages:
			cls.contexts_by_msgid.setdefault(entry["msgid"], set()).add(entry.get("msgctxt"))
		cls.en_entries = parse_po(EN_PO_FILE)
		cls.en_messages = [e for e in cls.en_entries if e["msgid"]]

	def test_header(self):
		self.assertEqual(self.header["msgid"], "", "the first entry must be the header")
		self.assertNotIn("msgctxt", self.header)
		self.assertIn("Content-Type: text/plain; charset=UTF-8\n", self.header["msgstr"])
		self.assertIn("Language: ar\n", self.header["msgstr"])
		self.assertEqual([e["line"] for e in self.entries if not e["msgid"]], [self.header["line"]])

	def test_babel_reads_the_catalogue(self):
		try:
			from babel.messages.pofile import read_po
		except ImportError:
			self.skipTest("babel is not installed")

		with open(PO_FILE, "rb") as f:
			try:
				catalog = read_po(f, abort_invalid=True)
			except TypeError:  # older babel without abort_invalid
				f.seek(0)
				catalog = read_po(f)
		self.assertEqual(len([m for m in catalog if m.id]), len(self.messages))

	def test_no_duplicate_entries(self):
		seen, duplicates = {}, []
		for entry in self.messages:
			key = (entry.get("msgctxt"), entry["msgid"])
			if key in seen:
				duplicates.append(f"line {entry['line']} (first at line {seen[key]}): {key}")
			seen.setdefault(key, entry["line"])
		self.assertEqual(duplicates, [])

	def test_every_entry_is_translated_with_its_placeholders(self):
		problems = []
		for entry in self.messages:
			if not entry["msgstr"].strip():
				problems.append(f"line {entry['line']}: empty translation of {entry['msgid']!r}")
			elif sorted(PLACEHOLDER.findall(entry["msgid"])) != sorted(PLACEHOLDER.findall(entry["msgstr"])):
				problems.append(f"line {entry['line']}: placeholders differ in {entry['msgid']!r}")
		self.assertEqual(problems, [])

	def test_generic_core_words_only_with_context(self):
		plain = sorted(
			f"line {e['line']}: {e['msgid']}"
			for e in self.messages
			if "msgctxt" not in e and e["msgid"] in GENERIC_WORDS
		)
		self.assertEqual(plain, [])

	def test_treasury_terms_have_a_treasury_entry(self):
		missing = sorted(
			t for t in TREASURY_TERMS if TREASURY_CONTEXT not in self.contexts_by_msgid.get(t, ())
		)
		self.assertEqual(missing, [])

	def test_treasury_terms_are_never_translated_without_context(self):
		plain, count = [], 0
		for suffix, extract in MESSAGE_EXTRACTORS:
			for path in source_files(suffix):
				for msgid, context, line in extract(path):
					count += 1
					if msgid in TREASURY_TERMS and context is None:
						plain.append(f"{path.relative_to(APP_DIR)}:{line}: {msgid!r}")

		self.assertGreater(count, 500, "the extraction found (almost) no translatable strings")
		self.assertEqual(sorted(plain), [])

	def test_every_doctype_label_has_an_entry(self):
		generic = GENERIC_WORDS | core_msgids()
		missing, count = [], 0
		for doctype, definition in app_doctypes():
			for df in definition.get("fields", []):
				label = df.get("label")
				if not label:
					continue
				count += 1
				contexts = self.contexts_by_msgid.get(label, set())
				if doctype in contexts or (None in contexts and label not in generic):
					continue
				missing.append(f"{doctype}: {df['fieldname']} {label!r}")

		self.assertGreater(count, 300, "no DocType labels found")
		self.assertEqual(sorted(missing), [])

	def test_generic_select_options_have_a_doctype_entry(self):
		missing = []
		for doctype, definition in app_doctypes():
			for df in definition.get("fields", []):
				if df.get("fieldtype") != "Select" or df.get("fieldname") == "naming_series":
					continue
				for option in (df.get("options") or "").split("\n"):
					if option in GENERIC_WORDS and doctype not in self.contexts_by_msgid.get(option, ()):
						missing.append(f"{doctype}: {df['fieldname']} {option!r}")
		self.assertEqual(sorted(missing), [])

	def test_every_translatable_string_has_an_entry(self):
		core = core_msgids()
		missing, count = [], 0
		for suffix, extract in MESSAGE_EXTRACTORS:
			for path in source_files(suffix):
				for msgid, context, line in extract(path):
					count += 1
					if not self.is_translated(msgid, context, core):
						shown = "" if context in (None, ANY_CONTEXT) else f" [context {context!r}]"
						missing.append(f"{path.relative_to(APP_DIR)}:{line}: {msgid!r}{shown}")

		self.assertGreater(count, 500, "the extraction found (almost) no translatable strings")
		self.assertEqual(sorted(set(missing)), [])

	def is_translated(self, msgid: str, context, core: set[str]) -> bool:
		if msgid in GENERIC_CORE_WORDS or msgid in core:
			return True
		contexts = self.contexts_by_msgid.get(msgid, set())
		if context is ANY_CONTEXT:
			return bool(contexts)
		# Frappe looks up "msgid:context" first, then the entry without context
		return None in contexts or (context is not None and context in contexts)

	def test_payment_mode_methods_are_translated(self):
		"""The methods of the Modes of Payment map (grid Select options, generated mode names and messages
		translate them with the child DocType as context)."""
		from treasury.accounting.payment_modes import MAP_DOCTYPE, METHODS

		missing = [m for m in METHODS if MAP_DOCTYPE not in self.contexts_by_msgid.get(m, ())]
		self.assertEqual(missing, [])

	def test_bank_position_alert_terms(self):
		"""Phase 7e: the Select options, the levels (shown with the Treasury Alert Log context), the status log
		event and the override field have Arabic entries."""
		expected = {
			"Treasury Company Settings": ("Ignore", "Warn", "Block"),
			"Treasury Alert Log": (
				"Digest",
				"Balance Alert",
				"Override",
				"Warning",
				"Critical",
				"OK",
				"No Threshold",
				"Level",
			),
			"Treasury Alert Recipient": ("Role", "User"),
			"Treasury Report": ("In", "Out"),
		}
		missing = [
			f"{context}: {msgid}"
			for context, msgids in expected.items()
			for msgid in msgids
			if context not in self.contexts_by_msgid.get(msgid, ())
		]
		for msgid in ("Low Balance Override", "Override Low Balance", "Bank Balance Outlook"):
			if None not in self.contexts_by_msgid.get(msgid, ()):
				missing.append(msgid)
		self.assertEqual(missing, [])

	def test_extraction_helpers(self):
		"""The static extraction understands the call styles used in the app."""
		py = APP_DIR / "utils" / "amount_in_words.py"
		found = {(msgid, context) for msgid, context, _line in python_messages(py)}
		self.assertIn(("{0} only", "Amount in words"), found)

		js = APP_DIR / "pdc" / "doctype" / "cheque_batch" / "cheque_batch.js"
		found = {(msgid, context) for msgid, context, _line in js_messages(js)}
		self.assertIn(("Invoice", "Cheque Batch"), found)  # context through a constant
		self.assertIn(("Print", "PDC Cheque"), found)  # literal context
		self.assertIn(("Generate Cheques", None), found)  # no context
		self.assertIn(
			(
				"One invoice per cheque: part of a cheque stays an advance while invoices are still open. "
				"Allocate those rows by hand, or reconcile the advance later.",
				None,
			),
			found,
		)  # string on its own line

		html = APP_DIR / "pdc" / "print_format" / "pdc_cheque_receipt" / "pdc_cheque_receipt.html"
		found = {(msgid, context) for msgid, context, _line in template_messages(html)}
		self.assertIn(("Amount", TREASURY_CONTEXT), found)  # Jinja context keyword
		self.assertIn(("Cheque No", None), found)

		html = APP_DIR / "pdc" / "report" / "projected_party_statement" / "projected_party_statement.html"
		found = {(msgid, context) for msgid, context, _line in template_messages(html)}
		self.assertIn(("Debit", "Treasury Report"), found)  # microtemplate `__(msg, null, context)`
		self.assertIn(("Date", TREASURY_CONTEXT), found)

		# Phase 7h: the page scripts are scanned too
		js = APP_DIR / "pdc" / "page" / "treasury_dashboard" / "treasury_dashboard.js"
		found = {(msgid, context) for msgid, context, _line in js_messages(js)}
		self.assertIn(("Treasury Dashboard", None), found)
		self.assertIn(js, source_files(".js"))

	# ------------------------------------------------------------------ Phase 8a: "PDC" is shown as "Cheques"

	def test_en_catalogue(self):
		"""en.po is valid: header, babel, no duplicate, plain entries only, translated with the placeholders,
		and never shows "PDC"."""
		header = self.en_entries[0]
		self.assertEqual(header["msgid"], "", "the first entry must be the header")
		self.assertIn("Content-Type: text/plain; charset=UTF-8\n", header["msgstr"])
		self.assertIn("Language: en\n", header["msgstr"])
		self.assertEqual([e["line"] for e in self.en_entries if not e["msgid"]], [header["line"]])
		self.assertGreater(len(self.en_messages), 40)

		try:
			from babel.messages.pofile import read_po
		except ImportError:
			read_po = None
		if read_po:
			with open(EN_PO_FILE, "rb") as f:
				try:
					catalog = read_po(f, abort_invalid=True)
				except TypeError:  # older babel without abort_invalid
					f.seek(0)
					catalog = read_po(f)
			self.assertEqual(len([m for m in catalog if m.id]), len(self.en_messages))

		problems, seen = [], set()
		for entry in self.en_messages:
			where = f"en.po:{entry['line']}"
			key = (entry.get("msgctxt"), entry["msgid"])
			if key in seen:
				problems.append(f"{where}: duplicate {key}")
			seen.add(key)
			if "msgctxt" in entry:
				problems.append(f"{where}: en.po entries are plain (no msgctxt)")
			if not entry["msgstr"].strip():
				problems.append(f"{where}: empty translation of {entry['msgid']!r}")
			elif sorted(PLACEHOLDER.findall(entry["msgid"])) != sorted(PLACEHOLDER.findall(entry["msgstr"])):
				problems.append(f"{where}: placeholders differ in {entry['msgid']!r}")
			if PDC_WORD.search(entry["msgstr"]):
				problems.append(f"{where}: 'PDC' in the English wording {entry['msgstr']!r}")
		self.assertEqual(problems, [])

	def test_en_msgids_exist_in_source(self):
		"""Every English override replaces a string the app really shows (copied byte-exactly)."""
		texts = source_texts()
		missing = []
		for entry in self.en_messages:
			msgid = entry["msgid"]
			forms = {msgid, json.dumps(msgid)[1:-1], json.dumps(msgid, ensure_ascii=False)[1:-1]}
			if not any(form in text for form in forms for text in texts):
				missing.append(f"en.po:{entry['line']}: {msgid!r}")
		self.assertEqual(missing, [])

	def test_every_visible_pdc_string_has_an_english_entry(self):
		en = {entry["msgid"] for entry in self.en_messages}
		missing, count = set(), 0
		for text, where in visible_strings():
			count += 1
			if PDC_WORD.search(text) and text not in en:
				missing.add(f"{where}: {text!r}")
		self.assertGreater(count, 800, "no visible strings found")
		self.assertEqual(sorted(missing), [])

	def test_no_pdc_in_arabic(self):
		shown = [f"ar.po:{e['line']}: {e['msgstr']!r}" for e in self.messages if PDC_WORD.search(e["msgstr"])]
		self.assertEqual(shown, [])

	def test_post_dated_word_only_where_meant(self):
		"""The Arabic "آجل" (post-dated / usance) is only used where it is meant: every cheque is "شيك"."""
		unexpected = [
			f"ar.po:{e['line']}: {e['msgid']!r}"
			for e in self.messages
			if POST_DATED_WORD in e["msgstr"] and (e.get("msgctxt"), e["msgid"]) not in POST_DATED_ALLOWED
		]
		self.assertEqual(unexpected, [])
		present = {(e.get("msgctxt"), e["msgid"]) for e in self.messages if POST_DATED_WORD in e["msgstr"]}
		self.assertEqual(sorted(POST_DATED_ALLOWED - present, key=str), [], "stale allow-list entries")

	def test_every_english_entry_has_an_arabic_entry(self):
		core = core_msgids()
		missing = [
			f"en.po:{e['line']}: {e['msgid']!r}"
			for e in self.en_messages
			if e["msgid"] not in self.contexts_by_msgid and e["msgid"] not in core
		]
		self.assertEqual(missing, [])
