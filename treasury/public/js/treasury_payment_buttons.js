// Copyright (c) 2026, Ahmed Abdelmageed and contributors
// For license information, please see license.txt

// Loaded through hooks.py `doctype_js` on the ERPNext / hrms documents Treasury pays or receives (plain file,
// no bundle). A "Treasury" button group opens a new PDC Cheque, Cheque Batch, Bank Transfer or Security Cheque
// prepared by a read-only server mapper; the conditions below are hints only, the server checks everything
// again. The same file is loaded for every doctype: register the handlers once.
frappe.provide("treasury.payment_buttons");

treasury.payment_buttons.open = (method, args) =>
	frappe.call({ method, args, freeze: true }).then((r) => {
		if (!r.message) return;
		frappe.model.with_doctype(r.message.doctype, () => {
			const [doc] = frappe.model.sync(r.message);
			frappe.set_route("Form", doc.doctype, doc.name);
		});
	});

(() => {
	const PARTY_MASTERS = ["Customer", "Supplier", "Employee"];
	const CLOSED_ORDER = ["Closed", "On Hold", "Completed"];
	const CLOSED_HRMS = ["Paid", "Cancelled", "Claimed", "Returned"];
	// button -> the document it creates, the server mapper and extra arguments
	const ACTIONS = {
		cheque: { doctype: "PDC Cheque", method: "treasury.pdc.api.make_pdc_cheque", args: {} },
		transfer: { doctype: "Bank Transfer", method: "treasury.bank_transfers.api.make_bank_transfer", args: {} },
		fawran: {
			doctype: "Bank Transfer",
			method: "treasury.bank_transfers.api.make_bank_transfer",
			args: { method: "Fawran" },
		},
		installments: { doctype: "Cheque Batch", method: "treasury.pdc.api.make_cheque_batch", args: {} },
		security: {
			doctype: "Security Cheque",
			method: "treasury.security_cheques.api.make_security_cheque",
			args: {},
		},
	};
	const labels = (incoming) => ({
		cheque: incoming ? __("Receive by Cheque") : __("Pay by Cheque"),
		transfer: __("Bank Transfer"),
		fawran: __("Fawran"),
		installments: __("Installment Cheques"),
		security: __("Security Cheque"),
	});
	const open_order = (doc) =>
		doc.docstatus === 1 &&
		!CLOSED_ORDER.includes(doc.status) &&
		flt(doc.per_billed) < 100 &&
		flt(doc.advance_paid) < flt(doc.rounded_total || doc.grand_total);
	const open_hrms = (doc) => doc.docstatus === 1 && !CLOSED_HRMS.includes(doc.status);
	const PAYMENTS = ["cheque", "transfer", "fawran", "installments"];

	// doctype -> {show(doc), incoming, buttons}
	const CONFIG = {
		"Purchase Invoice": {
			show: (doc) =>
				doc.docstatus === 1 && !doc.is_return && flt(doc.outstanding_amount) > 0 && !doc.on_hold,
			buttons: PAYMENTS,
		},
		"Sales Invoice": {
			show: (doc) => doc.docstatus === 1 && !doc.is_return && flt(doc.outstanding_amount) > 0,
			incoming: true,
			buttons: PAYMENTS,
		},
		"Purchase Order": { show: open_order, buttons: [...PAYMENTS, "security"] },
		"Sales Order": { show: open_order, incoming: true, buttons: [...PAYMENTS, "security"] },
		"Journal Entry": {
			show: (doc) => doc.docstatus === 1 && (doc.accounts || []).some((row) => row.party),
			buttons: PAYMENTS,
			journal: true,
		},
		"Expense Claim": { show: open_hrms, buttons: ["cheque", "transfer", "fawran"] },
		"Employee Advance": { show: open_hrms, buttons: ["cheque", "transfer", "fawran"] },
		Customer: { show: (doc) => !doc.disabled, incoming: true, buttons: [...PAYMENTS, "security"] },
		Supplier: { show: (doc) => !doc.disabled, buttons: [...PAYMENTS, "security"] },
		Employee: { show: (doc) => doc.status === "Active", buttons: [...PAYMENTS, "security"] },
		Quotation: { show: (doc) => doc.docstatus < 2, buttons: ["security"], tender: true },
		"Supplier Quotation": { show: (doc) => doc.docstatus < 2, buttons: ["security"], tender: true },
	};

	const check_saved = (frm) => {
		if (frm.is_dirty()) {
			frappe.throw(__("You have unsaved changes in this form. Please save before you continue."));
		}
	};

	const base_args = (frm) => {
		const args = { source_doctype: frm.doctype, source_name: frm.doc.name };
		if (PARTY_MASTERS.includes(frm.doctype)) {
			args.company = frappe.defaults.get_user_default("Company");
		}
		return args;
	};

	// Journal Entry: settle one of its open parties (asked when there are several)
	const with_journal_party = (frm, callback) => {
		frappe
			.call({
				method: "treasury.utils.source_documents.get_journal_parties",
				args: { journal_entry: frm.doc.name },
			})
			.then((r) => {
				const parties = r.message || [];
				if (!parties.length) {
					frappe.msgprint(__("This Journal Entry has no open party to settle."));
				} else if (parties.length === 1) {
					callback(parties[0]);
				} else {
					const options = parties.map((p, i) => ({
						value: String(i),
						label: `${__(p.party_type)}: ${p.party_name} (${format_currency(p.outstanding, p.currency)})`,
					}));
					frappe.prompt(
						[{ fieldname: "party", fieldtype: "Select", label: __("Party", null, "Treasury"), options, reqd: 1 }],
						(values) => callback(parties[cint(values.party)]),
						__("Choose the party")
					);
				}
			});
	};

	const run = (frm, config, key) => {
		check_saved(frm);
		const { method } = ACTIONS[key];
		const args = { ...base_args(frm), ...ACTIONS[key].args };
		if (!config.journal) {
			treasury.payment_buttons.open(method, args);
			return;
		}
		with_journal_party(frm, (party) =>
			treasury.payment_buttons.open(method, { ...args, party_type: party.party_type, party: party.party })
		);
	};

	frappe.treasury_payment_button_doctypes = frappe.treasury_payment_button_doctypes || new Set();
	Object.entries(CONFIG).forEach(([doctype, config]) => {
		if (frappe.treasury_payment_button_doctypes.has(doctype)) {
			return;
		}
		frappe.treasury_payment_button_doctypes.add(doctype);
		frappe.ui.form.on(doctype, {
			refresh(frm) {
				if (frm.is_new() || !config.show(frm.doc)) {
					return;
				}
				const names = labels(config.incoming);
				if (config.journal) {
					names.cheque = __("Cheque", null, "Treasury");
				}
				if (config.tender) {
					names.security = __("Security Cheque (Tender Bond)");
				}
				config.buttons.forEach((key) => {
					if (!frappe.model.can_create(ACTIONS[key].doctype)) {
						return;
					}
					frm.add_custom_button(names[key], () => run(frm, config, key), __("Treasury"));
				});
			},
		});
	});
})();
