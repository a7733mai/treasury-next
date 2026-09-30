// Copyright (c) 2026, Ahmed Abdelmageed and contributors
// For license information, please see license.txt

// List scripts share the global scope: the names below are prefixed `TAL_LIST_`.
const TAL_LIST_CTX = "Treasury Alert Log";
const TAL_LIST_LEVEL_CTX = "Bank Balance Outlook";

frappe.listview_settings["Treasury Alert Log"] = {
	add_fields: ["alert_type", "level", "is_escalation", "currency", "balance"],

	get_indicator(doc) {
		if (doc.alert_type === "Override") {
			return [__("Override", null, TAL_LIST_CTX), "purple", "alert_type,=,Override"];
		}
		if (doc.alert_type === "Digest") {
			return [__("Digest", null, TAL_LIST_CTX), "blue", "alert_type,=,Digest"];
		}
		if (doc.level === "Critical" && doc.is_escalation) {
			return [__("Worse: Critical", null, TAL_LIST_LEVEL_CTX), "red", "is_escalation,=,1"];
		}
		if (doc.level === "Critical") {
			return [__("Critical", null, TAL_LIST_CTX), "red", "level,=,Critical"];
		}
		if (doc.level === "Warning") {
			return [__("Warning", null, TAL_LIST_CTX), "orange", "level,=,Warning"];
		}
		return [__(doc.alert_type || "", null, TAL_LIST_CTX), "gray", `alert_type,=,${doc.alert_type}`];
	},

	formatters: {
		balance(value, df, doc) {
			if (doc.alert_type === "Digest") {
				return "";
			}
			const text = frappe.utils.escape_html(format_currency(value, doc.currency));
			return flt(value) < 0 ? `<span class="text-danger"><bdi>${text}</bdi></span>` : `<bdi>${text}</bdi>`;
		},
	},
};
