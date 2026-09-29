// Copyright (c) 2026, Ahmed Abdelmageed and contributors
// For license information, please see license.txt

frappe.listview_settings["Cheque Batch"] = {
	add_fields: ["status", "direction"],

	get_indicator(doc) {
		const colors = {
			Draft: "red",
			Submitted: "gray",
			"Partially Delivered": "orange",
			Delivered: "blue",
			"Partially Received": "orange",
			Received: "blue",
			"Partially Cleared": "purple",
			Cleared: "green",
			Cancelled: "darkgrey",
		};
		// context: generic words ("Partially Delivered") keep ERPNext's own translation elsewhere
		return [__(doc.status, null, "Cheque Batch"), colors[doc.status] || "gray", "status,=," + doc.status];
	},
};
