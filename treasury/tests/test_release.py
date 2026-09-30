"""Phase 7h: release 1.1.0 (spec 7h §3.7, §5, §6, §7 E): version, CHANGELOG, README, the patch order and the
workspace / sidebar records."""

import importlib
import json
import re
from pathlib import Path

import frappe
from frappe.tests import IntegrationTestCase

import treasury

APP_PATH = Path(frappe.get_app_path("treasury"))
REPO_PATH = APP_PATH.parent
PATCHES = [
	"treasury.patches.v1_1.set_bank_settlement_voucher",
	"treasury.patches.v1_1.set_bank_transfer_defaults",
	"treasury.patches.v1_1.set_document_payment_defaults",
	"treasury.patches.v1_1.set_bank_charge_defaults",
	"treasury.patches.v1_1.set_balance_alert_defaults",
	"treasury.patches.v1_1.set_cheque_leaf_defaults",
	"treasury.patches.v1_1.set_facility_defaults",
]
PAGE = "treasury-dashboard"


def read_json(*parts) -> dict:
	return json.loads(APP_PATH.joinpath(*parts).read_text(encoding="utf-8"))


class TestRelease(IntegrationTestCase):
	def test_version(self):
		self.assertEqual(treasury.__version__, "1.1.0")

	def test_changelog(self):
		lines = (REPO_PATH / "CHANGELOG.md").read_text(encoding="utf-8").splitlines()
		self.assertEqual(lines[0], "# Changelog")
		headings = [(i, line) for i, line in enumerate(lines) if line.startswith("## ")]
		self.assertRegex(headings[0][1], r"^## 1\.1\.0 — \d{4}-\d{2}-\d{2}$")
		self.assertFalse([line for line in lines if "Unreleased" in line])

		start, end = headings[0][0], headings[1][0]
		section = lines[start:end]
		positions = []
		for phase in ("7a", "7b", "7c", "7d", "7e", "7f", "7g", "7h"):
			found = [
				i for i, line in enumerate(section) if line.startswith("### ") and f"(Phase {phase})" in line
			]
			self.assertEqual(len(found), 1, phase)
			positions.append(found[0])
		self.assertEqual(positions, sorted(positions))

		self.assertEqual(headings[1][1], "## 1.0.1 — 2026-09-29")
		first = next(line for line in lines[end + 1 :] if line.strip())
		self.assertEqual(first, "Fixes found during UAT on a live ERPNext 16 site.")

	def test_readme(self):
		text = (REPO_PATH / "README.md").read_text(encoding="utf-8")
		self.assertIn("Version 1.1.0", text)
		for heading in ("What's new in 1.1", "Upgrading from 1.0", "Setup guide", "Known limitations"):
			self.assertTrue(re.search(rf"^#+ {re.escape(heading)}", text, re.M), heading)
		self.assertIn("post_scheduled_transfers", text)
		self.assertIn(PAGE, text)
		self.assertNotIn("No warning when an outgoing cheque would make the", text)

	def test_patches_order(self):
		text = (APP_PATH / "patches.txt").read_text(encoding="utf-8")
		section = text.split("[post_model_sync]", 1)[1]
		listed = [
			line.strip() for line in section.splitlines() if line.strip() and not line.strip().startswith("#")
		]
		self.assertEqual(listed, PATCHES)
		modules = sorted(p.stem for p in (APP_PATH / "patches" / "v1_1").glob("*.py") if p.stem != "__init__")
		self.assertEqual(sorted(p.rsplit(".", 1)[1] for p in listed), modules)
		for path in listed:
			with self.subTest(patch=path):
				self.assertTrue(callable(getattr(importlib.import_module(path), "execute", None)))

	def test_workspace_and_sidebar(self):
		workspace = read_json("pdc", "workspace", "treasury", "treasury.json")
		shortcuts = workspace["shortcuts"]
		self.assertEqual((shortcuts[0]["type"], shortcuts[0]["link_to"]), ("Page", PAGE))
		self.assertGreater(workspace["modified"], "2026-10-26 12:00:00")

		links = workspace["links"]
		cards = [link["label"] for link in links if link["type"] == "Card Break"]
		self.assertIn("Setup", cards)
		for removed in ("Treasury Masters", "Treasury Setup", "PDC Reports", "Bank Transfers"):
			self.assertNotIn(removed, cards)
		labels = {
			"shortcut": {s["label"] for s in shortcuts},
			"card": set(cards),
			"number_card": {c["label"] for c in workspace["number_cards"]},
			"chart": {c["label"] for c in workspace["charts"]},
		}
		name_keys = {
			"shortcut": "shortcut_name",
			"card": "card_name",
			"number_card": "number_card_name",
			"chart": "chart_name",
		}
		blocks = json.loads(workspace["content"])
		ids = [b["id"] for b in blocks]
		self.assertEqual(len(ids), len(set(ids)))
		self.assertEqual(blocks[1]["data"].get("shortcut_name"), "Treasury Dashboard")
		for block in blocks:
			if block["type"] in name_keys:
				with self.subTest(block=block["id"]):
					self.assertIn(block["data"][name_keys[block["type"]]], labels[block["type"]])

		for index, link in enumerate(links):
			if link["type"] != "Card Break":
				continue
			count = 0
			for row in links[index + 1 :]:
				if row["type"] == "Card Break":
					break
				count += 1
			self.assertEqual(link["link_count"], count, link["label"])

		setup_card = next(
			i for i, link in enumerate(links) if link["type"] == "Card Break" and link["label"] == "Setup"
		)
		setup = links[setup_card + 1 : setup_card + 1 + links[setup_card]["link_count"]]
		self.assertEqual(
			[row["link_to"] for row in setup],
			[
				"Treasury Company Settings",
				"Treasury Settings",
				"Mode of Payment",
				"Bank Account",
				"Cheque Book",
				"Cheque Layout",
				"Bank Charge Category",
				"Custody Location",
				"Document Naming Settings",
				"Document Naming Rule",
				"Treasury Alert Log",
			],
		)
		self.assertTrue(all(row["onboard"] for row in setup[:-1]))
		page_links = [link for link in links if link["type"] == "Link" and link.get("link_type") == "Page"]
		self.assertEqual([link["link_to"] for link in page_links], [PAGE])

		sidebar = read_json("workspace_sidebar", "treasury.json")
		self.assertGreater(sidebar["modified"], "2026-10-26 12:00:00")
		items = sidebar["items"]
		self.assertEqual((items[1]["link_type"], items[1]["link_to"]), ("Page", PAGE))
		sections = [i["label"] for i in items if i["type"] == "Section Break"]
		expected = ("Cheques", "Transfers & Charges", "Security Cheques", "Bank Facilities", "Reports", "Setup")
		for section in expected:
			self.assertIn(section, sections)

		reports = {link["link_to"] for link in links if link.get("link_type") == "Report"}
		reports |= {i["link_to"] for i in items if i.get("link_type") == "Report"}
		for report in sorted(reports):
			self.assertTrue(frappe.db.exists("Report", report), report)
		self.assertTrue(frappe.db.exists("Page", PAGE))
