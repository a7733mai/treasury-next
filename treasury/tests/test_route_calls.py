"""Static check of the app's client scripts: route options are passed through `frappe.route_options`.

Frappe 16 `frappe.set_route(...route, options)` with a spread route and a trailing options object turns the object
into a "[object Object]" segment on Form routes, so the link silently goes nowhere (the Treasury Dashboard "Open
Setup Checklist" button did).
"""

import re
from pathlib import Path

from frappe.tests import UnitTestCase

APP_DIR = Path(__file__).resolve().parents[1]
SPREAD_WITH_OPTIONS = re.compile(r"set_route\(\s*\.\.\.[\w.]+\s*,")


class TestRouteCalls(UnitTestCase):
	def test_no_spread_route_with_trailing_options(self):
		offenders = []
		scripts = [p for p in APP_DIR.rglob("*.js") if "node_modules" not in p.parts]
		self.assertTrue(scripts)
		for path in scripts:
			for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
				if SPREAD_WITH_OPTIONS.search(line):
					offenders.append(f"{path.relative_to(APP_DIR)}:{number}")
		self.assertEqual(offenders, [], "use frappe.route_options, then frappe.set_route(...route)")
