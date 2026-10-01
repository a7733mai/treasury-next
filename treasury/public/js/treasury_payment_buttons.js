// Copyright (c) 2026, Ahmed Abdelmageed and contributors
// For license information, please see license.txt

// Loaded through hooks.py `doctype_js` on the ERPNext / hrms documents Treasury pays or receives (plain file,
// no bundle). A "Treasury" button group opens a new PDC Cheque, Cheque Batch, Bank Transfer or Security Cheque
// prepared by a read-only server mapper; the conditions below are hints only, the server checks everything
// again and explains every refusal. The same file is loaded for every doctype: register the handlers once.
frappe.provide("treasury.payment_buttons");

// Open the unsaved document a mapper returns (nothing is written until the user saves it).
// options: {target: doctype being prepared, from: {doctype, name} of the clicked form, hint: extra line shown
// under an account refusal, type: "GET" for a mapper whitelisted for GET only}
treasury.payment_buttons.open = (method, args, options = {}) => {
	const error_handlers = {};
	if (options.hint) {
		error_handlers.TreasuryPartyAccountError = (r) => treasury.payment_buttons.show_refusal(r, options.hint);
	}
	return frappe
		.call({
			method,
			args,
			type: options.type || "POST",
			freeze: true,
			freeze_message: options.target
				? frappe.utils.escape_html(__("Preparing {0}…", [__(options.target)]))
				: undefined,
			error_handlers,
		})
		.then((r) => {
			if (!r || !r.message) return;
			frappe.model.with_doctype(r.message.doctype, () => {
				const [doc] = frappe.model.sync(r.message);
				// client only (never saved): the target form explains what is left to type (its intro)
				if (options.from) {
					doc.__treasury_from = options.from;
				}
				frappe.set_route("Form", doc.doctype, doc.name);
			});
		});
};

// The server messages of a refusal, with a muted line naming the next step under the refusal itself.
treasury.payment_buttons.show_refusal = (r, hint) => {
	let messages = [];
	try {
		messages = JSON.parse(r._server_messages || "[]").map((m) => (typeof m === "string" ? JSON.parse(m) : m));
	} catch (e) {
		messages = [];
	}
	const refusal = [...messages].reverse().find((m) => m && m.raise_exception);
	if (refusal) {
		refusal.message = `${refusal.message}<p class="text-muted small" style="margin-top: 8px">${frappe.utils.escape_html(
			hint
		)}</p>`;
	}
	if (messages.length) {
		frappe.hide_msgprint();
		frappe.msgprint(messages);
	}
};

(() => {
	const esc = (value) => frappe.utils.escape_html(value == null ? "" : String(value));
	const PARTY_MASTERS = ["Customer", "Supplier", "Employee"];
	const CLOSED_ORDER = ["Closed", "On Hold", "Completed"];
	const HRMS_DOCTYPES = ["Expense Claim", "Employee Advance"];
	const OUTGOING = "Outgoing";
	const INCOMING = "Incoming";

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

	// the label of each button: the direction on one-sided documents, the kind of bond on security cheques
	const cheque_label = (direction) => {
		if (direction === OUTGOING) return __("Pay by Cheque");
		if (direction === INCOMING) return __("Receive by Cheque");
		return __("Cheque", null, "Treasury");
	};
	const SECURITY_LABELS = {
		tender: () => __("Security Cheque (Tender Bond)"),
		performance: () => __("Security Cheque (Performance Bond)"),
		received: () => __("Security Cheque (Received)"),
		custody: () => __("Security Cheque (Custody)"),
	};
	const labels = (direction, security) => ({
		cheque: cheque_label(direction),
		transfer: __("Bank Transfer"),
		fawran: __("Fawran"),
		installments: __("Installment Cheques"),
		security: SECURITY_LABELS[security || "received"](),
	});

	const open_order = (doc) =>
		doc.docstatus === 1 &&
		!CLOSED_ORDER.includes(doc.status) &&
		flt(doc.per_billed) < 100 &&
		flt(doc.advance_paid) < flt(doc.rounded_total || doc.grand_total);
	const PAYMENTS = ["cheque", "transfer", "fawran", "installments"];
	const HRMS_PAYMENTS = ["cheque", "transfer", "fawran"];

	// doctype -> {show(doc), direction, buttons, security (label kind), journal, tender}
	// (the HRMS rules mirror its own "Create > Payment" buttons: expense_claim.js, employee_advance.js)
	const CONFIG = {
		"Purchase Invoice": {
			show: (doc) =>
				doc.docstatus === 1 && !doc.is_return && flt(doc.outstanding_amount) > 0 && !doc.on_hold,
			direction: OUTGOING,
			buttons: PAYMENTS,
		},
		"Sales Invoice": {
			show: (doc) => doc.docstatus === 1 && !doc.is_return && flt(doc.outstanding_amount) > 0,
			direction: INCOMING,
			buttons: PAYMENTS,
		},
		"Purchase Order": {
			show: open_order,
			direction: OUTGOING,
			buttons: [...PAYMENTS, "security"],
			security: "performance",
		},
		"Sales Order": {
			show: open_order,
			direction: INCOMING,
			buttons: [...PAYMENTS, "security"],
			security: "performance",
		},
		"Journal Entry": {
			show: (doc) => doc.docstatus === 1 && (doc.accounts || []).some((row) => row.party_type && row.party),
			buttons: PAYMENTS,
			journal: true,
		},
		"Expense Claim": {
			show: (doc) => doc.docstatus === 1 && doc.status !== "Paid" && doc.approval_status !== "Rejected",
			direction: OUTGOING,
			buttons: HRMS_PAYMENTS,
		},
		"Employee Advance": {
			show: (doc) =>
				doc.docstatus === 1 &&
				flt(doc.paid_amount) < flt(doc.advance_amount) &&
				!(doc.repay_unclaimed_amount_from_salary && flt(doc.paid_amount)),
			direction: OUTGOING,
			buttons: HRMS_PAYMENTS,
		},
		Supplier: {
			show: (doc) => !doc.disabled,
			direction: OUTGOING,
			buttons: [...PAYMENTS, "security"],
			security: "received",
		},
		Customer: {
			show: (doc) => !doc.disabled,
			direction: INCOMING,
			buttons: [...PAYMENTS, "security"],
			security: "received",
		},
		Employee: {
			show: (doc) => doc.status === "Active",
			direction: OUTGOING,
			buttons: [...PAYMENTS, "security"],
			security: "custody",
		},
		Quotation: {
			show: (doc) => doc.docstatus < 2 && doc.status !== "Lost",
			buttons: ["security"],
			security: "tender",
			tender: true,
		},
		"Supplier Quotation": {
			show: (doc) => doc.docstatus < 2,
			buttons: ["security"],
			security: "tender",
			tender: true,
		},
	};

	const check_saved = (frm) => {
		if (frm.is_dirty()) {
			frappe.throw(__("You have unsaved changes in this form. Please save before you continue."));
		}
	};

	// party masters have no company: the user's default one, else asked once
	const with_company = (frm, title, callback) => {
		if (!PARTY_MASTERS.includes(frm.doctype)) {
			callback(null);
			return;
		}
		const company = frappe.defaults.get_user_default("Company");
		if (company) {
			callback(company);
			return;
		}
		frappe.prompt(
			[{ fieldname: "company", fieldtype: "Link", options: "Company", label: __("Company"), reqd: 1 }],
			(values) => callback(values.company),
			title,
			__("Continue", null, "Treasury")
		);
	};

	const run = (frm, key, label, extra = {}) => {
		check_saved(frm);
		const action = ACTIONS[key];
		with_company(frm, label, (company) => {
			const args = { source_doctype: frm.doctype, source_name: frm.doc.name, ...action.args, ...extra };
			if (company) {
				args.company = company;
			}
			treasury.payment_buttons.open(action.method, args, {
				target: action.doctype,
				from: { doctype: frm.doctype, name: frm.doc.name },
				// an account refusal: HRMS pays these documents itself (Create > Payment)
				hint: HRMS_DOCTYPES.includes(frm.doctype) ? __("Use Create ▸ Payment on this form.") : null,
			});
		});
	};

	// "+" in Connections clicks our button (the same prefilled path), and shows only when the button does.
	// Keyed by the label as shown: Frappe looks the button up by `__(label)`, which keeps a translated label.
	const set_make_buttons = (frm, config, names) => {
		const make = {};
		config.buttons.forEach((key) => {
			const doctype = ACTIONS[key].doctype;
			if (!make[doctype]) {
				make[doctype] = names[key];
			}
		});
		frm.custom_make_buttons = Object.assign(frm.custom_make_buttons || {}, make);
	};

	const add_buttons = (frm, config, names, run_key) => {
		config.buttons.forEach((key) => {
			if (!frappe.model.can_create(ACTIONS[key].doctype)) {
				return;
			}
			frm.add_custom_button(names[key], () => run_key(key, names[key]), __("Treasury"));
		});
	};

	// ------------------------------------------------------------------------------ Journal Entry

	// the open party a click settles: the only supported one, else chosen in a dialog; without a supported
	// party, the first one (the server then gives the precise "uses account" refusal)
	const choose_journal_party = (frm, callback) => {
		const parties = frm._treasury_parties || [];
		const supported = parties.filter((p) => p.supported);
		if (!parties.length) {
			frappe.msgprint({ message: __("This Journal Entry has no open party to settle."), indicator: "blue" });
			return;
		}
		if (supported.length <= 1) {
			callback(supported[0] || parties[0]);
			return;
		}
		const option = (p, i) => ({
			value: String(i),
			label: esc(
				`${__(p.party_type)}: ${p.party_name || p.party} · ${
					p.direction === OUTGOING ? __("Pay", null, "Treasury Voucher") : __("Receive", null, "Treasury Voucher")
				} ${format_currency(p.outstanding, p.currency)}`
			),
		});
		const others = parties.filter((p) => !p.supported).map((p) => p.party_name || p.party);
		const fields = [
			{
				fieldname: "party_row",
				fieldtype: "Select",
				label: __("Party", null, "Treasury"),
				options: supported.map(option),
				default: "0",
				reqd: 1,
			},
		];
		if (others.length) {
			fields.push({
				fieldname: "note",
				fieldtype: "HTML",
				options: `<p class="text-muted small">${esc(
					__("Not listed (settle from ERPNext): {0}", [others.join(", ")])
				)}</p>`,
			});
		}
		const dialog = new frappe.ui.Dialog({
			title: __("Choose the party"),
			fields,
			primary_action_label: __("Continue", null, "Treasury"),
			primary_action(values) {
				dialog.hide();
				callback(supported[cint(values.party_row)]);
			},
		});
		dialog.show();
	};

	const journal_buttons = (frm, config) => {
		// no server call for a reader who may create none of the documents the buttons open
		if (!config.buttons.some((key) => frappe.model.can_create(ACTIONS[key].doctype))) {
			return;
		}
		const name = frm.doc.name;
		frappe.xcall("treasury.utils.source_documents.get_journal_parties", { journal_entry: name }).then((parties) => {
			if (frm.doc.name !== name || frm.is_new()) {
				return;
			}
			frm._treasury_parties = parties || [];
			if (!frm._treasury_parties.length) {
				return; // nothing left to settle: no Treasury group
			}
			const supported = frm._treasury_parties.filter((p) => p.supported);
			const directions = new Set((supported.length ? supported : frm._treasury_parties).map((p) => p.direction));
			const names = labels(directions.size === 1 ? [...directions][0] : null);
			set_make_buttons(frm, config, names);
			add_buttons(frm, config, names, (key, label) => {
				check_saved(frm);
				choose_journal_party(frm, (party) =>
					run(frm, key, label, { party_type: party.party_type, party: party.party })
				);
			});
			// the "+" of Connections follows the buttons that now exist
			frm.dashboard && frm.dashboard.after_refresh && frm.dashboard.after_refresh();
		});
	};

	// ------------------------------------------------------------------------------ tender bond line

	const TENDER_LINES = 3;
	const show_tender_bonds = (frm) => {
		// the tender bonds of the quotation, under its dashboard (read permission only)
		if (frm.is_new() || !frappe.model.can_read("Security Cheque")) {
			return;
		}
		const name = frm.doc.name;
		frappe.db
			.get_list("Security Cheque", {
				filters: { reference_doctype: frm.doctype, reference_name: name, docstatus: ["<", 2] },
				fields: ["name", "amount", "currency", "status", "expiry_date"],
				order_by: "creation desc",
				limit: 100,
			})
			.then((bonds) => {
				if (frm.doc.name !== name || !bonds || !bonds.length) {
					return;
				}
				const lines = bonds.slice(0, TENDER_LINES).map((bond) => {
					const href = frappe.utils.get_form_link("Security Cheque", bond.name);
					const link = `<a href="${esc(href)}"><b><bdi>${esc(bond.name)}</bdi></b></a>`;
					const amount = `<bdi>${esc(format_currency(bond.amount, bond.currency))}</bdi>`;
					const status = esc(__(bond.status, null, "Security Cheque"));
					return bond.expiry_date
						? __("Tender bond: {0} · {1} · {2} · expires {3}", [
								link,
								amount,
								status,
								esc(frappe.datetime.str_to_user(bond.expiry_date)),
						  ])
						: __("Tender bond: {0} · {1} · {2}", [link, amount, status]);
				});
				if (bonds.length > TENDER_LINES) {
					lines.push(esc(__("+{0} more", [bonds.length - TENDER_LINES])));
				}
				// every value is escaped above; the sentences are our own translations
				frm.dashboard.add_comment(lines.join("<br>"), "blue", true);
			});
	};

	// ------------------------------------------------------------------------------ registration

	frappe.treasury_payment_button_doctypes = frappe.treasury_payment_button_doctypes || new Set();
	Object.entries(CONFIG).forEach(([doctype, config]) => {
		if (frappe.treasury_payment_button_doctypes.has(doctype)) {
			return;
		}
		frappe.treasury_payment_button_doctypes.add(doctype);
		frappe.ui.form.on(doctype, {
			refresh(frm) {
				if (config.tender) {
					show_tender_bonds(frm);
				}
				if (frm.is_new()) {
					return;
				}
				if (!config.show(frm.doc)) {
					// no Treasury button: "+" of the Treasury connections shows nowhere either
					set_make_buttons(frm, config, labels(config.direction, config.security));
					return;
				}
				if (config.journal) {
					set_make_buttons(frm, config, labels(null));
					journal_buttons(frm, config);
					return;
				}
				const names = labels(config.direction, config.security);
				set_make_buttons(frm, config, names);
				add_buttons(frm, config, names, (key, label) => run(frm, key, label));
			},
		});
	});
})();
