// Copyright (c) 2026, Ahmed Abdelmageed and contributors
// For license information, please see license.txt

frappe.listview_settings["PDC Cheque"] = {
	add_fields: ["direction", "operational_status", "accounting_status", "effective_due_date"],

	get_indicator(doc) {
		const settled = [
			"Cleared",
			"Bounced",
			"Returned",
			"Endorsed",
			"Discounted",
			"Replaced",
			"Cancelled",
		];
		const today = frappe.datetime.get_today();
		if (
			!settled.includes(doc.operational_status) &&
			doc.effective_due_date &&
			doc.effective_due_date < today
		) {
			// "|" separates filters; a "not in" value is a comma-separated list
			return [
				__("Overdue", null, "Treasury"),
				"red",
				`effective_due_date,<,${today}|operational_status,not in,${settled.join(",")}`,
			];
		}

		const colors = {
			Created: "gray",
			Delivered: "blue",
			Received: "blue",
			Deposited: "purple",
			Cleared: "green",
			Bounced: "red",
			Returned: "orange",
			Endorsed: "cyan",
			Discounted: "cyan",
			Replaced: "light-gray",
			Cancelled: "darkgrey",
		};
		return [
			__(doc.operational_status),
			colors[doc.operational_status] || "gray",
			"operational_status,=," + doc.operational_status,
		];
	},
};
