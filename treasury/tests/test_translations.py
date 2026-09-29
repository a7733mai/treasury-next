"""The Arabic catalogue (locale/ar.po) is valid, complete and never overrides Frappe / ERPNext wording.

- ar.po parses (header, quoting, escapes), has no duplicate (msgctxt, msgid) pair, no empty translation and
  keeps the numbered placeholders of every message.
- Every literal `_("...")` of the Python code and `__("...")` of the client scripts has a translation: ours
  (with the context of the call, or without context, which Frappe falls back to), or Frappe / ERPNext's own.
- Generic words that Frappe / ERPNext already translate are never translated by us without a context: an entry
  without msgctxt is loaded site-wide after the core apps and would replace their wording everywhere.
"""

import ast
import re
from pathlib import Path

import frappe
from frappe.tests import IntegrationTestCase

APP_DIR = Path(__file__).resolve().parents[1]
PO_FILE = APP_DIR / "locale" / "ar.po"
CORE_APPS = ("frappe", "erpnext")

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
			if "msgctxt" not in e and e["msgid"] in GENERIC_CORE_WORDS
		)
		self.assertEqual(plain, [])

	def test_every_translatable_string_has_an_entry(self):
		core = core_msgids()
		missing, count = [], 0
		for suffix, extract in ((".py", python_messages), (".js", js_messages)):
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
