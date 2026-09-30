// Copyright (c) 2026, Ahmed Abdelmageed and contributors
// For license information, please see license.txt

// List scripts share the global scope: the names below are prefixed `BCC_LIST_`.
const BCC_LIST_CTX = "Bank Charge Category";

frappe.listview_settings["Bank Charge Category"] = {
	add_fields: ["disabled", "auto_select_proposals"],

	get_indicator(doc) {
		return doc.disabled
			? [__("Disabled", null, BCC_LIST_CTX), "gray", "disabled,=,1"]
			: [__("Enabled", null, BCC_LIST_CTX), "green", "disabled,=,0"];
	},
};
