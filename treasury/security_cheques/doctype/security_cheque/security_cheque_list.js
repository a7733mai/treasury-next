// Copyright (c) 2026, Ahmed Abdelmageed and contributors
// For license information, please see license.txt

frappe.listview_settings["Security Cheque"] = {
	add_fields: ["status", "expiry_date", "direction"],
	get_indicator(doc) {
		const live = ["Received", "In Custody", "Issued"].includes(doc.status);
		if (live && doc.expiry_date && doc.expiry_date < frappe.datetime.get_today()) {
			return [__("Expired"), "red", "status,in,Received|In Custody|Issued"];
		}
		const colors = {
			Created: "gray",
			Received: "blue",
			"In Custody": "blue",
			Issued: "orange",
			Returned: "green",
			Encashed: "purple",
			Lost: "red",
			Cancelled: "gray",
		};
		return [__(doc.status, null, "Security Cheque"), colors[doc.status] || "gray", `status,=,${doc.status}`];
	},
};
