"""Phase 7c: connections of ERPNext / hrms documents to Treasury documents and of the Treasury documents among
themselves (spec 7c §5.2, §5.3, §7): the overrides only add to ERPNext's dashboards, and the counts shown
(`get_open_count`) find the right documents."""

import copy

import frappe
from frappe.desk.notifications import get_open_count
from frappe.modules import load_doctype_module
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, today

from treasury.overrides import dashboards
from treasury.tests.utils import (
	COMPANY,
	CUSTOMER,
	PE_SETTINGS,
	SUPPLIER,
	TreasuryTestMixin,
	ensure_customer,
	ensure_fiscal_year,
	ensure_supplier,
	get_voucher_count,
	make_bank_facility,
	make_bank_transfer,
	make_drawdown,
	make_own_bank,
	make_pdc_cheque,
	make_purchase_invoice,
	make_quotation,
	make_security_cheque,
	unique_cheque_no,
)

TREASURY_ITEMS = {
	"Sales Invoice": ["PDC Cheque", "Cheque Batch", "Bank Transfer"],
	"Purchase Invoice": ["PDC Cheque", "Cheque Batch", "Bank Transfer"],
	"Sales Order": ["PDC Cheque", "Cheque Batch", "Bank Transfer", "Security Cheque"],
	"Purchase Order": ["PDC Cheque", "Cheque Batch", "Bank Transfer", "Security Cheque"],
	"Quotation": ["Security Cheque"],
	"Supplier Quotation": ["Security Cheque"],
	"Payment Entry": [
		"PDC Cheque",
		"Bank Transfer",
		"Letter of Credit",
		"Letter of Guarantee",
		"Cheque Discounting",
	],
	"Journal Entry": [
		"PDC Cheque",
		"Bank Transfer",
		"Letter of Credit",
		"Letter of Guarantee",
		"Cheque Discounting",
		"Facility Drawdown",
		"Facility Repayment",
	],
	"Customer": ["PDC Cheque", "Cheque Batch", "Bank Transfer", "Security Cheque"],
	"Supplier": ["PDC Cheque", "Cheque Batch", "Bank Transfer", "Security Cheque", "Letter of Credit"],
	"Employee": ["PDC Cheque", "Cheque Batch", "Bank Transfer", "Security Cheque"],
	"Bank Account": ["PDC Cheque", "Cheque Batch", "Cheque Book", "Bank Transfer", "Security Cheque"],
	"Expense Claim": ["PDC Cheque", "Cheque Batch", "Bank Transfer"],
	"Employee Advance": ["PDC Cheque", "Cheque Batch", "Bank Transfer"],
}
OWN_DASHBOARDS = (
	"PDC Cheque",
	"Cheque Batch",
	"Bank Transfer",
	"Security Cheque",
	"Bank Facility",
	"Facility Drawdown",
	"Facility Repayment",
	"Letter of Credit",
	"Letter of Guarantee",
	"Cheque Discounting",
)


def original_dashboard(doctype: str) -> frappe._dict:
	"""The dashboard of `doctype` before the override hooks: its `_dashboard.py` and its JSON links."""
	data = frappe._dict()
	try:
		module = load_doctype_module(doctype, suffix="_dashboard")
		if hasattr(module, "get_data"):
			data = frappe._dict(module.get_data())
	except ImportError:
		pass
	frappe.get_meta(doctype).add_doctype_links(data)
	return data


def group(data, label: str) -> dict | None:
	return next((g for g in data.transactions if g.get("label") == label), None)


class TestDashboards(TreasuryTestMixin, IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_fiscal_year(COMPANY, today())

	def setUp(self):
		super().setUp()
		self.use_company_settings(**PE_SETTINGS)
		# get_open_count sets a statement timeout on the connection
		self.addCleanup(frappe.db.set_execution_timeout, 0)

	def counts(self, doctype: str, name: str) -> dict:
		result = get_open_count(doctype, name)["count"]
		found = {row["doctype"]: row["count"] for row in result["external_links_found"]}
		for row in result["internal_links_found"]:
			found[row["doctype"]] = row["count"]
		return found

	# ------------------------------------------------------------------ ERPNext / hrms dashboards

	def test_override_keeps_erpnext_data(self):
		for doctype, items in TREASURY_ITEMS.items():
			if not frappe.db.exists("DocType", doctype):
				continue  # hrms doctypes without hrms
			with self.subTest(doctype=doctype):
				original = original_dashboard(doctype)
				extended = frappe._dict(getattr(dashboards, frappe.scrub(doctype))(copy.deepcopy(original)))

				labels = [g.get("label") for g in original.transactions]
				self.assertEqual([g.get("label") for g in extended.transactions][: len(labels)], labels)
				for before, after in zip(original.transactions, extended.transactions, strict=False):
					self.assertEqual(after["items"][: len(before["items"])], list(before["items"]))
				if doctype == "Employee" and not original.get("fieldname"):
					self.assertEqual(extended.fieldname, "employee")
				else:
					self.assertEqual(extended.get("fieldname"), original.get("fieldname"))
				for key in ("non_standard_fieldnames", "internal_links", "dynamic_links"):
					for name, value in (original.get(key) or {}).items():
						self.assertEqual(extended[key][name], value, f"{key}[{name}]")
				treasury = group(extended, frappe._("Treasury"))
				self.assertIsNotNone(treasury)
				self.assertEqual(treasury["items"], items)

				# the hook is wired
				data = frappe.get_meta(doctype).get_dashboard_data()
				self.assertIsNotNone(group(data, frappe._("Treasury")))

		bank_account = frappe._dict(
			dashboards.bank_account(copy.deepcopy(original_dashboard("Bank Account")))
		)
		self.assertEqual(
			group(bank_account, frappe._("Bank Facilities"))["items"],
			[
				"Bank Facility",
				"Facility Drawdown",
				"Facility Repayment",
				"Letter of Credit",
				"Letter of Guarantee",
				"Cheque Discounting",
			],
		)

	def test_counts_on_invoice(self):
		pi = make_purchase_invoice(20000)
		make_pdc_cheque(amount=5000, invoice=pi)
		cheque_batch = frappe.get_doc(
			{
				"doctype": "Cheque Batch",
				"direction": "Outgoing",
				"company": COMPANY,
				"party_type": "Supplier",
				"party": SUPPLIER,
				"bank_account": self.data.egp.bank_account,
				"issue_date": today(),
				"cheques": [
					{
						"due_date": add_days(today(), 30 * i),
						"amount": 2000,
						"cheque_no": unique_cheque_no(),
						"reference_doctype": "Purchase Invoice",
						"reference_name": pi.name,
						"allocated_amount": 2000,
					}
					for i in (1, 2)
				],
			}
		).insert()
		cheque_batch.submit()  # its cheques exist from the submit on
		make_bank_transfer(
			amount=3000,
			references=[
				{"reference_doctype": "Purchase Invoice", "reference_name": pi.name, "allocated_amount": 3000}
			],
		)

		counts = self.counts("Purchase Invoice", pi.name)
		self.assertEqual((counts["PDC Cheque"], counts["Cheque Batch"], counts["Bank Transfer"]), (3, 1, 1))
		payment_entries = frappe.get_all(
			"Payment Entry Reference",
			filters={"reference_doctype": "Purchase Invoice", "reference_name": pi.name},
			pluck="parent",
			distinct=True,
		)
		self.assertEqual(counts["Payment Entry"], len(payment_entries))

	def test_counts_on_payment_entry(self):
		cheque = make_pdc_cheque(amount=1000)
		cheque.deliver(handover_date=today())
		cheque.reload()
		counts = self.counts("Payment Entry", cheque.party_payment_entry)
		self.assertEqual((counts["PDC Cheque"], counts["Bank Transfer"]), (1, 0))

		transfer = make_bank_transfer(amount=500)
		counts = self.counts("Payment Entry", transfer.payment_entry)
		self.assertEqual((counts["PDC Cheque"], counts["Bank Transfer"]), (0, 1))

	def test_counts_on_parties_and_bank(self):
		name = "_Test Treasury Party Counts"
		customer = ensure_customer(name)
		make_pdc_cheque("Incoming", party=customer)
		make_pdc_cheque("Incoming", party=customer)
		make_security_cheque(party_type="Customer", party=customer)
		counts = self.counts("Customer", customer)
		self.assertEqual(
			(
				counts["PDC Cheque"],
				counts["Security Cheque"],
				counts["Cheque Batch"],
				counts["Bank Transfer"],
			),
			(2, 1, 0, 0),
		)
		# a Supplier with the same name: the party type filter keeps the customer's cheques out
		supplier = ensure_supplier(name)
		counts = self.counts("Supplier", supplier)
		self.assertEqual((counts["PDC Cheque"], counts["Security Cheque"]), (0, 0))
		counts = self.counts("Supplier", SUPPLIER)
		self.assertIn("Letter of Credit", counts)

		# a bank account of its own: one cheque book and one outgoing security cheque
		own = make_own_bank()
		frappe.get_doc(
			{
				"doctype": "Cheque Book",
				"company": COMPANY,
				"bank_account": own.bank_account,
				"start_no": 1,
				"end_no": 50,
			}
		).insert()
		make_security_cheque(direction="Outgoing", party_type="Supplier", bank_account=own.bank_account)
		counts = self.counts("Bank Account", own.bank_account)
		self.assertEqual(
			(counts["Cheque Book"], counts["Security Cheque"], counts["PDC Cheque"], counts["Bank Transfer"]),
			(1, 1, 0, 0),
		)
		self.assertEqual(counts["Bank Facility"], 0)

	def test_counts_on_quotation(self):
		quotation = make_quotation(1000)
		make_security_cheque(
			direction="Outgoing",
			party_type="Customer",
			party=CUSTOMER,
			purpose="Tender",
			reference_doctype="Quotation",
			reference_name=quotation.name,
		)
		self.assertEqual(self.counts("Quotation", quotation.name)["Security Cheque"], 1)

	# ------------------------------------------------------------------ Treasury dashboards

	def test_own_dashboards(self):
		for doctype in OWN_DASHBOARDS:
			with self.subTest(doctype=doctype):
				data = frappe.get_meta(doctype).get_dashboard_data()
				self.assertTrue(data.fieldname)
				items = [item for g in data.transactions for item in g["items"]]
				self.assertTrue(items)
				self.assertEqual(len(items), len(set(items)), "an item listed twice")
				for item in items:
					self.assertTrue(frappe.db.exists("DocType", item), item)

		# a cheque of a batch, converted from a security cheque, posted to the party
		cheque_batch = frappe.get_doc(
			{
				"doctype": "Cheque Batch",
				"direction": "Outgoing",
				"company": COMPANY,
				"party_type": "Supplier",
				"party": SUPPLIER,
				"bank_account": self.data.egp.bank_account,
				"issue_date": today(),
				"cheques": [
					{"due_date": add_days(today(), 30), "amount": 1500, "cheque_no": unique_cheque_no()}
				],
			}
		).insert()
		cheque_batch.submit()
		cheque = frappe.get_doc("PDC Cheque", {"cheque_batch": cheque_batch.name})
		security = make_security_cheque()
		frappe.db.set_value("PDC Cheque", cheque.name, "security_cheque", security.name)
		cheque.deliver(handover_date=today())
		counts = self.counts("PDC Cheque", cheque.name)
		self.assertEqual(counts["Payment Entry"], get_voucher_count("PDC Cheque", cheque.name, docstatus=1))
		self.assertEqual(counts["Payment Entry"], 1)
		self.assertEqual((counts["Cheque Batch"], counts["Security Cheque"]), (1, 1))
		self.assertEqual(self.counts("Cheque Batch", cheque_batch.name)["PDC Cheque"], 1)
		self.assertEqual(self.counts("Security Cheque", security.name)["PDC Cheque"], 1)

		# a facility with one document of each kind
		facility = make_bank_facility()
		drawdown = make_drawdown(facility)
		for doctype, values in (
			("Facility Repayment", {"drawdown": drawdown.name, "facility": facility.name}),
			("Letter of Credit", {"facility": facility.name, "supplier": SUPPLIER}),
			("Letter of Guarantee", {"facility": facility.name}),
			("Cheque Discounting", {"facility": facility.name}),
		):
			# the connection only needs the link fields: saved without the document's own validation
			frappe.get_doc(
				{"doctype": doctype, "name": frappe.generate_hash(length=12), **values}
			).db_insert()
		frappe.get_doc(
			{
				"doctype": "Bank Facility Security Cheque",
				"name": frappe.generate_hash(length=12),
				"parent": facility.name,
				"parenttype": "Bank Facility",
				"parentfield": "security_cheques",
				"idx": 99,
				"security_cheque": security.name,
			}
		).db_insert()
		counts = self.counts("Bank Facility", facility.name)
		self.assertEqual(
			[
				counts[d]
				for d in (
					"Facility Drawdown",
					"Facility Repayment",
					"Letter of Credit",
					"Letter of Guarantee",
					"Cheque Discounting",
					"Security Cheque",
				)
			],
			[1, 1, 1, 1, 1, 1],
		)
		self.assertEqual(self.counts("Security Cheque", security.name)["Bank Facility"], 1)

		counts = self.counts("Facility Drawdown", drawdown.name)
		self.assertEqual(
			counts["Journal Entry"],
			frappe.db.count(
				"Journal Entry",
				{"treasury_source_doctype": "Facility Drawdown", "treasury_source_name": drawdown.name},
			),
		)
		self.assertGreaterEqual(counts["Journal Entry"], 1)
		self.assertEqual((counts["Bank Facility"], counts["Facility Repayment"]), (1, 1))
