"""Phase 9 fix 1: a saved cheque / transfer of a Custody Request edited through the form or REST (review M1,
security #1), custodian cheques of 1.2 without a source (review minor 3), the request access of a replacement
(security #5) and the Treasury context of a user without a Treasury role (security #3).

Skipped when the custody app is not installed; custody's invariants are checked on tearDown.
"""

import frappe
from frappe import _
from frappe.utils import flt, fmt_money, today

from treasury.integrations.custody import REQUEST, api, documents
from treasury.tests.custody_utils import (
	as_user,
	assert_request,
	build_cheque,
	cheque_from_request,
	make_custodian,
	make_request,
)
from treasury.tests.test_custody_transfers import CustodyTransferTestCase
from treasury.tests.utils import (
	OTHER_ABBR,
	OTHER_COMPANY,
	SUPPLIER,
	assert_gl_entries,
	bank_account_name,
	ensure_company,
	ensure_user,
)

CHEQUE = "PDC Cheque"
TRANSFER = "Bank Transfer"
TREASURY_USER = "trs-edit-user@treasury.test"


class TestCustodyEdits(CustodyTransferTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		# a real other company: the change of company is refused for being frozen, not for a missing link
		ensure_company(OTHER_COMPANY, OTHER_ABBR)

	def at_most(self, req, amount) -> str:
		return _("At most {0} can still be paid on Custody Request {1}.").format(
			fmt_money(amount, currency="EGP"), frappe.bold(req.name)
		)

	def saved(self, doc, **values):
		"""`doc` reloaded, `values` set and saved as a form save would (no lifecycle flag)."""
		doc.reload()
		doc.update(values)
		doc.save()
		return doc

	# ------------------------------------------------------------------ cheque (M1)

	def test_cheque_amount_rechecked(self):
		req = self.request()
		cheque = cheque_from_request(req.name, amount=6000)
		self.assert_refused(lambda: self.saved(cheque, amount=10000.01), self.at_most(req, 10000))
		self.assertEqual(flt(frappe.db.get_value(CHEQUE, cheque.name, "amount")), 6000)

		# within what the request accepts (the cheque's own reservation is excluded)
		self.saved(cheque, amount=10000)
		self.assertEqual(flt(frappe.db.get_value(CHEQUE, cheque.name, "amount")), 10000)
		self.assertEqual(documents.reserved(req.name, "Outgoing"), 10000)

		# another open cheque now takes part of it: the edit cannot take it back
		self.saved(cheque, amount=7000)
		cheque_from_request(req.name, amount=3000)
		self.assert_refused(lambda: self.saved(cheque, amount=7000.01), self.at_most(req, 7000))

		# the lifecycle still works on the edited cheque
		cheque.reload()
		cheque.deliver(handover_date=today())
		self.assert_status(cheque, "Delivered", "Party Posted")
		assert_request(self, req.name, paid_amount=7000, remaining_to_pay=3000)

	def frozen_message(self, doc, *fieldnames) -> str:
		labels = ", ".join(_(doc.meta.get_label(f)) for f in fieldnames)
		return _("{0} cannot be changed on a {1} created from Custody Request {2}.").format(
			labels, _(doc.doctype), frappe.bold(doc.source_name)
		)

	def assert_identity_frozen(self, doc, cases: tuple[tuple[dict, tuple], ...]):
		"""Every change in `cases` ({values}, frozen fieldnames) is refused: through a form save (the controller
		may refuse it before custody, e.g. a bank of another company) and by custody's check itself, with its
		exact message. The stored identity never moves. The expected values are read before any edit: a
		refused save leaves its values on the in-memory document."""
		fieldnames = [f for f in documents.FROZEN_FIELDS if doc.meta.has_field(f)]
		original = frappe.db.get_value(doc.doctype, doc.name, fieldnames, as_dict=True)
		self.assertTrue(original.bank_account and original.company)
		for values, frozen in cases:
			with self.subTest(values=values):
				with self.assertRaises(frappe.ValidationError):
					self.saved(doc, **values)
				self.assertEqual(frappe.db.get_value(doc.doctype, doc.name, fieldnames, as_dict=True), original)

				changed = frappe.get_doc(doc.doctype, doc.name)
				changed.update(values)
				with self.assertRaises(frappe.ValidationError) as cm:
					documents.validate(changed)
				self.assertEqual(str(cm.exception), self.frozen_message(changed, *frozen))
		doc.reload()
		self.assertEqual({f: doc.get(f) for f in fieldnames}, original)
		return original

	def test_cheque_identity_frozen(self):
		req = self.request()
		cheque = cheque_from_request(req.name, amount=1000)
		_employee, other = make_custodian()
		original = self.assert_identity_frozen(
			cheque,
			(
				({"party": other}, ("party",)),
				({"party_type": "Supplier", "party": SUPPLIER}, ("party_type", "party")),
				({"bank_account": bank_account_name("USD")}, ("bank_account",)),
				({"company": OTHER_COMPANY}, ("company",)),
				({"direction": "Incoming"}, ("direction",)),
			),
		)
		self.assertEqual(
			(original.party_type, original.party, original.company, original.direction),
			("Custodian", req.custodian, req.company, "Outgoing"),
		)
		self.assertNotEqual(original.bank_account, bank_account_name("USD"))

	def test_cheque_references_refused_on_edit(self):
		req = self.request()
		cheque = cheque_from_request(req.name, amount=1000)
		cheque.reload()
		cheque.append(
			"references",
			{"reference_doctype": "Purchase Invoice", "reference_name": "PI-X", "allocated_amount": 100},
		)
		self.assert_refused(
			lambda: documents.validate(cheque), _("A payment to a custodian cannot have references.")
		)

	def test_cheque_unchanged_edit_allowed(self):
		"""A form save that changes nothing the request checks (e.g. remarks) does not re-check the request,
		even when the request no longer accepts more (the cheque's own amount is reserved)."""
		req = self.request()
		cheque = cheque_from_request(req.name)
		self.saved(cheque, remarks="Edited remarks")
		self.assertEqual(frappe.db.get_value(CHEQUE, cheque.name, "remarks"), "Edited remarks")

	# ------------------------------------------------------------------ transfer (M1)

	def test_transfer_amount_rechecked(self):
		req = self.request()
		transfer = self.transfer(req, "Normal", submit=False, amount=6000)
		self.assert_refused(lambda: self.saved(transfer, amount=10000.01), self.at_most(req, 10000))
		self.assertEqual(flt(frappe.db.get_value(TRANSFER, transfer.name, "amount")), 6000)
		self.saved(transfer, amount=10000)
		self.assertEqual(documents.reserved(req.name, "Outgoing"), 10000)
		transfer.reload()
		transfer.submit()
		transfer.reload()
		self.assertEqual(transfer.status, "Sent")
		assert_gl_entries(self, "Payment Entry", transfer.payment_entry, self.e7(req))
		assert_request(self, req.name, paid_amount=10000, status="Paid")

	def test_transfer_identity_frozen(self):
		req = self.request()
		transfer = self.transfer(req, "Normal", submit=False, amount=1000)
		_employee, other = make_custodian()
		original = self.assert_identity_frozen(
			transfer,
			(
				({"party": other}, ("party",)),
				({"bank_account": bank_account_name("USD")}, ("bank_account",)),
				({"company": OTHER_COMPANY}, ("company",)),
			),
		)
		self.assertEqual((original.party, original.company), (req.custodian, req.company))
		self.assertNotEqual(original.bank_account, bank_account_name("USD"))

	def test_transfer_method_rechecked(self):
		req = self.request()
		transfer = self.transfer(req, "Normal", submit=False, amount=1000)
		transfer.reload()
		transfer.method = "SWIFT"
		self.assert_refused(
			lambda: documents.validate(transfer), _("SWIFT transfers to custodians are not supported.")
		)

	# ------------------------------------------------------------------ 1.2 custodian cheques (minor 3)

	def test_old_custodian_cheque_without_source(self):
		"""A custodian cheque made before 1.3 (no "Created From") can still be saved and cancelled; only a new
		one, or a change of its party, needs the request."""
		req = self.request()
		cheque = cheque_from_request(req.name, amount=1000)
		frappe.db.set_value(CHEQUE, cheque.name, {"source_doctype": None, "source_name": None})
		self.saved(cheque, remarks="1.2 cheque")
		_employee, other = make_custodian()
		self.assert_refused(
			lambda: self.saved(cheque, party=other),
			_("A cheque or transfer to a custodian must be created from its Custody Request."),
		)
		cheque.reload()
		cheque.cancel_cheque(reason="Upgrade clean-up")
		self.assert_status(cheque, "Cancelled", "Not Posted")

	# ------------------------------------------------------------------ request access (security 5)

	def test_replacement_access(self):
		req = self.request()
		cheque = cheque_from_request(req.name, amount=1000)
		user = ensure_user(TREASURY_USER, ["Treasury User"])
		not_permitted = _("Custody Request {0} does not exist or you may not read it.").format(
			frappe.bold(req.name)
		)
		with as_user(user):
			self.assertFalse(frappe.has_permission(REQUEST, "read", req.name))
			# a replacement (lifecycle) of a cheque the user may write
			replacement = build_cheque(req, amount=1000, replaces=cheque.name)
			replacement.flags.treasury_lifecycle = True
			documents._check_request_access(replacement, req)
			# not a replacement, or not a lifecycle insert: read on the request is required
			plain = build_cheque(req, amount=1000)
			self.assert_refused(
				lambda: documents._check_request_access(plain, req), not_permitted, frappe.PermissionError
			)
			forged = build_cheque(req, amount=1000, replaces=cheque.name)
			self.assert_refused(
				lambda: documents._check_request_access(forged, req), not_permitted, frappe.PermissionError
			)
			# the same error for a request that does not exist
			missing = build_cheque(req, amount=1000, source_name="CR-NO-SUCH-REQUEST")
			self.assert_refused(
				lambda: documents._check_request_access(missing, None),
				_("Custody Request {0} does not exist or you may not read it.").format(
					frappe.bold("CR-NO-SUCH-REQUEST")
				),
				frappe.PermissionError,
			)
			self.assert_refused(
				lambda: api.make_pdc_cheque("CR-NO-SUCH-REQUEST", "Outgoing"),
				"does not exist or you may not read it",
				frappe.PermissionError,
			)
			self.assert_refused(
				lambda: api.make_pdc_cheque(req.name, "Outgoing"),
				"does not exist or you may not read it",
				frappe.PermissionError,
			)

	# ------------------------------------------------------------------ request lock (review minor 4)

	def test_request_locked_before_available(self):
		"""The request row is locked (FOR UPDATE) before what it still accepts is read, on insert and edit."""
		from unittest.mock import patch

		from treasury.integrations.custody import compat

		req = self.request()
		calls = []
		lock, available, lock_custodian = documents.lock_request, documents.available, compat.lock_custodian

		def record_lock(name):
			calls.append(("lock", name))
			return lock(name)

		def record_custodian(custodian):
			calls.append(("custodian", custodian))
			return lock_custodian(custodian)

		def record_available(*args, **kwargs):
			calls.append(("available", args[0].name))
			return available(*args, **kwargs)

		# the custodian is locked before the request (custody's order, review 9b minor 1)
		expected = [("lock", req.name), ("custodian", req.custodian), ("available", req.name)]
		with (
			patch.object(documents, "lock_request", side_effect=record_lock),
			patch.object(compat, "lock_custodian", side_effect=record_custodian),
			patch.object(documents, "available", side_effect=record_available),
		):
			cheque = cheque_from_request(req.name, amount=1000)
			self.assertEqual(calls[-3:], expected)
			calls.clear()
			self.saved(cheque, amount=2000)
			self.assertEqual(calls, expected)

	def test_checks_read_request_under_lock(self):
		"""The checks run on the request read under the lock, not on the copy loaded before it (9b minor 2)."""
		from unittest.mock import patch

		req = self.request()
		cheque = cheque_from_request(req.name, amount=1000, insert=False)
		stale = frappe.get_doc("Custody Request", req.name)
		frappe.db.set_value("Custody Request", req.name, "is_closed", 1)
		with patch.object(documents, "get_request", return_value=stale):
			self.assert_refused(cheque.insert, "is closed")
		self.assertFalse(frappe.get_all(CHEQUE, filters={"source_name": req.name}))

	# ------------------------------------------------------------------ read-only APIs (review minor 5)

	def test_read_only_apis_are_get_only(self):
		for method in (api.get_payment_context, api.make_pdc_cheque, api.make_bank_transfer):
			with self.subTest(method=method.__name__):
				self.assertIn(method, frappe.whitelisted)
				self.assertEqual(frappe.allowed_http_methods_for_whitelisted_func[method], ["GET"])

	# ------------------------------------------------------------------ context without a Treasury role (security 3)

	def test_context_without_treasury_role(self):
		email = f"trs-custody-{frappe.generate_hash(length=6)}@treasury.test"
		custody_user = ensure_user(email, ["Custody User"])
		_employee, custodian = make_custodian(user=custody_user)
		own = make_request(custodian, 1000, cost_center=self.cost_center)
		with as_user(custody_user):
			self.assertTrue(frappe.has_permission(REQUEST, "read", own.name))
			self.assertEqual(api.get_payment_context(own.name), {"can_cheque": False, "can_transfer": False})

	def test_context_custodian_refusal(self):
		"""The context shows custody's refusal of the custodian instead of a button the mapper would refuse."""
		from unittest.mock import patch

		req = self.request()

		def refuse(custodian, winddown):
			frappe.throw("Custodian not usable (test)")

		shown = len(frappe.local.message_log)
		with patch.object(documents.compat, "assert_custodian_usable", side_effect=refuse):
			context = api.get_payment_context(req.name)
		self.assertEqual(context["pay"]["amount"], 0)
		self.assertIn("Custodian not usable (test)", context["pay"]["reason"])
		self.assertFalse(context["pay"]["blocked"])
		# a reason of the button, not a message shown to the user
		self.assertEqual(len(frappe.local.message_log), shown)
