// Copyright (c) 2026, Ahmed Abdelmageed and contributors
// For license information, please see license.txt

// Loaded through hooks.py `doctype_js` on the custody app's Custody Request (plain file, no bundle; never loaded
// without custody). A "Treasury" button group opens a new PDC Cheque prepared by a read-only server mapper
// (treasury.integrations.custody.api); the amounts are hints only, the server checks everything again when
// the cheque is saved.
frappe.provide("treasury.custody_request");

// Open the unsaved PDC Cheque the mapper returns (nothing is written until the user saves it).
treasury.custody_request.open_cheque = (frm, direction) =>
	frappe
		.call({
			method: "treasury.integrations.custody.api.make_pdc_cheque",
			args: { custody_request: frm.doc.name, direction },
			freeze: true,
			freeze_message: frappe.utils.escape_html(__("Preparing {0}…", [__("PDC Cheque")])),
		})
		.then((r) => {
			if (!r || !r.message) return;
			frappe.model.with_doctype(r.message.doctype, () => {
				const [doc] = frappe.model.sync(r.message);
				frappe.set_route("Form", doc.doctype, doc.name);
			});
		});

treasury.custody_request.add_buttons = (frm, context) => {
	if (!context.can_cheque) return;
	const group = __("Treasury");
	if (context.pay.amount > 0) {
		frm.add_custom_button(
			__("Pay by Cheque"),
			() => treasury.custody_request.open_cheque(frm, "Outgoing"),
			group
		);
	}
	if (context.receive.amount > 0) {
		frm.add_custom_button(
			__("Receive Cheque"),
			() => treasury.custody_request.open_cheque(frm, "Incoming"),
			group
		);
	}
	// why Treasury cannot pay / receive now although custody would (a draft replenishment, open cheques)
	const blocked = [context.pay, context.receive].find((side) => side.blocked && side.reason);
	if (blocked) {
		frm.dashboard.set_headline(frappe.utils.escape_html(strip_html(blocked.reason)), "orange");
	}
};

frappe.ui.form.on("Custody Request", {
	refresh(frm) {
		if (frm.doc.docstatus !== 1 || frm.doc.is_closed) return;
		if (!frappe.model.can_create("PDC Cheque") && !frappe.model.can_create("Bank Transfer")) return;
		const name = frm.doc.name;
		frappe
			.call({
				method: "treasury.integrations.custody.api.get_payment_context",
				args: { custody_request: name },
			})
			.then((r) => {
				// the form may show another request by now
				if (!r || !r.message || frm.doc.name !== name) return;
				treasury.custody_request.add_buttons(frm, r.message);
			});
	},
});
