// Copyright (c) 2026, Ahmed Abdelmageed and contributors
// For license information, please see license.txt

// Design 7f §4.1: the layout that prints for its bank account is the enabled default one.
frappe.listview_settings["Cheque Layout"] = {
	add_fields: ["is_default", "disabled", "default_language"],
	get_indicator(doc) {
		if (cint(doc.disabled)) {
			return [__("Disabled", null, "Cheque Layout"), "gray", "disabled,=,1"];
		}
		if (cint(doc.is_default)) {
			return [__("Default", null, "Cheque Layout"), "green", "is_default,=,1"];
		}
		return [__("Enabled", null, "Cheque Layout"), "blue", "disabled,=,0"];
	},
	formatters: {
		default_language(value) {
			return frappe.utils.escape_html(value ? __(value, null, "Cheque Layout") : "");
		},
	},
};
