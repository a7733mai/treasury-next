// Copyright (c) 2026, Ahmed Abdelmageed and contributors
// For license information, please see license.txt

// Business rules live in treasury/bank_facilities/facility.py and bank_facility.py; the form only mirrors them.
// Form scripts share the global scope: names are prefixed `BF_` / `bf_`.
const BF_STATUS_COLORS = { Draft: "gray", Active: "green", Suspended: "orange", Expired: "red", Closed: "darkgrey" };
// mirrors treasury.bank_facilities.lenders
const BF_ISLAMIC_TYPES = ["Murabaha", "Tawarruq", "Ijarah", "Istisna'a"];
const BF_FINANCE_COMPANY_TYPES = [
	"Short Term Loan",
	"Long Term Loan",
	"Murabaha",
	"Tawarruq",
	"Ijarah",
	"Istisna'a",
	"Invoice Discounting",
	"Other",
];
const BF_LIMIT_TYPES = [
	"Overdraft",
	"Short Term Loan",
	"Long Term Loan",
	"Letter of Guarantee",
	"Letter of Credit",
	"Trust Receipt",
	"Cheque Discounting",
	"Invoice Discounting",
	"Murabaha",
	"Tawarruq",
	"Ijarah",
	"Istisna'a",
	"Other",
];

frappe.ui.form.on("Bank Facility", {
	setup(frm) {
		// a finance company's money may sit at any of the company's banks (design 7g §2.1)
		frm.set_query("default_bank_account", () => {
			const filters = { company: frm.doc.company, is_company_account: 1 };
			if (frm.doc.lender_type !== "Finance Company") {
				filters.bank = frm.doc.bank;
			}
			return { filters };
		});
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
		frm.set_query("late_payment_charity_account", () => account_query("Expense", frm.company_currency));
		frm.set_query("charity_payable_account", () => account_query("Liability", frm.company_currency));
		frm.set_query("lender", () => ({ filters: { disabled: 0 } }));
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
		frm.events.set_lender_fields(frm);
		frm.events.set_terms(frm);
		if (!frm.is_new()) {
			frm.page.set_indicator(
				__(frm.doc.status, null, "Bank Facility"),
				BF_STATUS_COLORS[frm.doc.status] || "blue"
			);
			frm.events.add_indicators(frm);
			frm.events.add_actions(frm);
		}
	},

	lender_type(frm) {
		frm.events.set_lender_fields(frm);
		frm.events.clear_unmatched_bank_account(frm);
	},

	bank(frm) {
		frm.events.clear_unmatched_bank_account(frm);
	},

	is_islamic(frm) {
		frm.events.set_terms(frm);
		frm.events.lock_fields(frm);
	},

	lock_fields(frm) {
		// mirrors BankFacility.validate_locked_fields: key terms move through Renew only once activated
		const locked = !frm.is_new() && frm.doc.status !== "Draft";
		// (an empty lender of a bank facility is filled by the server on first use, never by the form:
		// lenders.get_lender_supplier)
		["company", "bank", "currency", "total_limit", "start_date", "expiry_date", "lender_type", "lender"].forEach(
			(fieldname) => frm.set_df_property(fieldname, "read_only", locked ? 1 : 0)
		);
		// mirrors validate_lender: an Islamic sub-limit makes the facility Islamic
		frm.set_df_property("is_islamic", "read_only", locked || bf_has_islamic_limit(frm) ? 1 : 0);
	},

	set_lender_fields(frm) {
		// design 7g §2.1: who lends decides the fields; a finance company has no bank (P4)
		const finance = frm.doc.lender_type === "Finance Company";
		frm.set_df_property(
			"lender",
			"description",
			finance
				? __("The financier as a Supplier (e.g. Al Jazeera Finance). For a bank, created from the Bank on first use.")
				: __("Leave empty: the bank is added as a Supplier on its first Payment Entry.")
		);
		frm.toggle_display("lender_name", !frm.is_new() && !!frm.doc.lender_name);
		// a finance company grants loans and Islamic financing only (lenders.FINANCE_COMPANY_TYPES)
		const options = finance ? BF_FINANCE_COMPANY_TYPES : BF_LIMIT_TYPES;
		const grid = frm.fields_dict.limits && frm.fields_dict.limits.grid;
		grid && grid.update_docfield_property("limit_type", "options", options.join("\n"));
	},

	clear_unmatched_bank_account(frm) {
		const account = frm.doc.default_bank_account;
		if (!account || frm.doc.lender_type === "Finance Company") {
			return;
		}
		frappe.db.get_value("Bank Account", account, "bank").then((r) => {
			const bank = r.message && r.message.bank;
			if (frm.doc.default_bank_account === account && bank !== frm.doc.bank) {
				frm.set_value("default_bank_account", "");
			}
		});
	},

	set_terms(frm) {
		// design 7g §1: one word per facility (Interest / Profit)
		const terms = treasury.facility.terms(frm.doc.is_islamic);
		frm.set_df_property("interest_expense_account", "label", terms.interest_expense_account);
		frm.set_df_property("accrued_interest_account", "label", terms.accrued_interest_account);
		const grid = frm.fields_dict.limits && frm.fields_dict.limits.grid;
		grid && grid.update_docfield_property("interest_rate", "label", terms.interest_rate);
		frm.refresh_field("limits");
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

	add_indicators(frm) {
		// design 7g §2.3: who lends, the vocabulary in use (P1) and the collateral held by the lender (P5)
		const esc = frappe.utils.escape_html;
		const finance = frm.doc.lender_type === "Finance Company";
		const lender = frm.doc.lender_name || frm.doc.bank || "";
		frm.dashboard.add_indicator(
			esc(finance ? __("Finance Company · {0}", [lender]) : __("Bank · {0}", [lender])),
			finance ? "purple" : "blue"
		);
		if (cint(frm.doc.is_islamic)) {
			frm.dashboard.add_indicator(esc(__("Islamic")), "cyan");
		}
		const security = frm.doc.__onload && frm.doc.__onload.security_summary;
		if (security && security.count) {
			frm.dashboard.add_indicator(
				esc(
					__("{0} security cheque(s) · {1}", [
						security.count,
						format_currency(security.total, security.currency || frm.doc.currency),
					])
				),
				"gray"
			);
		}
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
		const has_invoice_discounting = (frm.doc.limits || []).some((row) => row.limit_type === "Invoice Discounting");

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
		if (!["Draft", "Closed"].includes(status) && frappe.model.can_create("Security Cheque")) {
			// our cheque to the lender as collateral: everything is defaulted (the 7c mapper pattern, no dialog)
			frm.add_custom_button(__("Security Cheque"), () => bf_make_security_cheque(frm), __("Create"));
		}
		if (status === "Active" && has_invoice_discounting && frappe.model.can_create("Invoice Discounting")) {
			frm.add_custom_button(
				__("Invoice Discounting"),
				() =>
					frappe.new_doc("Invoice Discounting", {
						company: frm.doc.company,
						treasury_facility: frm.doc.name,
					}),
				__("Create")
			);
		}
		if (has_invoice_discounting && frappe.model.can_read("Invoice Discounting")) {
			// design 7g P8
			frm.add_custom_button(
				__("Invoice Discountings"),
				() => frappe.set_route("List", "Invoice Discounting", { treasury_facility: frm.doc.name }),
				__("View")
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

// ------------------------------------------------------------------------------------------ sub-limits
frappe.ui.form.on("Bank Facility Limit", {
	form_render(frm, cdt, cdn) {
		bf_set_limit_hint(frm, cdn);
	},

	limit_type(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		bf_set_limit_hint(frm, cdn);
		// mirrors validate_lender: an Islamic sub-limit makes the facility Islamic (the labels switch now)
		if (BF_ISLAMIC_TYPES.includes(row.limit_type) && !cint(frm.doc.is_islamic)) {
			frm.set_value("is_islamic", 1);
			frappe.show_alert({
				message: frappe.utils.escape_html(__("Islamic sub-limit: the facility uses Profit instead of Interest.")),
				indicator: "blue",
			});
		}
		frm.events.lock_fields(frm);
	},

	limits_remove(frm) {
		frm.events.lock_fields(frm);
	},
});

function bf_has_islamic_limit(frm) {
	return (frm.doc.limits || []).some((row) => BF_ISLAMIC_TYPES.includes(row.limit_type));
}

function bf_set_limit_hint(frm, cdn) {
	// design 7g §2.2: what each Islamic / trade sub-limit means, under the type in the row form
	const row = locals["Bank Facility Limit"][cdn];
	const hints = {
		Ijarah: __("Leasing: the asset is financed; rentals repay principal and profit."),
		"Istisna'a": __(
			"Manufacturing / construction: the drawdown is disbursed in stages. Undisbursed stages still use the limit."
		),
		"Trust Receipt": __("Created automatically when a Letter of Credit is settled with Finance by Trust Receipt."),
		"Invoice Discounting": __("Used by ERPNext Invoice Discounting documents linked to this facility."),
	};
	const grid_row = frm.fields_dict.limits.grid.grid_rows_by_docname[cdn];
	const field = grid_row && grid_row.grid_form && grid_row.grid_form.fields_dict.limit_type;
	if (field) {
		// the help box directly: set_description() keeps an old text when the new one is empty
		const hint = (row && hints[row.limit_type]) || "";
		field.$wrapper.find(".help-box").html(frappe.utils.escape_html(hint)).toggleClass("hide", !hint);
	}
}

function bf_make_security_cheque(frm) {
	frappe
		.xcall("treasury.security_cheques.api.make_security_cheque", {
			source_doctype: frm.doc.doctype,
			source_name: frm.doc.name,
		})
		.then((doc) => {
			const synced = frappe.model.sync(doc)[0];
			frappe.set_route("Form", synced.doctype, synced.name).then(() => {
				frappe.show_alert({
					message: frappe.utils.escape_html(
						__("Security cheque to {0} for {1}. Enter the cheque number and save.", [
							frm.doc.lender_name || frm.doc.bank || "",
							frm.doc.facility_name || frm.doc.name,
						])
					),
					indicator: "blue",
				});
			});
		});
}

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

// Design 7g §1: the Interest / Profit wording of a facility, shared by the facility forms (the same helper is
// in each of them: there are no bundles; defined once by whichever loads first). The strings are those of
// treasury.bank_facilities.lenders.terms().
frappe.provide("treasury.facility");
if (!treasury.facility.terms) {
	treasury.facility.terms = function (is_islamic, limit_type) {
		const islamic = !!cint(is_islamic);
		const rental = islamic && limit_type === "Ijarah";
		return {
			interest: islamic ? __("Profit") : __("Interest"),
			interest_rate: islamic ? __("Profit Rate (% p.a.)") : __("Interest Rate (% p.a.)"),
			total_interest: islamic ? __("Total Profit") : __("Total Interest"),
			interest_paid: islamic ? __("Profit Paid") : __("Interest Paid"),
			paid_interest: islamic ? __("Paid Profit") : __("Paid Interest"),
			accrued_interest: islamic ? __("Accrued Profit") : __("Accrued Interest"),
			accrued_interest_account: islamic ? __("Accrued Profit Account") : __("Accrued Interest Account"),
			interest_expense_account: islamic ? __("Profit Expense Account") : __("Interest Expense Account"),
			accrued_interest_cleared: islamic ? __("Accrued Profit Cleared") : __("Accrued Interest Cleared"),
			interest_expensed: islamic ? __("Profit Expensed") : __("Interest Expensed"),
			installment: rental ? __("Rental") : __("Installment"),
			total: rental ? __("Rental") : __("Total", null, "Treasury"),
			penalty: islamic ? __("Late Payment Charge") : __("Penalty"),
		};
	};
}
