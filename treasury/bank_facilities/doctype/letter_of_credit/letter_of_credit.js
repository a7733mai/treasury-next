// Copyright (c) 2026, Ahmed Abdelmageed and contributors
// For license information, please see license.txt

// Business rules live in treasury/bank_facilities/lc.py; the buttons below only mirror them.
const LC_INDICATORS = {
	Draft: "gray",
	Opened: "blue",
	Amended: "blue",
	"Documents Received": "purple",
	Accepted: "orange",
	Settled: "green",
	Expired: "red",
	Cancelled: "darkgrey",
};

frappe.ui.form.on("Letter of Credit", {
	setup(frm) {
		lc_setup_queries(frm);
	},

	facility(frm) {
		if (!frm.doc.facility) {
			return;
		}
		frappe.db.get_doc("Bank Facility", frm.doc.facility).then((facility) => {
			const limit = (facility.limits || []).find((row) => row.limit_type === "Letter of Credit");
			frm.set_value("bank_account", facility.default_bank_account || frm.doc.bank_account);
			frm.set_value("cash_margin_percent", limit ? limit.cash_margin_percent : 0);
			frm.set_value("commission_rate", limit ? limit.commission_rate : 0);
			if (!frm.doc.commission_account && facility.commission_expense_account) {
				frm.set_value("commission_account", facility.commission_expense_account);
			}
		});
	},

	refresh(frm) {
		lc_translate_log(frm);
		if (frm.is_new() || frm.doc.docstatus === 0) {
			return;
		}
		frm.page.set_indicator(__(frm.doc.status, null, "Letter of Credit"), LC_INDICATORS[frm.doc.status] || "blue");
		if (frm.doc.docstatus !== 1 || frm.is_dirty() || !frappe.user.has_role("Treasury Manager")) {
			return;
		}
		const status = frm.doc.status;
		const today = (fieldname, label) => ({
			fieldname,
			label,
			fieldtype: "Date",
			reqd: 1,
			default: frappe.datetime.get_today(),
		});
		const money = (fieldname, label, extra) => ({
			fieldname,
			label,
			fieldtype: "Currency",
			options: frm.doc.currency,
			...extra,
		});
		const rate = {
			fieldname: "exchange_rate",
			label: __("Exchange Rate"),
			fieldtype: "Float",
			precision: 9,
			description: __("Foreign currency only. Default: the exchange rate of the date."),
		};
		const remarks = { fieldname: "remarks", label: __("Remarks"), fieldtype: "Small Text" };

		if (["Opened", "Amended"].includes(status)) {
			lc_action(frm, "amend", __("Amend", null, "Letter of Credit"), [
				today("amendment_date", __("Amendment Date")),
				money("new_amount", __("New Amount"), { default: frm.doc.current_amount }),
				{
					fieldname: "new_expiry_date",
					label: __("New Expiry Date"),
					fieldtype: "Date",
					default: frm.doc.effective_expiry_date,
				},
				money("charges", __("Amendment Charges")),
				rate,
				remarks,
			]);
			lc_action(frm, "receive_documents", __("Receive Documents"), [
				today("documents_date", __("Documents Date")),
				money("documents_amount", __("Documents Amount"), { reqd: 1, default: frm.doc.current_amount }),
				money("charges", __("Documents Charges")),
				rate,
				remarks,
			]);
		}
		if (status === "Documents Received" && frm.doc.lc_type === "Usance") {
			lc_action(frm, "accept", __("Accept", null, "Letter of Credit"), [
				today("acceptance_date", __("Acceptance Date")),
				{
					fieldname: "maturity_date",
					label: __("Maturity Date"),
					fieldtype: "Date",
					description: __("Default: the acceptance date plus the usance days."),
				},
				remarks,
			]);
		}
		if (
			(status === "Documents Received" && frm.doc.lc_type === "Sight") ||
			(status === "Accepted" && frm.doc.lc_type === "Usance")
		) {
			lc_action(frm, "settle", __("Settle", null, "Letter of Credit"), [
				today("settlement_date", __("Settlement Date")),
				money("amount", __("Amount"), { reqd: 1, default: frm.doc.documents_amount }),
				rate,
				{
					fieldname: "purchase_invoice",
					label: __("Purchase Invoice"),
					fieldtype: "Link",
					options: "Purchase Invoice",
					get_query: () => ({
						filters: {
							supplier: frm.doc.supplier,
							company: frm.doc.company,
							docstatus: 1,
							outstanding_amount: [">", 0],
						},
					}),
				},
				remarks,
			]);
		}
		if (status === "Expired" && flt(frm.doc.margin_held) > 0) {
			lc_action(frm, "release_margin", __("Release Margin"), [
				today("release_date", __("Release Date")),
				rate,
				remarks,
			]);
		}
	},
});

function lc_setup_queries(frm) {
	frm.set_query("facility", () => ({ filters: { status: "Active" } }));
	frm.set_query("bank_account", () => ({
		filters: { company: frm.doc.company, is_company_account: 1, bank: frm.doc.bank },
	}));
	frm.set_query("purchase_order", () => ({
		filters: { supplier: frm.doc.supplier, company: frm.doc.company, docstatus: 1 },
	}));
	frm.set_query("margin_account", () => ({
		filters: {
			company: frm.doc.company,
			is_group: 0,
			root_type: "Asset",
			account_currency: frm.doc.currency,
			account_type: ["not in", ["Payable", "Receivable", "Bank", "Cash"]],
		},
	}));
	["commission_account", "charges_account"].forEach((fieldname) => {
		frm.set_query(fieldname, () => ({
			filters: { company: frm.doc.company, is_group: 0, root_type: "Expense" },
		}));
	});
}

function lc_translate_log(frm) {
	// events and statuses are stored in English in the status log (Data fields): translate them on display
	["event", "from_status", "to_status"].forEach((fieldname) => {
		for (const name of [null, frm.doc.name]) {
			const df = frappe.meta.get_docfield("Bank Instrument Status Log", fieldname, name);
			if (df) {
				df.formatter = (value) =>
					frappe.utils.escape_html(value ? __(value, null, "Letter of Credit") : "");
			}
		}
	});
	frm.refresh_field("status_log");
}

function lc_action(frm, method, label, fields) {
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
