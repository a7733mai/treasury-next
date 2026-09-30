// Copyright (c) 2026, Ahmed Abdelmageed and contributors
// For license information, please see license.txt

// Business rules live in treasury/pdc/lifecycle.py; the buttons below only mirror them for convenience.
const PDC_SETTLED = ["Cleared", "Bounced", "Returned", "Endorsed", "Discounted", "Replaced", "Cancelled"];
// Accounting details copied to every voucher (PDCCheque ACCOUNTING_FIELDS): locked once the cheque is posted.
const PDC_ACCOUNTING_FIELDS = ["cost_center", "project", "mode_of_payment", "party_bank_account", "contact_person"];
const PDC_PAYEE_ROLES = ["Treasury Manager", "Treasury User", "System Manager"];
// cheque_printing.PRINTABLE_STATUSES
const PDC_PRINTABLE = ["Created", "Delivered"];

// mirrors pdc_cheque.reference_rules: invoices / orders only for a supplier's outgoing or a customer's
// incoming cheque; any cheque may settle a Journal Entry of its party
function pdc_reference_types(direction, party_type) {
	if (direction === "Outgoing" && party_type === "Supplier") {
		return ["Purchase Invoice", "Purchase Order", "Journal Entry"];
	}
	if (direction === "Incoming" && party_type === "Customer") {
		return ["Sales Invoice", "Sales Order", "Journal Entry"];
	}
	return ["Journal Entry"];
}

frappe.ui.form.on("PDC Cheque", {
	setup(frm) {
		// any Party Type (Customer, Supplier, Employee, Shareholder ...)
		frm.set_query("bank_account", () => ({
			filters: { company: frm.doc.company, is_company_account: 1 },
		}));
		frm.set_query("cheque_book", () => ({
			filters: { company: frm.doc.company, bank_account: frm.doc.bank_account, status: "Active" },
		}));
		frm.set_query("reference_doctype", "references", () => ({
			filters: { name: ["in", pdc_reference_types(frm.doc.direction, frm.doc.party_type)] },
		}));
		pdc_setup_accounting_queries(frm);
		frm.set_query("reference_name", "references", (doc, cdt, cdn) => {
			const row = locals[cdt][cdn];
			const filters = { docstatus: 1, company: frm.doc.company };
			const party_field = {
				"Sales Invoice": "customer",
				"Sales Order": "customer",
				"Purchase Invoice": "supplier",
				"Purchase Order": "supplier",
			}[row.reference_doctype];
			if (party_field && frm.doc.party) {
				filters[party_field] = frm.doc.party;
			}
			return { filters };
		});
	},

	onload(frm) {
		if (frm.is_new() && !frm.doc.company) {
			frm.set_value("company", frappe.defaults.get_user_default("Company"));
		}
	},

	refresh(frm) {
		frm.events.set_labels(frm);
		frm.events.set_party_label(frm);
		frm.events.lock_identity_fields(frm);
		frm.events.translate_status_log(frm);
		frm.events.render_vouchers(frm);
		frm.events.set_payment_entry_headline(frm);
		frm.events.set_indicator(frm);
		pdc_set_source_intro(frm);
		pdc_toggle_new_payee(frm);
		// vouchers are created by posting only: no "+" beside them in Connections
		frm.can_make_methods = { "Payment Entry": () => false, "Journal Entry": () => false };
		if (!frm.is_new()) {
			frm.events.add_actions(frm);
			frm.events.add_print_buttons(frm);
		}
	},

	add_print_buttons(frm) {
		const open_print = (doctype, name, format) => {
			const params = new URLSearchParams({ doctype, name, format, _lang: frappe.boot.lang || "" });
			window.open(`/printview?${params.toString()}`);
		};
		frm.add_custom_button(
			__("Print Receipt"),
			() => open_print(frm.doctype, frm.doc.name, "PDC Cheque Receipt"),
			__("Print", null, "PDC Cheque")
		);
		const layout = frm.doc.__onload && frm.doc.__onload.cheque_layout;
		if (frm.doc.direction === "Outgoing" && layout) {
			// the bank's Cheque Layout: the print is registered (counter, status log) before it opens; a cheque
			// in another status cannot be printed (the server refuses it): no button (design 7f §6.1)
			if (PDC_PRINTABLE.includes(frm.doc.operational_status)) {
				frm.add_custom_button(
					__("Print Cheque"),
					() => pdc_print_dialog(frm, layout),
					__("Print", null, "PDC Cheque")
				);
			}
		} else {
			// the PDC Account Mapping of the bank may name its own cheque layout
			const cheque_format = (frm.doc.__onload && frm.doc.__onload.cheque_print_format) || "PDC Cheque Print";
			frm.add_custom_button(
				__("Print Cheque"),
				() => open_print(frm.doctype, frm.doc.name, cheque_format),
				__("Print", null, "PDC Cheque")
			);
		}
		frm.events.add_cheque_template_button(frm, open_print);
	},

	add_cheque_template_button(frm, open_print) {
		// ERPNext's Cheque Print Template (named after the Bank) prints a Payment Entry: offer it for the
		// party Payment Entry of an outgoing cheque.
		const doc = frm.doc;
		if (doc.direction !== "Outgoing" || !doc.party_payment_entry || !doc.bank_account) {
			return;
		}
		// Treasury roles hold no accounting permissions: the lookup needs the user's own read access
		if (!frappe.model.can_read("Bank Account") || !frappe.model.can_read("Payment Entry")) {
			return;
		}
		frappe.db.get_value("Bank Account", doc.bank_account, "bank").then((r) => {
			const bank = r.message && r.message.bank;
			if (!bank) {
				return;
			}
			frappe.db.exists("Cheque Print Template", bank).then((exists) => {
				if (exists) {
					frm.add_custom_button(
						__("Print on Cheque Template"),
						() => open_print("Payment Entry", doc.party_payment_entry, bank),
						__("Print", null, "PDC Cheque")
					);
				}
			});
		});
	},

	direction(frm) {
		frm.events.set_labels(frm);
		// an Employee / Shareholder ... the user picked is kept; only the Customer / Supplier default follows
		if (!frm.doc.party_type || ["Customer", "Supplier"].includes(frm.doc.party_type)) {
			frm.set_value("party_type", frm.doc.direction === "Incoming" ? "Customer" : "Supplier");
		}
		if (frm.doc.direction === "Incoming") {
			frm.set_value("cheque_book", "");
		}
	},

	party_type(frm) {
		frm.set_value("party", "");
		frm.events.set_party_label(frm);
		frm.toggle_display("contact_person", frm.doc.party_type !== "Employee");
		pdc_toggle_new_payee(frm);
	},

	party(frm) {
		// the server defaults them again for the new party on save
		frm.set_value("party_bank_account", "");
		frm.set_value("contact_person", "");
		pdc_toggle_new_payee(frm);
	},

	set_party_label(frm) {
		frm.set_df_property(
			"party",
			"label",
			frm.doc.party_type ? __(frm.doc.party_type) : __("Party", null, "Treasury")
		);
		frm.toggle_display("contact_person", frm.doc.party_type !== "Employee");
	},

	bank_account(frm) {
		frm.set_value("cheque_book", "");
		pdc_set_source_intro(frm);
	},

	cheque_book(frm) {
		frm.events.set_labels(frm);
		pdc_set_source_intro(frm);
	},

	set_labels(frm) {
		const incoming = frm.doc.direction === "Incoming";
		frm.set_df_property("handover_date", "label", incoming ? __("Receipt Date") : __("Handover Date"));
		frm.toggle_reqd("bank_account", !incoming);
		frm.toggle_reqd("cheque_no", !(frm.doc.direction === "Outgoing" && frm.doc.cheque_book));
	},

	lock_identity_fields(frm) {
		// mirrors PDCCheque.validate_locked_fields: fixed once the cheque left the Created status
		const locked = !frm.is_new() && frm.doc.operational_status !== "Created";
		// and, for a cheque of a submitted Cheque Batch, everything the batch voucher lists
		const batch_locked = !frm.is_new() && !!(frm.doc.__onload && frm.doc.__onload.batch_locked);
		const batch_fieldnames = ["drawer_bank", "references"];
		batch_fieldnames.forEach((fieldname) => frm.set_df_property(fieldname, "read_only", batch_locked ? 1 : 0));
		const fieldnames = [
			"direction",
			"company",
			"party_type",
			"party",
			"bank_account",
			"cheque_book",
			"cheque_no",
			"amount",
			"exchange_rate",
			"issue_date",
			"original_due_date",
		];
		// mirrors PDCCheque._validate_printed_fields: what the printed leaf shows (cheque_printing.PRINTED_FIELDS)
		const printed = !frm.is_new() && cint(frm.doc.print_count) > 0;
		const unprinted_fieldnames = ["direction", "exchange_rate", "issue_date"];
		fieldnames.forEach((fieldname) =>
			frm.set_df_property(
				fieldname,
				"read_only",
				locked ||
					(batch_locked && fieldname !== "exchange_rate") ||
					(printed && !unprinted_fieldnames.includes(fieldname))
					? 1
					: 0
			)
		);
		frm.set_df_property("payee_on_cheque", "read_only", printed ? 1 : 0);
		// mirrors the server lock: the vouchers carry the accounting details and dimensions
		const posted = !frm.is_new() && frm.doc.accounting_status !== "Not Posted";
		[...PDC_ACCOUNTING_FIELDS, ...pdc_dimension_fields(frm)].forEach((fieldname) =>
			frm.set_df_property(fieldname, "read_only", posted ? 1 : 0)
		);
	},

	translate_status_log(frm) {
		// statuses are stored in English in the status log (Data fields): translate them on display
		["from_operational", "to_operational", "from_accounting", "to_accounting"].forEach((fieldname) => {
			for (const name of [null, frm.doc.name]) {
				const df = frappe.meta.get_docfield("PDC Status Log", fieldname, name);
				if (df) {
					df.formatter = (value) => frappe.utils.escape_html(value ? __(value) : "");
				}
			}
		});
		frm.refresh_field("status_log");
	},

	set_indicator(frm) {
		if (frm.is_new()) {
			return;
		}
		const overdue =
			!PDC_SETTLED.includes(frm.doc.operational_status) &&
			frm.doc.effective_due_date &&
			frm.doc.effective_due_date < frappe.datetime.get_today();
		if (overdue) {
			frm.page.set_indicator(__("Overdue", null, "Treasury"), "red");
		} else {
			frm.page.set_indicator(__(frm.doc.operational_status), "blue");
		}
	},

	render_vouchers(frm) {
		// server-rendered, escaped summary (treasury.utils.voucher_summary)
		frm.get_field("vouchers_html").$wrapper.html((frm.doc.__onload && frm.doc.__onload.voucher_summary) || "");
	},

	set_payment_entry_headline(frm) {
		// the paper-filing key: the submitted Payment Entries of the current cycle, at a glance
		const entries = (!frm.is_new() && frm.doc.__onload && frm.doc.__onload.payment_entries) || [];
		if (!entries.length) {
			return;
		}
		const esc = frappe.utils.escape_html;
		const items = entries
			.map((pe) => {
				const route = `/app/payment-entry/${encodeURIComponent(pe.name)}`;
				let text = `<a href="${route}"><b><bdi>${esc(pe.name)}</bdi></b></a> ${esc(pe.event_label || "")} ${esc(
					frappe.datetime.str_to_user(pe.posting_date)
				)}`;
				if (pe.clearance_date) {
					text += ` • ${esc(__("Cleared {0}", [frappe.datetime.str_to_user(pe.clearance_date)]))}`;
				}
				return text;
			})
			.join(" · ");
		const $headline = frm.dashboard.set_headline(
			// `items` is escaped above; the message itself is our own translation
			`<div>${__("Payment Entries: {0}", [items])}
				&nbsp; <a class="treasury-copy-pe">⧉ ${esc(__("Copy", null, "Treasury Voucher"))}</a></div>`,
			"blue"
		);
		$headline &&
			$headline.find(".treasury-copy-pe").on("click", () => {
				frappe.utils.copy_to_clipboard(entries.map((pe) => pe.name).join(", "));
			});
	},

	add_actions(frm) {
		if (frm.is_dirty()) {
			return;
		}
		const doc = frm.doc;
		const status = doc.operational_status;
		const outgoing = doc.direction === "Outgoing";
		const is_manager = frappe.user.has_role("Treasury Manager");
		const can_postpone = frappe.user.has_role(["Treasury Manager", "Cheque Approver"]);
		const date = (fieldname, label) => ({
			fieldname,
			label,
			fieldtype: "Date",
			reqd: 1,
			default: frappe.datetime.get_today(),
		});
		const reason = { fieldname: "reason", label: __("Reason"), fieldtype: "Small Text", reqd: 1 };
		const remarks = { fieldname: "remarks", label: __("Remarks", null, "Treasury"), fieldtype: "Small Text" };

		if (status === "Created") {
			if (outgoing) {
				pdc_action(frm, "deliver", __("Deliver"), [date("handover_date", __("Handover Date")), remarks]);
			} else {
				pdc_action(frm, "receive", __("Receive", null, "PDC Cheque"), [date("receipt_date", __("Receipt Date")), remarks]);
			}
		}

		if (
			doc.accounting_status === "Not Posted" &&
			doc.bank_account &&
			["Created", "Delivered", "Received", "Deposited"].includes(status)
		) {
			// the server rejects a date before the handover / receipt (dates are "YYYY-MM-DD" strings)
			const handover = doc.handover_date || "";
			const default_date = handover > (doc.effective_due_date || "") ? handover : doc.effective_due_date;
			pdc_action(frm, "post_party", __("Post to Party Now"), [
				{ ...date("posting_date", __("Posting Date", null, "Treasury")), default: default_date },
			]);
		}

		if (!outgoing && status === "Received") {
			pdc_action(frm, "deposit", __("Deposit", null, "PDC Cheque"), [
				date("deposit_date", __("Deposit Date")),
				{
					fieldname: "bank_account",
					label: __("Bank Account", null, "Treasury"),
					fieldtype: "Link",
					options: "Bank Account",
					default: doc.bank_account,
					reqd: 1,
					get_query: () => ({ filters: { company: doc.company, is_company_account: 1 } }),
				},
				remarks,
			]);
		}

		const clearable = outgoing ? ["Delivered"] : ["Received", "Deposited"];
		if (clearable.includes(status)) {
			pdc_action(frm, "clear", __("Clear", null, "PDC Cheque"), [
				date("clearance_date", __("Actual Clearance Date")),
				{
					// an incoming cheque received without a bank: the bank it was cleared into
					fieldname: "bank_account",
					label: __("Bank Account", null, "Treasury"),
					fieldtype: "Link",
					options: "Bank Account",
					reqd: doc.bank_account ? 0 : 1,
					hidden: doc.bank_account ? 1 : 0,
					get_query: () => ({ filters: { company: doc.company, is_company_account: 1 } }),
				},
				{ fieldname: "bank_transaction_date", label: __("Bank Transaction Date"), fieldtype: "Date" },
				{ fieldname: "bank_charges", label: __("Bank Charges"), fieldtype: "Currency" },
				{
					fieldname: "exchange_rate",
					label: __("Exchange Rate at Clearance"),
					fieldtype: "Float",
					precision: 9,
					hidden: doc.exchange_rate === 1 ? 1 : 0,
				},
				remarks,
			]);
			if (is_manager) {
				pdc_action(frm, "bounce", __("Bounce"), [
					date("bounce_date", __("Bounce Date")),
					reason,
					{
						fieldname: "charges",
						label: __("Bounce Charges"),
						fieldtype: "Currency",
						hidden: outgoing ? 1 : 0,
					},
				]);
			}
		}

		if (status === "Discounted" && is_manager) {
			// the bank collects a discounted cheque: paid at maturity, or bounced (recourse on the company)
			pdc_action(frm, "settle_discounted", __("Paid at Maturity (Discounted)"), [
				date("settlement_date", __("Settlement Date")),
				remarks,
			]);
			pdc_action(frm, "bounce", __("Bounce"), [date("bounce_date", __("Bounce Date")), reason]);
		}

		if (status === "Cleared" && !["Bank Posted", "Reversed"].includes(doc.accounting_status)) {
			pdc_action(frm, "post_bank", __("Post to Bank"), [
				{
					...date("posting_date", __("Posting Date", null, "Treasury")),
					default: doc.bank_transaction_date || doc.actual_clearance_date,
				},
				{ fieldname: "bank_charges", label: __("Bank Charges"), fieldtype: "Currency", default: doc.bank_charges },
			]);
		}

		if (!PDC_SETTLED.includes(status) && can_postpone) {
			pdc_action(frm, "postpone", __("Postpone"), [
				// propose one month after the current due date, not today
				{
					...date("new_due_date", __("New Due Date")),
					default: frappe.datetime.add_months(doc.effective_due_date, 1),
				},
				reason,
				{ fieldname: "contact_person", label: __("Contact Person"), fieldtype: "Data" },
			]);
		}

		if (status === "Bounced" && is_manager) {
			pdc_action(frm, "represent", __("Re-present"), [
				date("represent_date", __("Date", null, "Treasury")),
				{ fieldname: "new_due_date", label: __("New Due Date"), fieldtype: "Date" },
				remarks,
			]);
		}

		if (!outgoing && ["Received", "Bounced"].includes(status)) {
			pdc_action(frm, "return_to_party", __("Return to Party"), [date("return_date", __("Return Date")), reason]);
		}

		if (["Created", "Delivered", "Received", "Bounced"].includes(status) && is_manager) {
			frm.add_custom_button(__("Replace"), () => pdc_replace(frm), __("Actions"));
		}

		const cancellable =
			(status === "Created" && doc.accounting_status === "Not Posted") ||
			(["Bounced", "Returned"].includes(status) && ["Not Posted", "Reversed"].includes(doc.accounting_status));
		if (cancellable && is_manager) {
			pdc_action(frm, "cancel_cheque", __("Cancel Cheque"), [reason]);
		}
	},
});

function pdc_setup_accounting_queries(frm) {
	frm.set_query("cost_center", () => ({ filters: { company: frm.doc.company, is_group: 0 } }));
	frm.set_query("project", () => ({ filters: { company: frm.doc.company } }));
	frm.set_query("mode_of_payment", () => ({ filters: { enabled: 1 } }));
	frm.set_query("party_bank_account", () => ({
		filters: { party_type: frm.doc.party_type, party: frm.doc.party, is_company_account: 0 },
	}));
	frm.set_query("contact_person", () => ({
		query: "frappe.contacts.doctype.contact.contact.contact_query",
		filters: { link_doctype: frm.doc.party_type, link_name: frm.doc.party },
	}));
	// ERPNext's own helper: dimension queries by company and the company's default dimensions
	if (window.erpnext && erpnext.accounts && erpnext.accounts.dimensions) {
		erpnext.accounts.dimensions.setup_dimension_filters(frm, frm.doctype);
	}
}

function pdc_dimension_fields(frm) {
	// the accounting dimension fields ERPNext adds between `accounting_dimensions_section` and the next section
	const fieldnames = [];
	let inside = false;
	for (const df of frm.meta.fields || []) {
		if (df.fieldname === "accounting_dimensions_section") {
			inside = true;
		} else if (inside && df.fieldtype === "Section Break") {
			break;
		} else if (inside && df.fieldtype === "Link") {
			fieldnames.push(df.fieldname);
		}
	}
	return fieldnames;
}

const PDC_PARTY_MASTERS = ["Customer", "Supplier", "Employee"];

function pdc_source_link(doctype, name) {
	// "<Purchase Invoice> <ACC-PINV-...>" as a link to the document (escaped)
	const esc = frappe.utils.escape_html;
	const route = `/app/${frappe.router.slug(doctype)}/${encodeURIComponent(name)}`;
	return `<a href="${route}">${esc(__(doctype))} <b><bdi>${esc(name)}</bdi></b></a>`;
}

function pdc_set_source_intro(frm) {
	// what is left to type on a new cheque opened from a document or a party (Treasury buttons of the ERPNext
	// forms, public/js/treasury_payment_buttons.js; `__treasury_from` is client only and never saved)
	const doc = frm.doc;
	const ref = (doc.references || [])[0];
	const from =
		doc.__treasury_from ||
		(ref && ref.reference_doctype && ref.reference_name
			? { doctype: ref.reference_doctype, name: ref.reference_name }
			: null);
	if (!frm.is_new() || !from || !from.doctype || !from.name) {
		frm.set_intro();
		return;
	}
	const esc = frappe.utils.escape_html;
	const link = pdc_source_link(from.doctype, from.name);
	if (PDC_PARTY_MASTERS.includes(from.doctype) && !ref) {
		frm.set_intro(
			__("For {0}: type the amount and the cheque number, then save. The cheque is an advance (not allocated).", [
				link,
			]),
			"blue"
		);
		return;
	}
	const amount = `<bdi>${esc(
		flt(doc.amount) ? format_currency(doc.amount, doc.currency) : format_currency(ref ? ref.allocated_amount : 0)
	)}</bdi>`;
	// the values are escaped above; the sentences are our own translations
	if (doc.direction === "Incoming") {
		frm.set_intro(
			__("From {0}: {1}. Type the cheque number (and the due date if it differs), then save.", [link, amount]),
			"blue"
		);
	} else if (!doc.bank_account) {
		frm.set_intro(
			`${__("From {0}: {1}. Choose the bank account and type the cheque number.", [link, amount])} ${esc(
				__('Tip: set "Default Bank (Outgoing Cheques)" in Treasury Company Settings.')
			)}`,
			"orange"
		);
	} else if (doc.cheque_book) {
		frm.set_intro(
			__("From {0}: {1}. Check the due date and save; the cheque number is taken from cheque book {2}.", [
				link,
				amount,
				`<b><bdi>${esc(doc.cheque_book)}</bdi></b>`,
			]),
			"blue"
		);
	} else {
		frm.set_intro(__("From {0}: {1}. Type the cheque number and save.", [link, amount]), "blue");
	}
}

function pdc_toggle_new_payee(frm) {
	// "+ New Payee" under the party field: a Supplier created from a name only (treasury.utils.party.create_payee)
	const field = frm.fields_dict.party;
	if (!field) {
		return;
	}
	const help = field.$wrapper.find(".help-box");
	const editable =
		frm.doc.docstatus === 0 &&
		(frm.is_new() || frm.doc.operational_status === "Created") &&
		!(frm.doc.__onload && frm.doc.__onload.batch_locked);
	const show =
		editable &&
		!frm.doc.party &&
		(!frm.doc.party_type || frm.doc.party_type === "Supplier") &&
		frappe.user.has_role(PDC_PAYEE_ROLES);
	if (!show) {
		help.html("").addClass("hide");
		return;
	}
	help.html(`<a class="treasury-new-payee">+ ${frappe.utils.escape_html(__("New Payee"))}</a>`).removeClass("hide");
	help.find(".treasury-new-payee").on("click", () => pdc_new_payee(frm));
}

function pdc_new_payee(frm) {
	const typed = frm.doc.party ? "" : (frm.fields_dict.party.$input && frm.fields_dict.party.$input.val()) || "";
	const dialog = new frappe.ui.Dialog({
		title: __("New Payee"),
		size: "small",
		fields: [
			{
				fieldname: "supplier_name",
				label: __("Payee Name"),
				fieldtype: "Data",
				length: 140,
				reqd: 1,
				default: typed.trim(),
			},
			{
				fieldname: "help",
				fieldtype: "HTML",
				options: `<p class="text-muted small">${frappe.utils.escape_html(
					__(
						"A Supplier is created in the group set in Treasury Settings (Payees). Only the name is needed; the accountant can complete it later."
					)
				)}</p>`,
			},
		],
		primary_action_label: __("Create and Select"),
		primary_action(values) {
			frappe
				.xcall("treasury.utils.party.create_payee", { supplier_name: values.supplier_name })
				.then((r) => {
					dialog.hide();
					// the party_type handler clears the party: set the party once it ran
					const set_type =
						frm.doc.party_type === r.party_type ? Promise.resolve() : frm.set_value("party_type", r.party_type);
					return Promise.resolve(set_type)
						.then(() => frm.set_value("party", r.party))
						.then(() => {
							frappe.show_alert({
								message: r.created
									? __("Payee {0} created.", [frappe.utils.escape_html(r.party_name)])
									: __("Existing supplier {0} selected.", [frappe.utils.escape_html(r.party_name)]),
								indicator: r.created ? "green" : "blue",
							});
						});
				});
		},
	});
	dialog.show();
}

function pdc_action(frm, method, label, fields) {
	frm.add_custom_button(
		label,
		() => {
			frappe.prompt(
				fields,
				(values) => {
					if (method === "deliver") {
						pdc_call_with_override(frm, method, values);
						return;
					}
					frm.call({ doc: frm.doc, method, args: values, freeze: true }).then(() => frm.reload_doc());
				},
				label,
				label
			);
		},
		__("Actions")
	);
}

// Low balance guard (Block mode): the shared dialog (below) offers a Treasury Manager "Override and Continue",
// which calls again with the override. The server is authoritative: it checks the role and logs the override.
function pdc_call_with_override(frm, method, args, override) {
	treasury.low_balance.retry = override ? null : () => pdc_call_with_override(frm, method, args, true);
	frm.call({
		doc: frm.doc,
		method,
		args: Object.assign({}, args, override ? { allow_low_balance: 1 } : {}),
		freeze: true,
		always: () => (treasury.low_balance.retry = null),
	}).then(() => frm.reload_doc());
}

// ------------------------------------------------------------------------------------ low balance guard
// Design 7e §4.3: one dialog for the LowBalanceError of every guarded action and submit form, registered once by
// whichever guarded form loads first (the same code is in each guarded form script: there are no bundles).
// Frappe shows no message of its own when a handler exists, so the dialog renders the server message (built
// and escaped by treasury.balance_alerts.guard). A manager gets one red button: the action helpers set
// `treasury.low_balance.retry` just before their call; a draft with `allow_low_balance` is submitted again.
frappe.provide("treasury.low_balance");
if (!treasury.low_balance.show) {
	treasury.low_balance.show = function (r) {
		const retry = treasury.low_balance.retry;
		treasury.low_balance.retry = null;
		let html = "";
		try {
			html = JSON.parse(r._server_messages || "[]")
				.map((m) => JSON.parse(m).message || "")
				.join("");
		} catch (e) {
			html = "";
		}
		const frm = window.cur_frm;
		const can_override = frappe.user.has_role(["Treasury Manager", "System Manager"]);
		const submit_form = !retry && frm && frm.doc && frm.doc.docstatus === 0 && frm.fields_dict.allow_low_balance;
		const dialog = new frappe.ui.Dialog({
			title: __("Low Projected Balance"),
			indicator: "red",
			fields: [{ fieldname: "message", fieldtype: "HTML", options: html }],
		});
		if (can_override && (retry || submit_form)) {
			dialog.set_primary_action(retry ? __("Override and Continue") : __("Override and Submit"), () => {
				dialog.hide();
				// the red button is the confirmation: no second "Permanently Submit?" prompt
				retry ? retry() : frm.set_value("allow_low_balance", 1).then(() => frm.save("Submit"));
			});
			dialog.get_primary_btn().removeClass("btn-primary").addClass("btn-danger");
			dialog.set_secondary_action(() => dialog.hide());
			dialog.set_secondary_action_label(__("Cancel"));
		} else {
			dialog.set_primary_action(__("Close"), () => dialog.hide());
		}
		dialog.show();
	};
	frappe.request.on_error("LowBalanceError", (r) => treasury.low_balance.show(r));
}

frappe.ui.form.on("PDC Cheque", {
	refresh(frm) {
		pdc_low_balance_log(frm);
		pdc_print_indicator(frm);
		pdc_payee_placeholder(frm);
	},

	party_name(frm) {
		pdc_payee_placeholder(frm);
	},
});

// Events shown as a pill in the status log: the low-balance override (7e) and the prints (7f §5.2)
const PDC_EVENT_PILLS = { "Low Balance Override": "orange", Printed: "blue", Reprinted: "orange" };

function pdc_low_balance_log(frm) {
	// the override and print events are pills in the status log; the latest override a line on the dashboard
	const esc = frappe.utils.escape_html;
	const event = "Low Balance Override";
	for (const name of [null, frm.doc.name]) {
		const df = frappe.meta.get_docfield("PDC Status Log", "event", name);
		if (df && !(df.formatter && df.formatter.treasury_event_pills)) {
			const base = df.formatter;
			df.formatter = (value, ...rest) => {
				if (PDC_EVENT_PILLS[value]) {
					return `<span class="indicator-pill ${PDC_EVENT_PILLS[value]}">${esc(
						__(value, null, "PDC Status Log")
					)}</span>`;
				}
				return base ? base(value, ...rest) : esc(value ? __(value, null, "PDC Status Log") : "");
			};
			df.formatter.treasury_event_pills = true;
		}
	}
	frm.refresh_field("status_log");
	const row = (frm.doc.status_log || []).filter((r) => r.event === event).pop();
	if (row) {
		frm.dashboard.set_headline(
			`<div>${esc(
				__("Submitted with a low-balance override by {0} on {1}.", [
					frappe.user.full_name(row.user) || row.user,
					frappe.datetime.str_to_user(row.datetime),
				])
			)}</div>`,
			"orange"
		);
	}
}

// ------------------------------------------------------------------------------------ cheque printing
// Design 7f §5–§6. lifecycle.record_print registers the print (counter, "Printed" / "Reprinted" in the status log);
// a printed cheque is printed again only with a reason and, when the company says so, by a Treasury Manager. The
// server is authoritative: the dialog only mirrors these rules. Printing takes one click plus Enter: the language
// is defaulted (the last one used for the bank account, else the layout's).
const PDC_LANGUAGES = { Arabic: "ar", English: "en" };

function pdc_print_indicator(frm) {
	// design 7f P20: printed or not, at a glance (outgoing cheques printed on a Cheque Layout)
	const doc = frm.doc;
	if (frm.is_new() || doc.direction !== "Outgoing" || !(doc.__onload && doc.__onload.cheque_layout)) {
		return;
	}
	const count = cint(doc.print_count);
	const by = frappe.user.full_name(doc.last_printed_by) || doc.last_printed_by || "";
	const on = doc.last_printed_on ? frappe.datetime.str_to_user(doc.last_printed_on.split(" ")[0]) : "";
	const esc = frappe.utils.escape_html;
	if (!count) {
		if (PDC_PRINTABLE.includes(doc.operational_status)) {
			frm.dashboard.add_indicator(esc(__("Not printed")), "orange");
		}
	} else if (count === 1) {
		frm.dashboard.add_indicator(esc(__("Printed · {0} by {1}", [on, by])), "blue");
	} else {
		frm.dashboard.add_indicator(esc(__("Printed {0}× · last {1} by {2}", [count, on, by])), "orange");
	}
}

function pdc_payee_placeholder(frm) {
	// an empty "Payee Name on Cheque" visibly means "the party name"
	const field = frm.fields_dict.payee_on_cheque;
	field && field.$input && field.$input.attr("placeholder", frm.doc.party_name || frm.doc.party || "");
}

function pdc_last_language(frm, layout) {
	// [P17] the last language used for this bank account, else the layout's default
	try {
		const value = window.localStorage.getItem(`treasury_cheque_lang:${frm.doc.bank_account}`);
		if (PDC_LANGUAGES[value]) {
			return value;
		}
	} catch (e) {
		// storage blocked: the layout's default
	}
	return layout.default_language || "Arabic";
}

function pdc_remember_language(frm, language) {
	try {
		window.localStorage.setItem(`treasury_cheque_lang:${frm.doc.bank_account}`, language);
	} catch (e) {
		// storage blocked: nothing remembered
	}
}

function pdc_server_text(r) {
	// the server messages as plain text (DOMParser runs no script); escaped again where shown
	try {
		return JSON.parse(r._server_messages || "[]")
			.map((m) => new DOMParser().parseFromString(JSON.parse(m).message || "", "text/html").body.textContent)
			.join(" ");
	} catch (e) {
		return "";
	}
}

function pdc_print_dialog(frm, layout, notice) {
	const doc = frm.doc;
	const esc = frappe.utils.escape_html;
	const reprint = cint(doc.print_count) > 0;
	// [P15] a user who may not reprint gets no dead end: the reason and the button are hidden
	const blocked = reprint && layout.reprint_requires_manager && !frappe.user.has_role("Treasury Manager");
	const row = (label, value) =>
		`<tr><th class="text-muted" style="width:25%;font-weight:normal;white-space:nowrap">${esc(
			__(label, null, "Cheque Print Dialog")
		)}</th><td>${value}</td></tr>`;
	const payee = esc(doc.payee_on_cheque || doc.party_name || doc.party || "");
	const leaf = doc.cheque_book
		? __("Insert leaf {0} of book {1} face up.", [doc.cheque_no, doc.cheque_book])
		: __("Insert leaf {0} face up.", [doc.cheque_no]);
	const summary = `<table class="table table-sm" style="margin-bottom:8px">
		${row("Payee", `<b class="pdc-print-payee">${payee}</b> &nbsp; <a class="pdc-print-edit small">${esc(
			__("Edit", null, "Cheque Print Dialog")
		)} ↗</a>`)}
		${row("Date", `<bdi class="pdc-print-date">${esc(frappe.datetime.str_to_user(doc.effective_due_date) || "")}</bdi>`)}
		${row(
			"Amount",
			`<b><bdi class="pdc-print-figures">${esc(format_currency(doc.amount, doc.currency))}</bdi></b> <span class="text-muted">${esc(
				doc.currency || ""
			)}</span>`
		)}
		${row("In words", `<div class="pdc-print-words"></div>`)}
		${row("Leaf", `<span class="text-muted">${esc(leaf)}</span>`)}
	</table>`;
	const reprint_block = reprint
		? `<div class="alert alert-warning small" style="margin-bottom:8px">${esc(
				__("Printed {0} times, last by {1} on {2}.", [
					doc.print_count,
					frappe.user.full_name(doc.last_printed_by) || doc.last_printed_by || "",
					frappe.datetime.str_to_user(doc.last_printed_on) || "",
				])
		  )}<br>${esc(
				__(
					"If the printed leaf is spoilt, use Actions → Replace to move the cheque to a new leaf instead of reprinting."
				)
		  )}</div>${
				blocked
					? `<p class="text-danger small">${esc(__("Only a Treasury Manager may reprint a cheque."))}</p>`
					: ""
		  }`
		: "";
	let dialog = null;
	const fields = [
		...(notice
			? [{ fieldname: "notice", fieldtype: "HTML", options: `<div class="alert alert-warning small">${esc(notice)}</div>` }]
			: []),
		{ fieldname: "summary", fieldtype: "HTML", options: summary },
		{
			fieldname: "language",
			fieldtype: "Select",
			label: __("Language", null, "Cheque Layout"),
			options: Object.keys(PDC_LANGUAGES).map((value) => ({ value, label: __(value, null, "Cheque Layout") })),
			default: pdc_last_language(frm, layout),
			reqd: 1,
			// the dialog may set its default before it exists
			onchange: () => dialog && pdc_print_preview_text(frm, dialog),
		},
		...(reprint ? [{ fieldname: "reprint_block", fieldtype: "HTML", options: reprint_block }] : []),
		...(reprint && !blocked ? [{ fieldname: "reason", fieldtype: "Small Text", label: __("Reason"), reqd: 1 }] : []),
	];
	dialog = new frappe.ui.Dialog({
		title: __("Print Cheque {0}", [esc(doc.cheque_no || doc.name)]),
		fields,
	});
	if (blocked) {
		dialog.set_primary_action(__("Close"), () => dialog.hide());
	} else {
		dialog.set_primary_action(reprint ? __("Reprint") : __("Print", null, "PDC Cheque"), (values) =>
			pdc_register_print(frm, layout, dialog, values, reprint)
		);
		// the colour is the confirmation of a reprint: no second confirm
		reprint && dialog.get_primary_btn().removeClass("btn-primary").addClass("btn-danger");
	}
	dialog.fields_dict.summary.$wrapper.find(".pdc-print-edit").on("click", () => {
		dialog.hide();
		frm.scroll_to_field("payee_on_cheque");
	});
	dialog.show();
	pdc_print_preview_text(frm, dialog);
}

function pdc_print_preview_text(frm, dialog) {
	// [P14] the treasurer's last check of the words ("فقط … لا غير") before a leaf is used; registers nothing
	const $summary = dialog.fields_dict.summary.$wrapper;
	frappe
		.call({
			method: "treasury.pdc.cheque_printing.get_cheque_text",
			args: { name: frm.doc.name, language: dialog.get_value("language") },
			type: "GET",
		})
		.then((r) => {
			const text = r.message;
			if (!text) {
				return;
			}
			// every value is escaped by the server
			$summary.find(".pdc-print-payee").html(text.payee);
			$summary.find(".pdc-print-date").html(text.date);
			$summary.find(".pdc-print-figures").html(text.figures);
			$summary
				.find(".pdc-print-words")
				.attr("dir", text.dir)
				.css("text-align", text.dir === "rtl" ? "right" : "left")
				.html(`<div>${text.words1}</div>${text.words2 ? `<div>${text.words2}</div>` : ""}`);
		});
}

function pdc_register_print(frm, layout, dialog, values, reprint) {
	// opened inside the click handler (a user gesture), so the browser does not block it (design 7f §6.4)
	const win = treasury.print.open_window();
	dialog.hide();
	const request = frm.call({
		doc: frm.doc,
		method: "print_cheque",
		args: { language: values.language, reprint: reprint ? 1 : 0, reason: values.reason || "" },
		freeze: true,
		freeze_message: __("Registering the print…"),
		error_handlers: {
			// printed meanwhile by someone else: ask again, as a reprint, with the server's message on top
			ChequeAlreadyPrintedError: (r) => {
				win.close();
				const notice = pdc_server_text(r);
				frm.reload_doc().then(() =>
					pdc_print_dialog(frm, (frm.doc.__onload && frm.doc.__onload.cheque_layout) || layout, notice)
				);
			},
		},
	});
	request.fail(() => win.close());
	request.then((r) => {
		const result = (r && r.message) || {};
		const params = new URLSearchParams({
			doctype: frm.doctype,
			name: frm.doc.name,
			format: result.print_format || "Treasury Cheque",
			no_letterhead: 1,
			trigger_print: 1,
			// the leaf renders only for this registration (cheque_printing.leaf_refusal)
			print_job: result.print_job || "",
			_lang: result.language || PDC_LANGUAGES[values.language] || "",
		});
		win.go(`/printview?${params.toString()}`);
		pdc_remember_language(frm, values.language);
		frappe.show_alert({
			message: frappe.utils.escape_html(
				__("Cheque {0} registered as printed ({1}).", [
					frm.doc.cheque_no || frm.doc.name,
					__(values.language, null, "Cheque Layout"),
				])
			),
			indicator: "green",
		});
		frm.reload_doc();
	});
}

// Design 7f §6.4: a print window opened synchronously in the click handler (never blocked as a popup); its URL
// is set once the server answered. Defined once by whichever print form loads first (there are no bundles).
frappe.provide("treasury.print");
if (!treasury.print.open_window) {
	treasury.print.open_window = function () {
		const win = window.open("", "_blank");
		if (win) {
			win.document.write(
				`<p style="font-family:sans-serif">${frappe.utils.escape_html(__("Preparing the print…"))}</p>`
			);
		}
		return {
			go: (url) => (win ? (win.location.href = url) : window.open(url)),
			close: () => win && win.close(),
		};
	};
}

function pdc_replace(frm) {
	const doc = frm.doc;
	frappe.prompt(
		[
			{
				fieldname: "cheque_no",
				label: __("New Cheque No"),
				fieldtype: "Data",
				description: __("Leave empty to take the next leaf of the cheque book."),
				reqd: doc.direction === "Incoming" || !doc.cheque_book ? 1 : 0,
			},
			{
				fieldname: "amount",
				label: __("Amount", null, "Treasury"),
				fieldtype: "Currency",
				default: doc.amount,
				reqd: 1,
			},
			{
				fieldname: "original_due_date",
				label: __("Due Date", null, "Treasury"),
				fieldtype: "Date",
				default: doc.effective_due_date,
				reqd: 1,
			},
			{
				fieldname: "replacement_date",
				label: __("Replacement Date"),
				fieldtype: "Date",
				default: frappe.datetime.get_today(),
				reqd: 1,
			},
			{ fieldname: "reason", label: __("Reason"), fieldtype: "Small Text", reqd: 1 },
		],
		(values) => {
			frm.call({ doc: frm.doc, method: "replace", args: { new_cheque_values: values }, freeze: true }).then((r) => {
				if (r.message && r.message.replaced_by) {
					frappe.set_route("Form", "PDC Cheque", r.message.replaced_by);
				} else {
					frm.reload_doc();
				}
			});
		},
		__("Replace"),
		__("Replace")
	);
}
