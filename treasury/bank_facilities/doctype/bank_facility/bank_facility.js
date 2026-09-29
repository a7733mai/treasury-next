// Copyright (c) 2026, Ahmed Abdelmageed and contributors
// For license information, please see license.txt

// Business rules live in treasury/bank_facilities/facility.py; the buttons below only mirror them.
frappe.ui.form.on("Bank Facility", {
	setup(frm) {
		frm.set_query("default_bank_account", () => ({
			filters: { company: frm.doc.company, is_company_account: 1, bank: frm.doc.bank },
		}));
		const account_query = (root_type, currency) => ({
			filters: {
				company: frm.doc.company,
				is_group: 0,
				root_type,
				account_currency: currency,
				account_type: ["not in", ["Payable", "Receivable"]],
			},
		});
		frm.set_query("interest_expense_account", () => account_query("Expense", frm.company_currency));
		frm.set_query("commission_expense_account", () => account_query("Expense", frm.company_currency));
		frm.set_query("accrued_interest_account", () => account_query("Liability", frm.doc.currency));
		frm.set_query("liability_account", "limits", () => account_query("Liability", frm.doc.currency));
		frm.set_query("security_cheque", "security_cheques", () => ({
			filters: { company: frm.doc.company },
		}));
	},

	onload(frm) {
		if (frm.is_new() && !frm.doc.company) {
			frm.set_value("company", frappe.defaults.get_user_default("Company"));
		}
	},

	company(frm) {
		frm.events.set_company_currency(frm);
	},

	set_company_currency(frm) {
		if (!frm.doc.company) {
			return;
		}
		frappe.db.get_value("Company", frm.doc.company, "default_currency").then((r) => {
			frm.company_currency = r.message && r.message.default_currency;
			if (frm.is_new() && !frm.doc.currency) {
				frm.set_value("currency", frm.company_currency);
			}
		});
	},

	refresh(frm) {
		frm.events.set_company_currency(frm);
		frm.events.lock_fields(frm);
		frm.events.translate_logs(frm);
		if (!frm.is_new()) {
			frm.page.set_indicator(
				__(frm.doc.status, null, "Bank Facility"),
				{ Draft: "gray", Active: "green", Suspended: "orange", Expired: "red", Closed: "darkgrey" }[
					frm.doc.status
				] || "blue"
			);
			frm.events.add_actions(frm);
		}
	},

	lock_fields(frm) {
		// mirrors BankFacility.validate_locked_fields: key terms move through Renew only once activated
		const locked = !frm.is_new() && frm.doc.status !== "Draft";
		["company", "bank", "currency", "total_limit", "start_date", "expiry_date"].forEach((fieldname) =>
			frm.set_df_property(fieldname, "read_only", locked ? 1 : 0)
		);
	},

	translate_logs(frm) {
		// statuses are stored in English in the status log (Data fields): translate them on display
		["from_status", "to_status"].forEach((fieldname) => {
			for (const name of [null, frm.doc.name]) {
				const df = frappe.meta.get_docfield("Bank Facility Status Log", fieldname, name);
				if (df) {
					df.formatter = (value) =>
						frappe.utils.escape_html(value ? __(value, null, "Bank Facility") : "");
				}
			}
		});
		frm.refresh_field("status_log");
	},

	add_actions(frm) {
		if (frm.is_dirty()) {
			return;
		}
		const status = frm.doc.status;
		const is_manager = frappe.user.has_role("Treasury Manager");
		const date = (fieldname, label) => ({
			fieldname,
			label,
			fieldtype: "Date",
			reqd: 1,
			default: frappe.datetime.get_today(),
		});
		const reason = { fieldname: "reason", label: __("Reason"), fieldtype: "Small Text", reqd: 1 };
		const remarks = { fieldname: "remarks", label: __("Remarks", null, "Treasury"), fieldtype: "Small Text" };

		frm.add_custom_button(__("Refresh Utilisation"), () => {
			frm.call({ doc: frm.doc, method: "refresh_utilisation", freeze: true }).then(() => frm.reload_doc());
		});
		if (["Active", "Suspended"].includes(status) && frappe.model.can_create("Facility Drawdown")) {
			frm.add_custom_button(
				__("Facility Drawdown"),
				() => frappe.new_doc("Facility Drawdown", { facility: frm.doc.name }),
				__("Create")
			);
		}
		if (!is_manager) {
			return;
		}
		if (status === "Draft") {
			facility_action(frm, "activate", __("Activate"), [
				date("activation_date", __("Date", null, "Treasury")),
				remarks,
			]);
		}
		if (status === "Suspended") {
			facility_action(frm, "activate", __("Reactivate"), [
				date("activation_date", __("Date", null, "Treasury")),
				remarks,
			]);
		}
		if (status === "Active") {
			facility_action(frm, "suspend", __("Suspend"), [
				date("suspension_date", __("Date", null, "Treasury")),
				reason,
			]);
		}
		if (["Active", "Suspended", "Expired"].includes(status)) {
			const limit_fields = (frm.doc.limits || []).map((row) => ({
				fieldname: `limit_${row.idx}`,
				label: __("New {0} Limit", [__(row.limit_type, null, "Bank Facility")]),
				fieldtype: "Currency",
				options: frm.doc.currency,
				default: row.limit_amount,
				limit_type: row.limit_type,
			}));
			frm.add_custom_button(
				__("Renew"),
				() => {
					frappe.prompt(
						[
							{
								fieldname: "new_expiry_date",
								label: __("New Expiry Date"),
								fieldtype: "Date",
								reqd: 1,
								default: frm.doc.expiry_date,
							},
							date("renewal_date", __("Renewal Date")),
							{
								fieldname: "new_total_limit",
								label: __("New Total Limit"),
								fieldtype: "Currency",
								options: frm.doc.currency,
								default: frm.doc.total_limit,
							},
							...limit_fields,
							remarks,
						],
						(values) => {
							const new_limits = {};
							limit_fields.forEach((df) => {
								new_limits[df.limit_type] = values[df.fieldname];
							});
							const args = {
								new_expiry_date: values.new_expiry_date,
								renewal_date: values.renewal_date,
								new_total_limit: values.new_total_limit,
								new_limits,
								remarks: values.remarks,
							};
							frm.call({ doc: frm.doc, method: "renew", args, freeze: true }).then(() => frm.reload_doc());
						},
						__("Renew"),
						__("Renew")
					);
				},
				__("Actions")
			);
			facility_action(frm, "close", __("Close Facility"), [
				date("closing_date", __("Date", null, "Treasury")),
				remarks,
			]);
		}
	},
});

function facility_action(frm, method, label, fields) {
	frm.add_custom_button(
		label,
		() => {
			frappe.prompt(
				fields,
				(values) => {
					frm.call({ doc: frm.doc, method, args: values, freeze: true }).then(() => frm.reload_doc());
				},
				label,
				label
			);
		},
		__("Actions")
	);
}
