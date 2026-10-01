"""Generic "Created From" link of a Treasury document (`source_doctype` / `source_name`, spec 9 §3.3).

The doctypes a document may be created from are registered by the apps through the `treasury_document_sources`
hook (e.g. the custody bridge registers "Custody Request"). What the link means for a given source (amounts,
permissions) is checked by that app's own `doc_events`; this module only keeps the link consistent: both
fields or none, a registered source doctype, and never changed once saved.
"""

import frappe
from frappe import _

HOOK = "treasury_document_sources"
FIELDS = ("source_doctype", "source_name")


def registered() -> tuple[str, ...]:
	"""The source doctypes registered by the installed apps."""
	return tuple(frappe.get_hooks(HOOK) or ())


def validate(doc):
	"""Both of source_doctype / source_name or none; source_doctype registered by an app (hook list);
	unchanged once saved (compared with get_doc_before_save; set_only_once already blocks the form)."""
	source_doctype, source_name = (doc.get(f) or None for f in FIELDS)
	if not doc.is_new():
		before = doc.get_doc_before_save()
		if before is not None:
			if tuple(before.get(f) or None for f in FIELDS) != (source_doctype, source_name):
				frappe.throw(_("Created From cannot be changed."), title=_("Not Allowed"))
			# unchanged: valid when it was set (an app removed since then does not block the document)
			return
	if bool(source_doctype) != bool(source_name):
		frappe.throw(_("Created From needs both the document type and the document."))
	if source_doctype and source_doctype not in registered():
		frappe.throw(_("{0} cannot be created from {1}.").format(_(doc.doctype), _(source_doctype)))
