// Copyright (c) 2026, Ahmed Abdelmageed and contributors
// For license information, please see license.txt

// Matching and clearing rules live in pdc_bank_clearing.py, treasury/pdc/lifecycle.py and
// treasury/bank_transfers/lifecycle.py. Form scripts share the global scope: names are prefixed `PBC_` / `pbc_`.
const PBC_MATCH = "PDC Clearing Match";
const PBC_QUALITY_COLORS = { Strong: "green", Weak: "orange", Keyword: "blue" };

frappe.ui.form.on("PDC Bank Clearing", {
	setup(frm) {
		frm.set_query("bank_account", () => ({
			filters: { company: frm.doc.company, is_company_account: 1 },
		}));
		frm.set_query("bank_charge_category", "matches", () => ({ filters: { disabled: 0 } }));
	},

	onload(frm) {
		if (!pbc_apply_route_options(frm) && !frm.doc.company) {
			frm.set_value("company", frappe.defaults.get_user_default("Company"));
		}
	},

	refresh(frm) {
		frm.disable_save();
		frm.set_df_property("matches", "cannot_add_rows", 1);
		frm.set_intro(
			__(
				"Matches bank statement lines with open cheques, bank transfers and bank charges, and proposes bank charges for unmatched lines."
			),
			"blue"
		);
		pbc_set_formatters(frm);
		// "Match Bank Statement" on a Bank Transfer opens this (single) form with its company and bank
		pbc_apply_route_options(frm);
	},

	company(frm) {
		frm.set_value("bank_account", "");
	},

	get_suggestions_button(frm) {
		frm.call({ doc: frm.doc, method: "get_suggestions", freeze: true }).then((r) => {
			frm.refresh_field("matches");
			if (!r.message) {
				frappe.show_alert({ message: __("No matching bank transactions found."), indicator: "orange" });
			}
		});
	},

	clear_selected_button(frm) {
		const selected = (frm.doc.matches || []).filter((row) => row.selected);
		if (!selected.length) {
			frappe.msgprint(__("Select the matches to clear."));
			return;
		}
		const is_charge = (row) => row.reference_doctype === "Bank Charge";
		const transfers = selected.filter((row) => !is_charge(row) && row.bank_transfer).length;
		const to_reconcile = selected.filter((row) => is_charge(row) && row.bank_charge).length;
		const to_create = selected.filter((row) => is_charge(row) && !row.bank_charge).length;
		const cheques = selected.length - transfers - to_reconcile - to_create;
		let message;
		if (to_reconcile || to_create) {
			message = __(
				"Clear {0} cheque(s), {1} bank transfer(s), reconcile {2} bank charge(s) and create {3} bank charge(s)?",
				[cheques, transfers, to_reconcile, to_create]
			);
		} else if (transfers) {
			message = __("Clear {0} cheque(s) and {1} bank transfer(s) and reconcile their bank transactions?", [
				cheques,
				transfers,
			]);
		} else {
			message = __("Clear {0} cheque(s) and reconcile their bank transactions?", [cheques]);
		}
		frappe.confirm(message, () => {
			frm.call({ doc: frm.doc, method: "clear_selected", freeze: true }).then(() => {
				frm.refresh_field("matches");
			});
		});
	},
});

// the Charge Category is chosen only on a keyword proposal (a Bank Charge to create)
frappe.ui.form.on(PBC_MATCH, {
	form_render(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		const grid_row = frm.fields_dict.matches.grid.grid_rows_by_docname[cdn];
		if (grid_row) {
			grid_row.toggle_editable("bank_charge_category", row.match_quality === "Keyword");
		}
	},
});

function pbc_apply_route_options(frm) {
	const options = frappe.route_options;
	if (!options || !(options.company || options.bank_account)) {
		return false;
	}
	frappe.route_options = null;
	// the company trigger clears the bank account: set the bank once it ran
	Promise.resolve(options.company ? frm.set_value("company", options.company) : null)
		.then(() => options.bank_account && frm.set_value("bank_account", options.bank_account))
		.then(() => frm.doc.company && frm.doc.bank_account && frm.trigger("get_suggestions_button"));
	return true;
}

function pbc_set_formatters(frm) {
	const esc = frappe.utils.escape_html;
	const formatters = {
		direction: (value) => esc(value ? __(value) : ""),
		match_quality: (value) =>
			value
				? `<span class="indicator-pill ${PBC_QUALITY_COLORS[value] || "gray"}">${esc(
						__(value, null, PBC_MATCH)
				  )}</span>`
				: "",
		bank_transfer: (value) =>
			value ? `<a href="/app/bank-transfer/${encodeURIComponent(value)}"><bdi>${esc(value)}</bdi></a>` : "",
		bank_charge: (value) =>
			value ? `<a href="/app/bank-charge/${encodeURIComponent(value)}"><bdi>${esc(value)}</bdi></a>` : "",
		bank_charge_category: (value) => esc(value ? __(value) : ""),
		reference_doctype: (value) => esc(value ? __(value, null, PBC_MATCH) : ""),
	};
	Object.entries(formatters).forEach(([fieldname, formatter]) => {
		for (const name of [null, frm.doc.name]) {
			const df = frappe.meta.get_docfield(PBC_MATCH, fieldname, name);
			if (df) {
				df.formatter = formatter;
			}
		}
	});
	frm.refresh_field("matches");
}
