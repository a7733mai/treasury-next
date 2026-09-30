"""Phase 8a: the Treasury sidebar and workspace are complete (spec 8 §3.3, §7).

- the sidebar links every non-child DocType, every Report and the Page of the app, grouped in the six sections
  "Cheques", "Transfers & Charges", "Security Cheques", "Bank Facilities", "Reports", "Setup";
- the workspace cards hold the same links under the same labels, with correct link counts, and every content
  block names an existing widget;
- no link is dangling, the section / card labels are translated and both records were re-synced (`modified`).
"""

import json

import frappe
from frappe.tests import IntegrationTestCase

from treasury.tests.test_translations import (
	APP_DIR,
	PO_FILE,
	SIDEBAR_FILE,
	WORKSPACE_FILE,
	app_doctypes,
	core_msgids,
	parse_po,
	read_json,
)

SECTIONS = ["Cheques", "Transfers & Charges", "Security Cheques", "Bank Facilities", "Reports", "Setup"]
SETUP = "Setup"
REPORTS = "Reports"
SETUP_ONLY = ("Treasury Settings", "Treasury Company Settings", "Treasury Alert Log")
# Useful core links (spec 8 §3.3), in the sidebar and the workspace
EXPECTED_CORE = frozenset(
	{
		("DocType", "Bank Transaction"),
		("DocType", "Invoice Discounting"),
		("DocType", "Mode of Payment"),
		("DocType", "Bank Account"),
		("DocType", "Document Naming Settings"),
		("DocType", "Document Naming Rule"),
	}
)
# Not linked on purpose (risk R4): a second way to clear Treasury vouchers without the cheque lifecycle
FORBIDDEN_CORE = frozenset({"Bank Reconciliation Tool", "Bank Clearance"})
# Singles that are tools of daily work rather than settings
TOOL_SINGLES = frozenset({"PDC Bank Clearing"})
PAGE = "treasury-dashboard"
PREVIOUS_MODIFIED = "2026-10-26 12:00:00"
LINK_TYPES = ("DocType", "Report", "Page")
# content block type -> (its data key, the workspace table holding the widget)
BLOCK_WIDGETS = {
	"shortcut": ("shortcut_name", "shortcuts"),
	"card": ("card_name", None),
	"number_card": ("number_card_name", "number_cards"),
	"chart": ("chart_name", "charts"),
}


def sidebar_sections(items: list[dict]) -> dict[str, list[dict]]:
	"""Link items by section label: an item belongs to the last Section Break before it."""
	sections, current = {}, None
	for item in items:
		if item["type"] == "Section Break":
			current = item["label"]
			sections[current] = []
		elif current is not None:
			sections[current].append(item)
	return sections


def workspace_cards(links: list[dict]) -> list[tuple[dict, list[dict]]]:
	"""(Card Break, the link rows that follow it) of the workspace links."""
	cards = []
	for link in links:
		if link["type"] == "Card Break":
			cards.append((link, []))
		elif cards:
			cards[-1][1].append(link)
	return cards


def targets(rows: list[dict]) -> set[tuple[str, str]]:
	return set(ordered_targets(rows))


def ordered_targets(rows: list[dict]) -> list[tuple[str, str]]:
	return [(row["link_type"], row["link_to"]) for row in rows if row.get("link_type") in LINK_TYPES]


class TestWorkspaceCompleteness(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.sidebar = read_json(SIDEBAR_FILE)
		cls.workspace = read_json(WORKSPACE_FILE)
		cls.items = cls.sidebar["items"]
		cls.sections = sidebar_sections(cls.items)

	def test_sidebar_links_every_app_record(self):
		linked = targets(self.items)
		doctypes = {name: definition for name, definition in app_doctypes() if not definition.get("istable")}
		reports = {read_json(path)["name"] for path in APP_DIR.glob("*/report/*/*.json")}
		pages = {read_json(path)["name"] for path in APP_DIR.glob("*/page/*/*.json")}
		self.assertGreater(len(doctypes), 15)
		self.assertEqual(len(reports), 23)
		self.assertEqual(pages, {PAGE})

		expected = {("DocType", name) for name in doctypes}
		expected |= {("Report", name) for name in reports} | {("Page", name) for name in pages}
		self.assertEqual(sorted(expected - linked), [])
		workspace_linked = targets(self.workspace["links"])
		self.assertEqual(sorted(EXPECTED_CORE - linked), [])
		self.assertEqual(sorted(EXPECTED_CORE - workspace_linked), [])
		shortcuts = {s.get("link_to") for s in self.workspace["shortcuts"]}
		for name in FORBIDDEN_CORE:
			with self.subTest(forbidden=name):
				self.assertNotIn(name, {link_to for _type, link_to in linked | workspace_linked})
				self.assertNotIn(name, shortcuts)

		setup ={item["link_to"] for item in self.sections[SETUP]}
		section_of = {}
		for label, rows in self.sections.items():
			for row in rows:
				section_of.setdefault(row["link_to"], []).append(label)
		for name, definition in doctypes.items():
			if definition.get("issingle") and name not in TOOL_SINGLES:
				with self.subTest(single=name):
					self.assertEqual(section_of.get(name), [SETUP])
		for name in SETUP_ONLY:
			self.assertIn(name, setup)

	def test_sidebar_sections(self):
		self.assertEqual([i["label"] for i in self.items if i["type"] == "Section Break"], SECTIONS)
		self.assertEqual((self.items[0]["link_type"], self.items[0]["link_to"]), ("Workspace", "Treasury"))
		self.assertEqual((self.items[1]["link_type"], self.items[1]["link_to"]), ("Page", PAGE))
		for item in self.items:
			with self.subTest(item=item["label"]):
				if item["type"] == "Section Break":
					self.assertEqual((item["indent"], item["link_type"]), (1, "DocType"))
					self.assertTrue(item.get("icon"))
				elif item["label"] not in ("Home", "Treasury Dashboard"):
					self.assertEqual((item["child"], item["indent"]), (1, 0))
					self.assertFalse(item.get("icon"))

	def test_no_dangling_links(self):
		rows = [(i["link_type"], i["link_to"], "sidebar") for i in self.items if i.get("link_to")]
		rows += [
			(link["link_type"], link["link_to"], "workspace link")
			for link in self.workspace["links"]
			if link["type"] == "Link"
		]
		rows += [
			(shortcut["type"], shortcut["link_to"], "workspace shortcut")
			for shortcut in self.workspace["shortcuts"]
			if shortcut.get("link_to")
		]
		dangling = [
			f"{where}: {link_type} {name}"
			for link_type, name, where in rows
			if link_type not in ("DocType", "Report", "Page", "Workspace")
			or not frappe.db.exists(link_type, name)
		]
		self.assertEqual(dangling, [])

	def test_workspace_matches_sidebar(self):
		cards = workspace_cards(self.workspace["links"])
		self.assertEqual([card["label"] for card, _rows in cards], SECTIONS)
		for card, rows in cards:
			with self.subTest(card=card["label"]):
				self.assertEqual(card["link_count"], len(rows))
				if card["label"] == REPORTS:
					# the Page is the first link of Reports (in the sidebar: the second item, above the sections)
					self.assertEqual((rows[0]["link_type"], rows[0]["link_to"]), ("Page", PAGE))
					rows = rows[1:]
				# same links in the same order as the sidebar section
				self.assertEqual(ordered_targets(rows), ordered_targets(self.sections[card["label"]]))
		self.assertEqual(targets(self.workspace["links"]), targets(self.items))

		labels = {
			"card": {card["label"] for card, _rows in cards},
			**{
				block_type: {row["label"] for row in self.workspace[table]}
				for block_type, (_key, table) in BLOCK_WIDGETS.items()
				if table
			},
		}
		blocks = json.loads(self.workspace["content"])
		self.assertEqual(len({b["id"] for b in blocks}), len(blocks))
		for block in blocks:
			if block["type"] in BLOCK_WIDGETS:
				key = BLOCK_WIDGETS[block["type"]][0]
				with self.subTest(block=block["id"]):
					self.assertIn(block["data"][key], labels[block["type"]])
		used = {block["data"]["card_name"] for block in blocks if block["type"] == "card"}
		self.assertEqual(used, set(SECTIONS))

	def test_labels_translated(self):
		"""The sidebar translates its labels without context (`_(label)`); the workspace cards too."""
		core = core_msgids()
		plain = {e["msgid"] for e in parse_po(PO_FILE) if e["msgid"] and "msgctxt" not in e}
		labels = [i["label"] for i in self.items if i["type"] == "Section Break"]
		labels += [card["label"] for card, _rows in workspace_cards(self.workspace["links"])]
		missing = sorted({label for label in labels if label not in plain and label not in core})
		self.assertEqual(missing, [])

	def test_modified_bumped(self):
		self.assertGreater(self.sidebar["modified"], PREVIOUS_MODIFIED)
		self.assertGreater(self.workspace["modified"], PREVIOUS_MODIFIED)
