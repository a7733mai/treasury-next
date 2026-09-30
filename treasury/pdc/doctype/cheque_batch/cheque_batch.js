// Copyright (c) 2026, Ahmed Abdelmageed and contributors
// For license information, please see license.txt

// Business rules live in treasury/pdc/batch.py and treasury/pdc/lifecycle.py; this script only calls them.
// Translation context of generic words, so their Arabic entries never change ERPNext's own translation.
// Form scripts share the global scope: the names below are prefixed to stay unique.
const CHEQUE_BATCH_CTX = "Cheque Batch";
const CHEQUE_BATCH_STATUS_COLORS = {
	Created: "gray",
	Delivered: "blue",
	Received: "blue",
	Deposited: "purple",
	Cleared: "green",
	Bounced: "red",
	Returned: "orange",
	Endorsed: "cyan",
	Discounted: "cyan",
	Replaced: "light-gray",
	Cancelled: "darkgrey",
};

const CHEQUE_BATCH_RELEASED = ["Cancelled", "Replaced"];
const CHEQUE_BATCH_PAYEE_ROLES = ["Treasury Manager", "Treasury User", "System Manager"];

// mirrors pdc_cheque.reference_rules (invoices / orders only for a supplier's outgoing or a customer's
// incoming batch; any batch may settle Journal Entries of its party)
function cheque_batch_reference_types(direction, party_type) {
	if (direction === "Outgoing" && party_type === "Supplier") {
		return ["Purchase Invoice", "Purchase Order", "Journal Entry"];
	}
	if (direction === "Incoming" && party_type === "Customer") {
		return ["Sales Invoice", "Sales Order", "Journal Entry"];
	}
	return ["Journal Entry"];
}

frappe.ui.form.on("Cheque Batch", {
	setup(frm) {
		// any Party Type (Customer, Supplier, Employee, Shareholder ...)
		frm.set_query("bank_account", () => ({
			filters: { company: frm.doc.company, is_company_account: 1 },
		}));
		frm.set_query("cheque_book", () => ({
			filters: { company: frm.doc.company, bank_account: frm.doc.bank_account, status: "Active" },
		}));
		frm.set_query("reference_doctype", "cheques", () => ({
			filters: { name: ["in", cheque_batch_reference_types(frm.doc.direction, frm.doc.party_type)] },
		}));
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
		frm.set_query("reference_name", "cheques", (doc, cdt, cdn) => {
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
		if (frm.is_new() && !frm.doc.party_type) {
			frm.set_value("party_type", frm.doc.direction === "Incoming" ? "Customer" : "Supplier");
		}
	},

	refresh(frm) {
		frm.events.set_labels(frm);
		frm.events.set_party_label(frm);
		frm.events.set_source_label(frm);
		frm.events.set_source_intro(frm);
		frm.events.render_dashboard(frm);
		frm.events.render_payment_entries(frm);
		cheque_batch_toggle_new_payee(frm);
		if (frm.doc.docstatus === 0) {
			frm.events.add_draft_buttons(frm);
			if (!frm.doc.posting_mode) {
				frm.events.fetch_posting_mode(frm);
			}
		}
		if (frm.doc.docstatus === 1) {
			frm.events.add_submitted_buttons(frm);
		}
		if (!frm.is_new()) {
			frm.add_custom_button(__("Cheques in PDC Register"), () =>
				frappe.set_route("query-report", "PDC Register", {
					company: frm.doc.company,
					cheque_batch: frm.doc.name,
				})
			);
		}
	},

	direction(frm) {
		// an Employee / Shareholder ... the user picked is kept; only the Customer / Supplier default follows
		if (!frm.doc.party_type || ["Customer", "Supplier"].includes(frm.doc.party_type)) {
			frm.set_value("party_type", frm.doc.direction === "Incoming" ? "Customer" : "Supplier");
		}
		if (frm.doc.direction === "Incoming") {
			frm.set_value("cheque_book", "");
		}
		frm.events.set_labels(frm);
		frm.events.fetch_posting_mode(frm);
	},

	company(frm) {
		cheque_batch_unlink_source(frm);
		frm.events.fetch_posting_mode(frm);
	},

	party_type(frm) {
		frm.set_value("party", "");
		frm.events.set_party_label(frm);
		cheque_batch_toggle_new_payee(frm);
	},

	party(frm) {
		// the server defaults them again for the new party on save
		frm.set_value("party_bank_account", "");
		frm.set_value("contact_person", "");
		cheque_batch_unlink_source(frm);
		cheque_batch_toggle_new_payee(frm);
	},

	set_source_label(frm) {
		// one readable row: "Created From Purchase Invoice" (the type field itself is hidden)
		const label = frm.doc.source_doctype
			? __("Created From {0}", [__(frm.doc.source_doctype)])
			: __("Created From", null, CHEQUE_BATCH_CTX);
		frm.set_df_property("source_name", "label", frappe.utils.escape_html(label));
	},

	set_source_intro(frm) {
		// what is left to type on a batch opened from a document (Treasury buttons) or created from one
		const doc = frm.doc;
		const from = cheque_batch_from(frm);
		if (doc.docstatus !== 0 || !from) {
			frm.set_intro();
			return;
		}
		const rows = (doc.cheques || []).length;
		const link = cheque_batch_link(from.doctype, from.name);
		let text;
		if (!frm.is_new()) {
			if (rows || !doc.source_name) {
				frm.set_intro();
				return;
			}
			text = __(
				"Created from {0}. Generate Cheques allocates the cheques to it in due-date order; cheques beyond its outstanding stay advances.",
				[link]
			);
		} else if (!doc.source_name) {
			// a party master: an on-account batch, the amounts are the user's
			text =
				doc.direction === "Outgoing" && doc.cheque_book
					? __("For {0}: type the Total Amount to Split, then click Generate Cheques.", [link])
					: __("For {0}: type the Total Amount to Split and the First Cheque No, then click Generate Cheques.", [
							link,
					  ]);
		} else if (rows) {
			text = __("From {0}: {1} cheques generated and allocated to it. Check the rows and save.", [link, rows]);
		} else {
			text = __("From {0}: type the First Cheque No, then click Generate Cheques.", [link]);
		}
		// the link is escaped (cheque_batch_link); the sentences are our own translations
		frm.set_intro(text, "blue");
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
	},

	set_labels(frm) {
		const incoming = frm.doc.direction === "Incoming";
		frm.set_df_property("handover_date", "label", incoming ? __("Receipt Date") : __("Handover Date"));
		frm.toggle_reqd("bank_account", !incoming);
	},

	fetch_posting_mode(frm) {
		if (!frm.doc.company || !frm.doc.direction) {
			return;
		}
		frappe
			.call({
				method: "treasury.pdc.doctype.cheque_batch.cheque_batch.get_posting_mode",
				args: { company: frm.doc.company, direction: frm.doc.direction },
			})
			.then((r) => {
				if (frm.doc.docstatus === 0 && (r.message || "") !== (frm.doc.posting_mode || "")) {
					frm.set_value("posting_mode", r.message || "");
				}
			});
	},

	add_draft_buttons(frm) {
		frm.add_custom_button(__("Generate Cheques"), () => {
			const generate = () =>
				frm.call({ doc: frm.doc, method: "generate_rows", doc: frm.doc, freeze: true }).then((r) => {
					frm.refresh_fields();
					const numbers = (r.message && r.message.cheque_numbers) || [];
					if (numbers.length) {
						frappe.show_alert({
							message: __("Cheque numbers {0} to {1} will be taken from the cheque book on submit.", [
								numbers[0],
								numbers[numbers.length - 1],
							]),
							indicator: "blue",
						});
					}
					// nothing allocated (other currency): the server's own message says why
					const allocated = flt(r.message && r.message.allocated);
					if (allocated > 0 && frm.doc.source_name) {
						const esc = frappe.utils.escape_html;
						const money = (value) => esc(format_currency(value, frm.doc.currency));
						const unallocated = flt(flt(r.message.total_amount) - allocated, precision("total_amount", frm.doc));
						let message = __("Allocated to {0}: {1}", [esc(frm.doc.source_name), money(allocated)]);
						if (unallocated > 0) {
							message += ` · ${esc(__("Unallocated (advance)"))}: ${money(unallocated)}`;
						}
						frappe.show_alert({ message, indicator: unallocated > 0 ? "blue" : "green" });
					}
					frm.events.set_source_intro(frm);
				});
			if ((frm.doc.cheques || []).length) {
				frappe.confirm(__("Replace the {0} existing rows?", [frm.doc.cheques.length]), generate);
			} else {
				generate();
			}
		});
		if (frm.doc.allocation_method === "FIFO Invoices") {
			frm.add_custom_button(__("Allocate FIFO"), () => {
				frm.call({ doc: frm.doc, method: "allocate_fifo", doc: frm.doc, freeze: true }).then((r) => {
					frm.refresh_fields();
					frm.events.show_fifo_summary(frm, r.message);
				});
			});
		}
	},

	allocation_method(frm) {
		frm.refresh();
	},

	show_fifo_summary(frm, summary) {
		if (!summary) {
			return;
		}
		const fmt = (value) => format_currency(value, frm.doc.currency);
		const rows = (summary.invoices || [])
			.map(
				(d) => `<tr>
					<td>${frappe.utils.escape_html(d.reference_name)}</td>
					<td class="text-right">${fmt(d.outstanding)}</td>
					<td class="text-right">${fmt(d.allocated)}</td>
				</tr>`
			)
			.join("");
		frappe.msgprint({
			title: __("FIFO Allocation"),
			indicator: "blue",
			message: `<table class="table table-bordered table-sm">
				<thead><tr>
					<th>${__("Invoice", null, CHEQUE_BATCH_CTX)}</th>
					<th>${__("Outstanding", null, CHEQUE_BATCH_CTX)}</th>
					<th>${__("Allocated Amount")}</th>
				</tr></thead>
				<tbody>${rows || `<tr><td colspan="3">${__("No outstanding invoices.")}</td></tr>`}</tbody>
			</table>
			<p>${__("Allocated", null, CHEQUE_BATCH_CTX)}: <b>${fmt(summary.allocated)}</b><br>
			${__("Unallocated (advance)")}: <b>${fmt(summary.unallocated)}</b><br>
			${__("Outstanding not covered")}: <b>${fmt(summary.not_covered)}</b></p>
			${
				flt(summary.unallocated) > 0 && flt(summary.not_covered) > 0
					? `<p class="text-muted">${__(
							"One invoice per cheque: part of a cheque stays an advance while invoices are still open. Allocate those rows by hand, or reconcile the advance later."
					  )}</p>`
					: ""
			}`,
		});
	},

	add_submitted_buttons(frm) {
		const outgoing = frm.doc.direction === "Outgoing";
		const cheques = (frm.doc.__onload && frm.doc.__onload.cheques) || [];
		if (cheques.some((c) => c.operational_status === "Created")) {
			const method = outgoing ? "deliver_all" : "receive_all";
			const date_field = outgoing ? "handover_date" : "receipt_date";
			const label = outgoing ? __("Deliver All") : __("Receive All");
			frm.add_custom_button(
				label,
				() => {
					frappe.prompt(
						[
							{
								fieldname: date_field,
								label: outgoing ? __("Handover Date") : __("Receipt Date"),
								fieldtype: "Date",
								reqd: 1,
								default: frappe.datetime.get_today(),
							},
							{ fieldname: "remarks", label: __("Remarks", null, "Treasury"), fieldtype: "Small Text" },
						],
						(values) => {
							frm.call({ doc: frm.doc, method, args: values, freeze: true }).then((r) => {
								frm.events.show_bulk_result(r.message);
								frm.reload_doc();
							});
						},
						label,
						label
					);
				},
				__("Actions")
			);
		}

		const open_print = (params) => window.open(`/printview?${new URLSearchParams(params).toString()}`);
		frm.add_custom_button(
			__("Print Batch Receipt"),
			() =>
				open_print({
					doctype: frm.doctype,
					name: frm.doc.name,
					format: "Cheque Batch Receipt",
					_lang: frappe.boot.lang || "",
				}),
			__("Print", null, "PDC Cheque")
		);
		frm.add_custom_button(
			__("Print All Cheques"),
			() => {
				frm.call({ doc: frm.doc, method: "print_all", doc: frm.doc }).then((r) => {
					const info = r.message;
					if (!info || !info.names.length) {
						frappe.msgprint(__("No cheques to print."));
						return;
					}
					const params = new URLSearchParams({
						doctype: info.doctype,
						name: JSON.stringify(info.names),
						format: info.print_format,
						_lang: frappe.boot.lang || "",
					});
					window.open(`/api/method/frappe.utils.print_format.download_multi_pdf?${params.toString()}`);
				});
			},
			__("Print", null, "PDC Cheque")
		);
	},

	show_bulk_result(result) {
		if (!result) {
			return;
		}
		const esc = frappe.utils.escape_html;
		const failed = (result.failed || [])
			.map((f) => `<li><b>${esc(f.cheque)}</b> (${esc(f.cheque_no || "")}): ${esc(f.error)}</li>`)
			.join("");
		frappe.msgprint({
			title: __("Result", null, CHEQUE_BATCH_CTX),
			indicator: failed ? "orange" : "green",
			message: `<p>${__("Succeeded", null, CHEQUE_BATCH_CTX)}: <b>${(result.succeeded || []).length}</b>,
				${__("Failed", null, CHEQUE_BATCH_CTX)}: <b>${(result.failed || []).length}</b>,
				${__("Skipped", null, CHEQUE_BATCH_CTX)}: <b>${(result.skipped || []).length}</b></p>
				${failed ? `<ul>${failed}</ul>` : ""}`,
		});
	},

	render_dashboard(frm) {
		const field = frm.get_field("cheques_html");
		if (!field || frm.doc.docstatus === 0) {
			return;
		}
		const cheques = (frm.doc.__onload && frm.doc.__onload.cheques) || [];
		if (!cheques.length) {
			field.$wrapper.html(`<p class="text-muted">${__("No cheques yet.")}</p>`);
			return;
		}
		const esc = frappe.utils.escape_html;
		const rows = cheques
			.map((c) => {
				const route = `/app/pdc-cheque/${encodeURIComponent(c.name)}`;
				const color = CHEQUE_BATCH_STATUS_COLORS[c.operational_status] || "gray";
				return `<tr>
					<td><a href="${route}">${esc(c.name)}</a></td>
					<td>${esc(c.cheque_no || "")}</td>
					<td>${frappe.datetime.str_to_user(c.effective_due_date)}</td>
					<td class="text-right">${format_currency(c.amount, c.currency)}</td>
					<td><span class="indicator-pill ${color}">${__(c.operational_status, null, "PDC Cheque")}</span></td>
					<td>${__(c.accounting_status, null, "PDC Cheque")}</td>
				</tr>`;
			})
			.join("");
		field.$wrapper.html(`<table class="table table-bordered table-sm">
			<thead><tr>
				<th>${__("PDC Cheque")}</th><th>${__("Cheque No")}</th><th>${__("Effective Due Date")}</th>
				<th>${__("Amount", null, "Treasury")}</th><th>${__("Operational Status")}</th>
				<th>${__("Accounting Status")}</th>
			</tr></thead>
			<tbody>${rows}</tbody>
		</table>`);
	},

	render_payment_entries(frm) {
		// the Payment Entry of every live cheque: the numbers the batch's papers are filed under
		const field = frm.get_field("payment_entries_html");
		if (!field || frm.doc.docstatus !== 1) {
			return;
		}
		const cheques = ((frm.doc.__onload && frm.doc.__onload.cheques) || []).filter(
			(c) => !CHEQUE_BATCH_RELEASED.includes(c.operational_status)
		);
		const esc = frappe.utils.escape_html;
		// "Created from <document>": built from escaped parts so that the link survives
		const source =
			frm.doc.source_doctype && frm.doc.source_name
				? __("Created from {0}", [cheque_batch_link(frm.doc.source_doctype, frm.doc.source_name)])
				: "";
		if (!cheques.length) {
			field.$wrapper.html("");
			source && frm.dashboard.set_headline(source, "blue");
			return;
		}
		const posted = cheques.filter((c) => c.party_payment_entry);
		const posted_text = esc(
			__("Payment Entries: {0}", [__("{0} of {1} cheques posted", [posted.length, cheques.length])])
		);
		frm.dashboard.set_headline(
			source ? `${source} · ${posted_text}` : posted_text,
			posted.length === cheques.length ? "green" : "blue"
		);
		const pe_link = (name) =>
			name
				? `<a href="/app/payment-entry/${encodeURIComponent(name)}"><b><bdi>${esc(name)}</bdi></b></a>`
				: `<span class="text-muted">—</span>`;
		// no Under Collection column when no cheque has one (outgoing batches)
		const with_uc = cheques.some((c) => c.under_collection_payment_entry);
		const rows = cheques
			.map(
				(c) => `<tr>
					<td><a href="/app/pdc-cheque/${encodeURIComponent(c.name)}"><bdi>${esc(c.cheque_no || c.name)}</bdi></a></td>
					<td style="white-space: nowrap">${esc(frappe.datetime.str_to_user(c.effective_due_date))}</td>
					<td>${pe_link(c.party_payment_entry)}</td>
					${with_uc ? `<td>${pe_link(c.under_collection_payment_entry)}</td>` : ""}
					<td>${pe_link(c.bank_payment_entry)}</td>
				</tr>`
			)
			.join("");
		const names = cheques
			.flatMap((c) => [c.party_payment_entry, c.under_collection_payment_entry, c.bank_payment_entry])
			.filter(Boolean);
		field.$wrapper.html(`<div class="table-responsive"><table class="table table-bordered table-sm">
			<thead class="bg-light"><tr>
				<th>${esc(__("Cheque No"))}</th>
				<th>${esc(__("Due Date", null, "Treasury"))}</th>
				<th>${esc(__("Party Posting", null, "Treasury Voucher"))}</th>
				${with_uc ? `<th>${esc(__("Under Collection", null, "Treasury Voucher"))}</th>` : ""}
				<th>${esc(__("Bank Settlement", null, "Treasury Voucher"))}</th>
			</tr></thead>
			<tbody>${rows}</tbody>
		</table></div>
		<p class="text-muted small">${esc(
			__("{0} of {1} cheques posted to party", [posted.length, cheques.length])
		)}${
			names.length
				? ` · <a class="treasury-copy-pe">${esc(__("Copy all PE numbers"))}</a>`
				: ""
		}</p>`);
		field.$wrapper.find(".treasury-copy-pe").on("click", () => {
			frappe.utils.copy_to_clipboard(names.join(", "));
		});
	},
});

function cheque_batch_link(doctype, name) {
	// "<Purchase Invoice> <ACC-PINV-...>" as a link to the document (escaped)
	const esc = frappe.utils.escape_html;
	const route = `/app/${frappe.router.slug(doctype)}/${encodeURIComponent(name)}`;
	return `<a href="${route}">${esc(__(doctype))} <b><bdi>${esc(name)}</bdi></b></a>`;
}

function cheque_batch_from(frm) {
	// the document the batch comes from: its source, or the party master a Treasury button was clicked on
	// (`__treasury_from`: set on the unsaved document by public/js/treasury_payment_buttons.js, never saved)
	const doc = frm.doc;
	if (doc.source_doctype && doc.source_name) {
		return { doctype: doc.source_doctype, name: doc.source_name };
	}
	const from = frm.is_new() && doc.__treasury_from;
	return from && from.doctype && from.name ? from : null;
}

function cheque_batch_unlink_source(frm) {
	// a draft whose party or company changes no longer settles its source: say so now, not on save
	const doc = frm.doc;
	if (doc.__treasury_from) {
		delete doc.__treasury_from;
		frm.events.set_source_intro(frm);
	}
	if (doc.docstatus !== 0 || !doc.source_name) {
		return;
	}
	const label = `${__(doc.source_doctype || "")} ${doc.source_name}`.trim();
	frm.set_value({ source_doctype: "", source_name: "" }).then(() => {
		frm.events.set_source_label(frm);
		frm.events.set_source_intro(frm);
	});
	frappe.show_alert({
		message: __("The batch is no longer linked to {0}.", [frappe.utils.escape_html(label)]),
		indicator: "orange",
	});
}

function cheque_batch_toggle_new_payee(frm) {
	// "+ New Payee" under the party field of a draft batch (treasury.utils.party.create_payee)
	const field = frm.fields_dict.party;
	if (!field) {
		return;
	}
	const help = field.$wrapper.find(".help-box");
	const show =
		frm.doc.docstatus === 0 &&
		!frm.doc.party &&
		(!frm.doc.party_type || frm.doc.party_type === "Supplier") &&
		frappe.user.has_role(CHEQUE_BATCH_PAYEE_ROLES);
	if (!show) {
		help.html("").addClass("hide");
		return;
	}
	help.html(`<a class="treasury-new-payee">+ ${frappe.utils.escape_html(__("New Payee"))}</a>`).removeClass("hide");
	help.find(".treasury-new-payee").on("click", () => cheque_batch_new_payee(frm));
}

function cheque_batch_new_payee(frm) {
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
