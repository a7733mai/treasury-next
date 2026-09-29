// Copyright (c) 2026, Ahmed Abdelmageed and contributors
// For license information, please see license.txt

// Business rules live in treasury/bank_facilities/lg.py; the buttons below only mirror them.
const LG_OPEN = ["Issued", "Extended", "Reduced"];
const LG_INDICATORS = {
	Draft: "gray",
	Issued: "blue",
	Extended: "blue",
	Reduced: "cyan",
	Released: "green",
	Claimed: "red",
	Expired: "orange",
	Cancelled: "darkgrey",
};

frappe.ui.form.on("Letter of Guarantee", {
	setup(frm) {
		treasury_instrument_setup(frm, "Letter of Guarantee");
		frm.set_query("reference_doctype", () => ({
			filters: {
				name: ["in", ["Project", "Contract", "Sales Order", "Purchase Order", "Quotation"]],
			},
		}));
	},

	facility(frm) {
		treasury_instrument_limit_defaults(frm, "Letter of Guarantee");
	},

	beneficiary_type(frm) {
		frm.set_value("beneficiary", null);
	},

	refresh(frm) {
		treasury_instrument_translate_log(frm, "Letter of Guarantee");
		if (frm.doc.docstatus === 0 || frm.is_new()) {
			return;
		}
		frm.page.set_indicator(
			__(frm.doc.status, null, "Letter of Guarantee"),
			LG_INDICATORS[frm.doc.status] || "blue"
		);
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
		const remarks = { fieldname: "remarks", label: __("Remarks"), fieldtype: "Small Text" };

		if ([...LG_OPEN, "Expired"].includes(status)) {
			treasury_instrument_action(frm, "extend_validity", __("Extend", null, "Letter of Guarantee"), [
				{
					fieldname: "new_expiry_date",
					label: __("New Expiry Date"),
					fieldtype: "Date",
					reqd: 1,
					default: frm.doc.effective_expiry_date,
				},
				today("extension_date", __("Extension Date")),
				money("extra_commission", __("Extension Commission")),
				remarks,
			]);
		}
		if (LG_OPEN.includes(status)) {
			treasury_instrument_action(frm, "reduce", __("Reduce", null, "Letter of Guarantee"), [
				money("new_amount", __("New Amount"), { reqd: 1 }),
				today("reduction_date", __("Reduction Date")),
				remarks,
			]);
		}
		if ([...LG_OPEN, "Expired"].includes(status)) {
			treasury_instrument_action(frm, "release", __("Release", null, "Letter of Guarantee"), [
				today("release_date", __("Release Date")),
				remarks,
			]);
			treasury_instrument_action(frm, "claim", __("Claim (Liquidation)"), [
				money("amount", __("Claim Amount"), { reqd: 1, default: frm.doc.open_amount }),
				{
					fieldname: "counter_account",
					label: __("Account to Debit"),
					fieldtype: "Link",
					options: "Account",
					reqd: 1,
					get_query: () => ({ filters: { company: frm.doc.company, is_group: 0 } }),
				},
				{
					fieldname: "party_type",
					label: __("Party Type"),
					fieldtype: "Select",
					options: ["", "Customer", "Supplier"],
					description: __("Only for a Payable / Receivable account."),
				},
				{
					fieldname: "party",
					label: __("Party"),
					fieldtype: "Dynamic Link",
					options: "party_type",
				},
				today("claim_date", __("Claim Date")),
				remarks,
			]);
		}
		if (status === "Claimed" && flt(frm.doc.claim_outstanding) > 0) {
			treasury_instrument_action(frm, "repay_claim", __("Repay Claim"), [
				money("amount", __("Amount"), { reqd: 1, default: frm.doc.claim_outstanding }),
				today("repayment_date", __("Repayment Date")),
				remarks,
			]);
		}
	},
});

// ---------------------------------------------------------------------------
// Shared by the Letter of Guarantee and Letter of Credit forms (each loads its own copy).
// ---------------------------------------------------------------------------

function treasury_instrument_setup(frm, doctype) {
	frm.set_query("facility", () => ({ filters: { status: "Active" } }));
	frm.set_query("bank_account", () => ({
		filters: { company: frm.doc.company, is_company_account: 1, bank: frm.doc.bank },
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
		if (frappe.meta.has_field(doctype, fieldname)) {
			frm.set_query(fieldname, () => ({
				filters: { company: frm.doc.company, is_group: 0, root_type: "Expense" },
			}));
		}
	});
}

function treasury_instrument_limit_defaults(frm, limit_type) {
	if (!frm.doc.facility) {
		return;
	}
	frappe.db.get_doc("Bank Facility", frm.doc.facility).then((facility) => {
		const limit = (facility.limits || []).find((row) => row.limit_type === limit_type);
		frm.set_value("bank_account", facility.default_bank_account || frm.doc.bank_account);
		frm.set_value("cash_margin_percent", limit ? limit.cash_margin_percent : 0);
		frm.set_value("commission_rate", limit ? limit.commission_rate : 0);
		if (!frm.doc.commission_account && facility.commission_expense_account) {
			frm.set_value("commission_account", facility.commission_expense_account);
		}
	});
}

function treasury_instrument_translate_log(frm, doctype) {
	// events and statuses are stored in English in the status log (Data fields): translate them on display
	["event", "from_status", "to_status"].forEach((fieldname) => {
		for (const name of [null, frm.doc.name]) {
			const df = frappe.meta.get_docfield("Bank Instrument Status Log", fieldname, name);
			if (df) {
				df.formatter = (value) => frappe.utils.escape_html(value ? __(value, null, doctype) : "");
			}
		}
	});
	frm.refresh_field("status_log");
}

function treasury_instrument_action(frm, method, label, fields) {
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
