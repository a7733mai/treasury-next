// Copyright (c) 2026, Ahmed Abdelmageed and contributors
// For license information, please see license.txt

// Business rules live in treasury/pdc/lifecycle.py; the buttons below only mirror them for convenience.
const PDC_SETTLED = ["Cleared", "Bounced", "Returned", "Endorsed", "Discounted", "Replaced", "Cancelled"];
// Accounting details copied to every voucher (PDCCheque ACCOUNTING_FIELDS): locked once the cheque is posted.
const PDC_ACCOUNTING_FIELDS = ["cost_center", "project", "mode_of_payment", "party_bank_account", "contact_person"];
const PDC_PAYEE_ROLES = ["Treasury Manager", "Treasury User", "System Manager"];

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
		// the PDC Account Mapping of the bank may name its own cheque layout
		const cheque_format = (frm.doc.__onload && frm.doc.__onload.cheque_print_format) || "PDC Cheque Print";
		frm.add_custom_button(
			__("Print Cheque"),
			() => open_print(frm.doctype, frm.doc.name, cheque_format),
			__("Print", null, "PDC Cheque")
		);
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
		fieldnames.forEach((fieldname) =>
			frm.set_df_property(
				fieldname,
				"read_only",
				locked || (batch_locked && fieldname !== "exchange_rate") ? 1 : 0
			)
		);
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

// Low balance guard (Block mode): a Treasury Manager may confirm the override and call again. The server is
// authoritative: it checks the role and logs the override.
const PDC_OVERRIDE_ROLES = ["Treasury Manager", "System Manager"];

function pdc_call_with_override(frm, method, args, override) {
	frm.call({
		doc: frm.doc,
		method,
		args: Object.assign({}, args, override ? { allow_low_balance: 1 } : {}),
		freeze: true,
		error: (r) => {
			if (!override && r && r.exc_type === "LowBalanceError" && frappe.user.has_role(PDC_OVERRIDE_ROLES)) {
				frappe.confirm(__("Override the low balance block and continue?"), () =>
					pdc_call_with_override(frm, method, args, true)
				);
			}
		},
	}).then(() => frm.reload_doc());
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
