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
				frm.call({ method: "refresh_status", freeze: true }).then(() => frm.reload_doc());
			});
		}
	},

	generate_schedule(frm) {
		const run = () =>
			frm.call({ method: "generate_schedule", doc: frm.doc, freeze: true }).then((r) => {
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
