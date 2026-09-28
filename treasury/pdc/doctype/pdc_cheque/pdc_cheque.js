// Copyright (c) 2026, Ahmed Abdelmageed and contributors
// For license information, please see license.txt

// Business rules live in treasury/pdc/lifecycle.py; the buttons below only mirror them for convenience.
const PDC_SETTLED = ["Cleared", "Bounced", "Returned", "Endorsed", "Discounted", "Replaced", "Cancelled"];

frappe.ui.form.on("PDC Cheque", {
	setup(frm) {
		frm.set_query("party_type", () => ({
			filters: { name: ["in", ["Customer", "Supplier"]] },
		}));
		frm.set_query("bank_account", () => ({
			filters: { company: frm.doc.company, is_company_account: 1 },
		}));
		frm.set_query("cheque_book", () => ({
			filters: { company: frm.doc.company, bank_account: frm.doc.bank_account, status: "Active" },
		}));
		frm.set_query("reference_doctype", "references", () => ({
			filters: {
				name: [
					"in",
					frm.doc.direction === "Incoming"
						? ["Sales Invoice", "Sales Order", "Journal Entry"]
						: ["Purchase Invoice", "Purchase Order", "Journal Entry"],
				],
			},
		}));
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
		frm.events.lock_identity_fields(frm);
		frm.events.translate_status_log(frm);
		frm.events.render_vouchers(frm);
		frm.events.set_indicator(frm);
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
			__("Print")
		);
		// the PDC Account Mapping of the bank may name its own cheque layout
		const cheque_format = (frm.doc.__onload && frm.doc.__onload.cheque_print_format) || "PDC Cheque Print";
		frm.add_custom_button(
			__("Print Cheque"),
			() => open_print(frm.doctype, frm.doc.name, cheque_format),
			__("Print")
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
						__("Print")
					);
				}
			});
		});
	},

	direction(frm) {
		frm.events.set_labels(frm);
		frm.set_value("party_type", frm.doc.direction === "Incoming" ? "Customer" : "Supplier");
		if (frm.doc.direction === "Incoming") {
			frm.set_value("cheque_book", "");
		}
	},

	party_type(frm) {
		frm.set_value("party", "");
	},

	bank_account(frm) {
		frm.set_value("cheque_book", "");
	},

	cheque_book(frm) {
		frm.events.set_labels(frm);
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
		fieldnames.forEach((fieldname) => frm.set_df_property(fieldname, "read_only", locked ? 1 : 0));
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
			frm.page.set_indicator(__("Overdue"), "red");
		} else {
			frm.page.set_indicator(__(frm.doc.operational_status), "blue");
		}
	},

	render_vouchers(frm) {
		const wrapper = frm.get_field("vouchers_html").$wrapper;
		const vouchers = (frm.doc.__onload && frm.doc.__onload.vouchers) || [];
		if (!vouchers.length) {
			wrapper.html(`<p class="text-muted">${__("No vouchers yet.")}</p>`);
			return;
		}
		const status = { 0: __("Draft"), 1: __("Submitted"), 2: __("Cancelled") };
		const rows = vouchers
			.map((v) => {
				const route = `/app/${frappe.router.slug(v.doctype)}/${encodeURIComponent(v.name)}`;
				return `<tr>
					<td>${__(v.doctype)}</td>
					<td><a href="${route}">${frappe.utils.escape_html(v.name)}</a></td>
					<td>${frappe.utils.escape_html(v.treasury_event || "")}</td>
					<td>${v.treasury_cycle || 1}</td>
					<td>${frappe.datetime.str_to_user(v.posting_date)}</td>
					<td>${status[v.docstatus] || ""}</td>
				</tr>`;
			})
			.join("");
		wrapper.html(`<table class="table table-bordered table-sm">
			<thead><tr>
				<th>${__("Voucher Type")}</th><th>${__("Voucher No")}</th><th>${__("Event")}</th>
				<th>${__("Cycle")}</th><th>${__("Posting Date")}</th><th>${__("Status")}</th>
			</tr></thead>
			<tbody>${rows}</tbody>
		</table>`);
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
		const remarks = { fieldname: "remarks", label: __("Remarks"), fieldtype: "Small Text" };

		if (status === "Created") {
			if (outgoing) {
				pdc_action(frm, "deliver", __("Deliver"), [date("handover_date", __("Handover Date")), remarks]);
			} else {
				pdc_action(frm, "receive", __("Receive"), [date("receipt_date", __("Receipt Date")), remarks]);
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
				{ ...date("posting_date", __("Posting Date")), default: default_date },
			]);
		}

		if (!outgoing && status === "Received") {
			pdc_action(frm, "deposit", __("Deposit"), [
				date("deposit_date", __("Deposit Date")),
				{
					fieldname: "bank_account",
					label: __("Bank Account"),
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
			pdc_action(frm, "clear", __("Clear"), [
				date("clearance_date", __("Actual Clearance Date")),
				{
					// an incoming cheque received without a bank: the bank it was cleared into
					fieldname: "bank_account",
					label: __("Bank Account"),
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

		if (status === "Cleared" && !["Bank Posted", "Reversed"].includes(doc.accounting_status)) {
			pdc_action(frm, "post_bank", __("Post to Bank"), [
				{
					...date("posting_date", __("Posting Date")),
					default: doc.bank_transaction_date || doc.actual_clearance_date,
				},
				{ fieldname: "bank_charges", label: __("Bank Charges"), fieldtype: "Currency", default: doc.bank_charges },
			]);
		}

		if (!PDC_SETTLED.includes(status) && can_postpone) {
			pdc_action(frm, "postpone", __("Postpone"), [
				date("new_due_date", __("New Due Date")),
				reason,
				{ fieldname: "contact_person", label: __("Contact Person"), fieldtype: "Data" },
			]);
		}

		if (status === "Bounced" && is_manager) {
			pdc_action(frm, "represent", __("Re-present"), [
				date("represent_date", __("Date")),
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

function pdc_action(frm, method, label, fields) {
	frm.add_custom_button(
		label,
		() => {
			frappe.prompt(
				fields,
				(values) => {
					frm.call({ method, args: values, freeze: true }).then(() => frm.reload_doc());
				},
				label,
				label
			);
		},
		__("Actions")
	);
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
			{ fieldname: "amount", label: __("Amount"), fieldtype: "Currency", default: doc.amount, reqd: 1 },
			{
				fieldname: "original_due_date",
				label: __("Due Date"),
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
			frm.call({ method: "replace", args: { new_cheque_values: values }, freeze: true }).then((r) => {
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
