// Copyright (c) 2026, Ahmed Abdelmageed and contributors
// For license information, please see license.txt

// Allocation, the accrued / expensed interest split and the posting are computed on the server
// (treasury/bank_facilities/repayment.py, accounting.py); save the draft to see them.
frappe.ui.form.on("Facility Repayment", {
	setup(frm) {
		frm.set_query("drawdown", () => ({ filters: { docstatus: 1, repayment_status: "Open" } }));
		frm.set_query("bank_account", () => ({
			filters: { company: frm.doc.company, is_company_account: 1 },
		}));
		frm.set_query("penalty_account", () => ({
			filters: { company: frm.doc.company, is_group: 0, root_type: "Expense" },
		}));
	},

	drawdown(frm) {
		frm.set_value("bank_account", null);
		frm.set_value("principal_paid", 0);
		frm.set_value("interest_paid", 0);
		frm.clear_table("allocations");
		frm.refresh_field("allocations");
	},

	refresh(frm) {
		// vouchers are created by posting only: no "+" beside them in Connections
		frm.can_make_methods = { "Payment Entry": () => false, "Journal Entry": () => false };
		// server-rendered, escaped summary (treasury.utils.voucher_summary)
		frm.get_field("vouchers_html").$wrapper.html((frm.doc.__onload && frm.doc.__onload.voucher_summary) || "");
		if (frm.doc.docstatus === 0 && frm.doc.prepayment) {
			frm.set_intro(
				__(
					"The principal beyond the installments due up to the repayment date reduces the later installments; the tenor is kept."
				),
				"blue"
			);
		}
	},
});

// Low balance guard: the override is offered to Treasury Managers only (the server checks the role)
frappe.ui.form.on("Facility Repayment", {
	refresh(frm) {
		frm.toggle_display(
			"allow_low_balance",
			frm.doc.docstatus === 0 && frappe.user.has_role(["Treasury Manager", "System Manager"])
		);
	},
});
