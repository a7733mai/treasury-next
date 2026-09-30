// Copyright (c) 2026, Ahmed Abdelmageed and contributors
// For license information, please see license.txt

// Business rules live in treasury/bank_facilities (schedule.py, accounting.py); the form only calls them.
frappe.ui.form.on("Facility Drawdown", {
	setup(frm) {
		frm.set_query("facility", () => ({ filters: { status: "Active" } }));
		frm.set_query("bank_account", () => ({
			filters: { company: frm.doc.company, is_company_account: 1 },
		}));
		frm.set_query("fees_account", () => ({
			filters: { company: frm.doc.company, is_group: 0, root_type: "Expense" },
		}));
	},

	refresh(frm) {
		// vouchers are created by posting only: no "+" beside them in Connections
		frm.can_make_methods = { "Payment Entry": () => false, "Journal Entry": () => false };
		// server-rendered, escaped summary (treasury.utils.voucher_summary)
		frm.get_field("vouchers_html").$wrapper.html((frm.doc.__onload && frm.doc.__onload.voucher_summary) || "");
		if (frm.doc.docstatus === 0 && !frm.is_new()) {
			frm.add_custom_button(__("Generate Schedule"), () => frm.events.generate_schedule(frm));
		}
		if (frm.doc.docstatus === 1) {
			const outstanding = (frm.doc.__onload || {}).outstanding_principal;
			if (outstanding !== undefined) {
				frm.dashboard.add_indicator(
					__("Outstanding Principal: {0}", [format_currency(outstanding, frm.doc.currency)]),
					outstanding > 0 ? "orange" : "green"
				);
			}
			if (frm.doc.repayment_status === "Open" && frappe.model.can_create("Facility Repayment")) {
				frm.add_custom_button(
					__("Facility Repayment"),
					() => frappe.new_doc("Facility Repayment", { drawdown: frm.doc.name }),
					__("Create")
				);
			}
			frm.add_custom_button(__("Refresh Status"), () => {
				frm.call({ doc: frm.doc, method: "refresh_status", freeze: true }).then(() => frm.reload_doc());
			});
			frm.events.add_facility_actions(frm);
		}
		frm.events.set_islamic_labels(frm);
	},

	set_islamic_labels(frm) {
		// "Profit" / "Rental" wording of an Islamic facility (server terms, treasury.bank_facilities.lenders)
		const terms = (frm.doc.__onload || {}).terms;
		if (!terms || !frm.doc.is_islamic) {
			return;
		}
		frm.set_df_property("interest_rate", "label", terms.interest_rate);
		frm.set_df_property("total_interest", "label", terms.interest);
		const grid = frm.get_field("repayment_schedule").grid;
		const interest = frm.doc.limit_type === "Ijarah" ? __("Rental") : terms.interest;
		grid.update_docfield_property("interest", "label", interest);
	},

	add_facility_actions(frm) {
		if (frm.is_dirty()) {
			return;
		}
		if (
			frm.doc.repayment_mode === "PDC to Lender" &&
			frm.doc.repayment_status === "Open" &&
			frappe.model.can_create("Cheque Batch")
		) {
			frm.add_custom_button(
				__("Generate Installment Cheques"),
				() =>
					frm.call({ doc: frm.doc, method: "make_installment_cheques", freeze: true }).then((r) => {
						if (r.message) {
							const synced = frappe.model.sync(r.message)[0];
							frappe.set_route("Form", synced.doctype, synced.name);
						}
					}),
				__("Create")
			);
		}
		const pending = (frm.doc.disbursement_stages || []).filter((row) => row.status !== "Disbursed");
		if (frm.doc.limit_type === "Istisna'a" && pending.length && frappe.user.has_role("Treasury Manager")) {
			frm.add_custom_button(__("Disburse Stage"), () => {
				frappe.prompt(
					[
						{
							fieldname: "stage",
							label: __("Stage"),
							fieldtype: "Select",
							reqd: 1,
							options: pending.map((row) => ({
								value: row.name,
								label: `${row.idx}: ${format_currency(row.amount, frm.doc.currency)}`,
							})),
							default: pending[0].name,
						},
						{
							fieldname: "disbursement_date",
							label: __("Date", null, "Treasury"),
							fieldtype: "Date",
							reqd: 1,
							default: frappe.datetime.get_today(),
						},
						{ fieldname: "remarks", label: __("Remarks", null, "Treasury"), fieldtype: "Small Text" },
					],
					(values) =>
						frm
							.call({ doc: frm.doc, method: "disburse_stage", args: values, freeze: true })
							.then(() => frm.reload_doc()),
					__("Disburse Stage"),
					__("Disburse Stage")
				);
			});
		}
	},

	generate_schedule(frm) {
		const run = () =>
			frm.call({ doc: frm.doc, method: "generate_schedule", doc: frm.doc, freeze: true }).then((r) => {
				if (r.message) {
					frappe.model.sync(r.message);
					frm.refresh();
					frm.dirty();
					frm.save();
				}
			});
		if (frm.is_dirty()) {
			frm.save().then(run);
		} else {
			run();
		}
	},

	facility(frm) {
		frm.set_value("bank_account", null);
		frm.set_value("interest_rate", 0);
		frm.clear_table("repayment_schedule");
		frm.refresh_field("repayment_schedule");
	},

	limit_type(frm) {
		frm.set_value("interest_rate", 0);
		frm.set_value("rate_type", null);
		frm.set_value("day_count", null);
	},
});

// A term change makes the schedule stale: clear it, the server rebuilds an empty schedule on save (and refuses
// a changed term with an unchanged schedule).
const TERM_FIELDS = [
	"amount",
	"drawdown_date",
	"interest_rate",
	"day_count",
	"profit_amount",
	"tenor_months",
	"repayment_frequency",
	"repayment_method",
	"first_repayment_date",
	"grace_months",
];
frappe.ui.form.on(
	"Facility Drawdown",
	Object.fromEntries(
		TERM_FIELDS.map((fieldname) => [
			fieldname,
			(frm) => {
				if (frm.doc.docstatus === 0 && (frm.doc.repayment_schedule || []).length) {
					frm.clear_table("repayment_schedule");
					frm.refresh_field("repayment_schedule");
				}
			},
		])
	)
);
