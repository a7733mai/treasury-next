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

frappe.ui.form.on("Cheque Batch", {
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
		frm.set_query("reference_doctype", "cheques", () => ({
			filters: {
				name: [
					"in",
					frm.doc.direction === "Incoming"
						? ["Sales Invoice", "Sales Order", "Journal Entry"]
						: ["Purchase Invoice", "Purchase Order", "Journal Entry"],
				],
			},
		}));
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
		frm.events.render_dashboard(frm);
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
		frm.set_value("party_type", frm.doc.direction === "Incoming" ? "Customer" : "Supplier");
		if (frm.doc.direction === "Incoming") {
			frm.set_value("cheque_book", "");
		}
		frm.events.set_labels(frm);
		frm.events.fetch_posting_mode(frm);
	},

	company(frm) {
		frm.events.fetch_posting_mode(frm);
	},

	party_type(frm) {
		frm.set_value("party", "");
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
							{ fieldname: "remarks", label: __("Remarks"), fieldtype: "Small Text" },
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
				<th>${__("Amount")}</th><th>${__("Operational Status")}</th><th>${__("Accounting Status")}</th>
			</tr></thead>
			<tbody>${rows}</tbody>
		</table>`);
	},
});
