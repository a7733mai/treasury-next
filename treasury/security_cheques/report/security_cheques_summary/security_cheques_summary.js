// Copyright (c) 2026, Ahmed Abdelmageed and contributors
// For license information, please see license.txt

// Design 7f §9. Column 1 (`group_label`) is the tree: Held from Others / Given to Others, then one row per party,
// purpose or status. Amounts are blank when 0 so the eye finds the money.
const SECURITY_SUMMARY_STATUSES = ["Created", "Received", "In Custody", "Issued", "Returned", "Encashed", "Lost", "Cancelled"];
const SECURITY_SUMMARY_AMOUNTS = ["amount", "expired", "range_1", "range_2", "range_3", "range_4", "range_5", "range_last", "no_expiry", "closed_amount"];
const SECURITY_SUMMARY_PARTY_ROUTES = { Customer: "customer", Supplier: "supplier", Employee: "employee" };

frappe.query_reports["Security Cheques Summary"] = {
	filters: [
		{
			fieldname: "company",
			label: __("Company"),
			fieldtype: "Link",
			options: "Company",
			default: frappe.defaults.get_user_default("Company"),
			reqd: 1,
		},
		{
			fieldname: "as_on",
			label: __("As On"),
			fieldtype: "Date",
			default: frappe.datetime.get_today(),
		},
		{
			fieldname: "direction",
			label: __("Direction"),
			fieldtype: "Select",
			options: [
				{ value: "", label: "" },
				{ value: "Incoming", label: __("Incoming") },
				{ value: "Outgoing", label: __("Outgoing") },
			],
		},
		{
			fieldname: "group_by",
			label: __("Group By", null, "Security Cheque"),
			fieldtype: "Select",
			options: [
				{ value: "Party", label: __("Party", null, "Security Cheques Summary") },
				{ value: "Purpose", label: __("Purpose", null, "Security Cheques Summary") },
				{ value: "Status", label: __("Status", null, "Security Cheques Summary") },
			],
			default: "Party",
		},
		{
			fieldname: "status",
			label: __("Status", null, "Treasury"),
			fieldtype: "MultiSelectList",
			description: __("Empty = live cheques (Received, In Custody, Issued)."),
			get_data: function (txt) {
				const search = (txt || "").toLowerCase();
				return SECURITY_SUMMARY_STATUSES.filter(
					(s) => !search || s.toLowerCase().includes(search) || __(s, null, "Security Cheque").includes(txt)
				).map((s) => ({ value: s, description: __(s, null, "Security Cheque") }));
			},
		},
		{
			fieldname: "party_type",
			label: __("Party Type"),
			fieldtype: "Select",
			options: [
				{ value: "", label: "" },
				{ value: "Customer", label: __("Customer") },
				{ value: "Supplier", label: __("Supplier") },
				{ value: "Employee", label: __("Employee", null, "Security Cheque") },
				{ value: "Other", label: __("Other", null, "Security Cheque") },
			],
			on_change: function () {
				frappe.query_report.set_filter_value("party", "");
			},
		},
		{
			fieldname: "party",
			label: __("Party", null, "Treasury Report"),
			fieldtype: "Dynamic Link",
			get_options: function () {
				const party_type = frappe.query_report.get_filter_value("party_type");
				if (!party_type || party_type === "Other") {
					frappe.throw(__("Please select Party Type first"));
				}
				return party_type;
			},
		},
		{
			fieldname: "purpose",
			label: __("Purpose", null, "Security Cheque"),
			fieldtype: "Select",
			options: [
				{ value: "", label: "" },
				{ value: "Tender", label: __("Tender", null, "Security Cheque") },
				{ value: "Contract Performance", label: __("Contract Performance", null, "Security Cheque") },
				{ value: "Advance Payment", label: __("Advance Payment", null, "Security Cheque") },
				{ value: "Rent", label: __("Rent", null, "Security Cheque") },
				{ value: "Employee Custody", label: __("Employee Custody", null, "Security Cheque") },
				{ value: "Facility Collateral", label: __("Facility Collateral", null, "Security Cheque") },
				{ value: "Other", label: __("Other", null, "Security Cheque") },
			],
		},
		{
			fieldname: "ageing_ranges",
			label: __("Ageing Ranges"),
			fieldtype: "Data",
			default: "30, 60, 90",
			description: __("Days to expiry, e.g. 30, 60, 90."),
		},
	],

	onload: function (report) {
		report.page.add_inner_button(__("Security Cheque Register"), function () {
			const options = { company: report.get_filter_value("company") };
			["direction", "party_type", "party"].forEach((fieldname) => {
				if (report.get_filter_value(fieldname)) {
					options[fieldname] = report.get_filter_value(fieldname);
				}
			});
			frappe.set_route("query-report", "Security Cheque Register", options);
		});
		report.page.add_inner_button(__("Security Cheque Movements"), function () {
			frappe.set_route("query-report", "Security Cheque Movements", {
				company: report.get_filter_value("company"),
				from_date: frappe.datetime.month_start(),
				to_date: frappe.datetime.get_today(),
			});
		});
	},

	formatter: function (value, row, column, data, default_formatter) {
		if (!data) {
			return default_formatter(value, row, column, data);
		}
		const esc = frappe.utils.escape_html;
		const field = column.fieldname;
		if (field === "group_label") {
			if (data.is_group) {
				return `<b>${esc(value || "")}</b>`;
			}
			if (data.group_by === "Purpose" || data.group_by === "Status") {
				return esc(value ? __(value, null, "Security Cheque") : "");
			}
			const route = SECURITY_SUMMARY_PARTY_ROUTES[data.party_type];
			return route && data.party
				? `<a href="/app/${route}/${encodeURIComponent(data.party)}">${esc(value || data.party)}</a>`
				: esc(value || "");
		}
		if (SECURITY_SUMMARY_AMOUNTS.includes(field) && !flt(value)) {
			return "";
		}
		let formatted = default_formatter(value, row, column, data);
		if (field === "expired") {
			return `<span style="color: var(--red-600); font-weight: bold">${formatted}</span>`;
		}
		if (field === "range_1") {
			return `<span style="color: var(--orange-600)">${formatted}</span>`;
		}
		if (field === "open_count") {
			// an open cheque given to others is an unlimited exposure
			return cint(value) > 0 && data.direction === "Outgoing"
				? `<span style="color: var(--orange-600); font-weight: bold">${formatted}</span>`
				: `<span class="text-muted">${formatted}</span>`;
		}
		if (["no_expiry", "closed_amount"].includes(field)) {
			return `<span class="text-muted">${formatted}</span>`;
		}
		return data.is_group ? `<b>${formatted}</b>` : formatted;
	},
};
