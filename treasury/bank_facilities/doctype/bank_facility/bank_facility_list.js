// Copyright (c) 2026, Ahmed Abdelmageed and contributors
// For license information, please see license.txt

// Design 7g P7: the status colours of the form, each with a filter on the status.
frappe.listview_settings["Bank Facility"] = {
	add_fields: ["status", "lender_type", "is_islamic"],
	get_indicator(doc) {
		const colors = { Draft: "gray", Active: "green", Suspended: "orange", Expired: "red", Closed: "darkgrey" };
		return [__(doc.status, null, "Bank Facility"), colors[doc.status] || "blue", `status,=,${doc.status}`];
	},
};
