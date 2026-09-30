// Copyright (c) 2026, Ahmed Abdelmageed and contributors
// For license information, please see license.txt

// Treasury Dashboard (spec 7h, 3.2): one read-only call, sections the user may read only. Plain page script
// (no bundle): Page.load_assets serves this file and treasury_dashboard.css.

const TD_CTX = "Treasury Dashboard";
const TD_STORAGE_KEY = "treasury_dashboard_filters";

frappe.provide("treasury");

frappe.pages["treasury-dashboard"].on_page_load = (wrapper) => {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("Treasury Dashboard"),
		single_column: true,
	});
	frappe.breadcrumbs.add("PDC");
	wrapper.treasury_dashboard = new treasury.TreasuryDashboard(page, wrapper);
};

frappe.pages["treasury-dashboard"].on_page_show = (wrapper) => {
	const dashboard = wrapper.treasury_dashboard;
	if (dashboard && frappe.route_options && frappe.route_options.company) {
		dashboard.company_field.set_value(frappe.route_options.company);
		frappe.route_options = null;
	}
};

treasury.TreasuryDashboard = class TreasuryDashboard {
	constructor(page, wrapper) {
		this.page = page;
		this.wrapper = $(wrapper);
		this.body = $('<div class="treasury-dashboard"></div>').appendTo(this.page.main);
		this.status = $('<div class="td-updated text-muted small"></div>').appendTo(this.body);
		this.content = $('<div class="td-content"></div>').appendTo(this.body);
		this.data = null;
		this.make_filters();
		this.make_actions();
		this.refresh();
	}

	stored_filters() {
		try {
			return JSON.parse(localStorage.getItem(TD_STORAGE_KEY) || "{}") || {};
		} catch (e) {
			return {};
		}
	}

	store_filters() {
		try {
			localStorage.setItem(TD_STORAGE_KEY, JSON.stringify(this.filters()));
		} catch (e) {
			// private window / blocked storage: the filters are simply not remembered
		}
	}

	make_filters() {
		const stored = this.stored_filters();
		const default_company =
			(frappe.route_options && frappe.route_options.company) ||
			stored.company ||
			frappe.defaults.get_user_default("Company") ||
			frappe.defaults.get_default("company");
		const reload = frappe.utils.debounce(() => this.refresh(), 300);

		this.company_field = this.page.add_field({
			fieldname: "company",
			label: __("Company"),
			fieldtype: "Link",
			options: "Company",
			reqd: 1,
			default: default_company,
			change: () => {
				if (this.company_field.get_value() !== this.last_company) {
					this.bank_field.set_value([]);
				}
				reload();
			},
		});
		this.bank_field = this.page.add_field({
			fieldname: "bank_accounts",
			label: __("Bank Account", null, "Treasury"),
			fieldtype: "MultiSelectList",
			get_data: (txt) =>
				frappe.db.get_link_options("Bank Account", txt, {
					company: this.company_field.get_value(),
					is_company_account: 1,
					disabled: 0,
				}),
			change: reload,
		});
		this.horizon_field = this.page.add_field({
			fieldname: "horizon",
			label: __("Horizon", null, TD_CTX),
			fieldtype: "Select",
			options: [7, 14, 30].map((n) => ({ value: String(n), label: __("{0} days", [n], TD_CTX) })),
			default: String(stored.horizon || 7),
			change: reload,
		});
		if (stored.company === default_company && Array.isArray(stored.bank_accounts)) {
			this.bank_field.set_value(stored.bank_accounts);
		}
		frappe.route_options = null;
	}

	make_actions() {
		this.page.set_primary_action(__("Refresh"), () => this.refresh(), "refresh");
		this.page.add_inner_button(__("Bank Balance Outlook"), () =>
			frappe.set_route("query-report", "Bank Balance Outlook", { company: this.filters().company })
		);
	}

	filters() {
		return {
			company: this.company_field.get_value(),
			bank_accounts: this.bank_field.get_value() || [],
			horizon: this.horizon_field.get_value() || "7",
		};
	}

	refresh() {
		const filters = this.filters();
		if (!filters.company) {
			this.content.html(this.empty(__("Select a company.", null, TD_CTX)));
			return;
		}
		this.last_company = filters.company;
		this.store_filters();
		this.body.addClass("td-loading");
		frappe
			.xcall("treasury.pdc.page.treasury_dashboard.treasury_dashboard.get_dashboard_data", {
				company: filters.company,
				bank_accounts: JSON.stringify(filters.bank_accounts),
				horizon: filters.horizon,
			})
			.then((data) => {
				this.data = data;
				this.render(data);
				this.status.text(__("Updated {0}", [frappe.datetime.now_time()]));
			})
			.finally(() => this.body.removeClass("td-loading"));
	}

	// ------------------------------------------------------------------ helpers

	money(value, currency) {
		return `<bdi>${format_currency(value || 0, currency || this.data.company_currency)}</bdi>`;
	}

	esc(value) {
		return frappe.utils.escape_html(value == null ? "" : String(value));
	}

	empty(message) {
		return `<div class="td-empty text-muted">${this.esc(message)}</div>`;
	}

	section(key, title, body, company_wide) {
		const note =
			company_wide && this.data.bank_accounts.length
				? `<div class="td-note text-muted small">${this.esc(__("All bank accounts of the company"))}</div>`
				: "";
		return `<div class="td-section" data-section="${key}">
			<div class="td-section-title">${this.esc(title)}</div>${note}${body}</div>`;
	}

	card(label, value, drill, extra) {
		const index = this.drills.push(drill) - 1;
		return `<button type="button" class="td-card" data-drill="${index}">
			<div class="td-card-label">${this.esc(label)}</div>
			<div class="td-card-value">${value}</div>
			${extra ? `<div class="td-card-extra text-muted small">${extra}</div>` : ""}
		</button>`;
	}

	chart(el, chart) {
		if (!chart || !chart.data || !chart.data.labels.length || !chart.data.datasets.length) {
			$(el).html(this.empty(__("Nothing to show.")));
			return;
		}
		const currency = chart.currency || this.data.company_currency;
		new frappe.Chart(el, {
			data: chart.data,
			type: chart.type,
			height: 240,
			barOptions: chart.barOptions,
			tooltipOptions: { formatTooltipY: (v) => format_currency(v, currency) },
			axisOptions: { xIsSeries: 1 },
		});
	}

	// ------------------------------------------------------------------ rendering

	render(data) {
		this.drills = [];
		this.page.remove_inner_button(__("Setup Checklist"));
		if (data.setup) {
			const setup = data.setup;
			this.page.add_inner_button(__("Setup Checklist"), () =>
				frappe.set_route(...setup.route, setup.route_options || {})
			);
		}
		const s = data.sections || {};
		const parts = [this.render_setup(data.setup)];
		if (s.positions) parts.push(this.render_positions(s.positions));
		if (s.cheques) parts.push(this.render_cheques(s.cheques));
		if (s.transfers) parts.push(this.render_transfers(s.transfers));
		if (s.charges) parts.push(this.render_charges(s.charges));
		if (s.security) parts.push(this.render_security(s.security));
		if (s.facilities || s.installments) parts.push(this.render_facilities(s.facilities, s.installments));
		if (s.guarantees || s.credits) parts.push(this.render_instruments(s.guarantees, s.credits));
		if (s.positions) {
			parts.push(
				this.section("maturities", __("Maturities (30 Days)"), '<div class="td-chart td-chart-maturities"></div>')
			);
		}
		this.content.html(parts.join(""));
		this.content.find("[data-drill]").on("click", (e) => {
			const drill = this.drills[cint($(e.currentTarget).attr("data-drill"))];
			if (drill && drill.route) frappe.set_route(...drill.route, drill.route_options || {});
		});
		if (s.positions) {
			this.chart(this.content.find(".td-chart-curve")[0], s.positions.charts.balance_curve);
			this.chart(this.content.find(".td-chart-flows")[0], s.positions.charts.cash_flows);
			this.chart(this.content.find(".td-chart-maturities")[0], s.positions.charts.maturities);
		}
		if (s.charges) this.chart(this.content.find(".td-chart-charges")[0], s.charges.by_category);
	}

	render_setup(setup) {
		if (!setup || !setup.missing) return "";
		const cls = setup.required_missing ? "td-banner-red" : "td-banner-orange";
		const index = this.drills.push({ route: setup.route, route_options: setup.route_options }) - 1;
		const text = setup.exists
			? __("{0} setup items need attention.", [setup.missing])
			: __("Treasury Company Settings are missing for this company.");
		return `<div class="td-banner ${cls}">${this.esc(text)}
			<a href="#" data-drill="${index}">${this.esc(__("Setup Checklist"))}</a></div>`;
	}

	render_positions(p) {
		if (!p.banks.length) {
			return this.section(
				"positions",
				__("Bank Positions", null, TD_CTX),
				this.empty(__("No company bank account with a GL account."))
			);
		}
		const t = p.totals;
		const cards = [
			this.card(__("Cash Today"), this.money(t.current_company), t.cash_route),
			this.card(__("Projected in 7 Days"), this.money(t.projected_7_company), t.projected_route),
			this.card(__("Banks Below Minimum"), this.esc(t.below_minimum.count), t.below_minimum),
			this.card(__("Banks Below Warning"), this.esc(t.below_warning.count), t.below_warning),
		].join("");
		const rows = p.banks
			.map((b) => {
				const index = this.drills.push(b) - 1;
				return `<tr data-drill="${index}" class="td-clickable">
					<td>${this.esc(b.bank_account)}</td>
					<td><span class="td-pill td-level-${this.esc(b.level).replace(/\s/g, "-").toLowerCase()}">${this.esc(b.level_label)}</span></td>
					<td class="td-num">${this.money(b.current_balance, b.currency)}</td>
					<td class="td-num">${this.money(b.lowest.closing, b.currency)}</td>
					<td>${this.esc(b.lowest.date ? frappe.datetime.str_to_user(b.lowest.date) : "")}</td>
					<td class="td-num">${this.money(b.closing_horizon, b.currency)}</td>
				</tr>`;
			})
			.join("");
		const table = `<table class="table table-sm td-table"><thead><tr>
			<th>${this.esc(__("Bank Account", null, "Treasury"))}</th>
			<th>${this.esc(__("Level", null, "Treasury Alert Log"))}</th>
			<th class="td-num">${this.esc(__("Current Balance"))}</th>
			<th class="td-num">${this.esc(__("Lowest Projected"))}</th>
			<th>${this.esc(__("Lowest On"))}</th>
			<th class="td-num">${this.esc(__("Closing at Horizon"))}</th>
			</tr></thead><tbody>${rows}</tbody></table>`;
		const unassigned = p.unassigned_flows
			? `<div class="td-note text-muted small">${this.esc(
					__("{0} expected flows have no bank account.", [p.unassigned_flows])
			  )}</div>`
			: "";
		return this.section(
			"positions",
			__("Bank Positions", null, TD_CTX),
			`<div class="td-cards">${cards}</div>${table}${unassigned}
			<div class="td-charts"><div class="td-chart td-chart-curve"></div><div class="td-chart td-chart-flows"></div></div>`
		);
	}

	render_cheques(c) {
		const kpi = (label, value) => this.card(label, this.money(value.amount), value, this.esc(value.count));
		const cards = [
			kpi(__("Due This Week") + " - " + __("Outgoing"), c.due_out),
			kpi(__("Due This Week") + " - " + __("Incoming"), c.due_in),
			kpi(__("Post-dated Issued"), c.issued),
			kpi(__("Post-dated in Hand"), c.in_hand),
			kpi(__("Bounced This Month"), c.bounced_month),
		].join("");
		return this.section("cheques", __("PDC Cheques", null, TD_CTX), `<div class="td-cards">${cards}</div>`);
	}

	render_transfers(t) {
		const card = this.card(
			__("Transfers Awaiting Confirmation"),
			this.money(t.awaiting.amount),
			t.awaiting,
			this.esc(t.awaiting.count)
		);
		return this.section("transfers", __("Bank Transfers", null, TD_CTX), `<div class="td-cards">${card}</div>`);
	}

	render_charges(c) {
		const card = this.card(__("Bank Charges This Month"), this.money(c.month.net), c.month, this.esc(c.month.vouchers));
		return this.section(
			"charges",
			__("Bank Charges", null, TD_CTX),
			`<div class="td-cards">${card}</div><div class="td-chart td-chart-charges"></div>`
		);
	}

	render_security(s) {
		const cards = [
			this.card(__("Held from Others"), this.money(s.held), s, this.esc(s.count_held)),
			this.card(__("Given to Others"), this.money(s.given), s, this.esc(s.count_given)),
		].join("");
		return this.section("security", __("Security Cheques"), `<div class="td-cards">${cards}</div>`, true);
	}

	render_facilities(f, i) {
		let body = "";
		if (f) {
			const rows = f.rows
				.map((r) => {
					const index = this.drills.push(r) - 1;
					const width = Math.min(Math.max(flt(r.percent_used), 0), 100);
					return `<div class="td-facility td-clickable" data-drill="${index}">
						<div class="td-facility-name">${this.esc(r.facility_name || r.facility)} <span class="text-muted">${this.esc(r.lender || "")}</span></div>
						<div class="td-bar"><div class="td-bar-fill" style="inline-size: ${width}%"></div></div>
						<div class="small">${this.money(r.utilised, r.currency)} / ${this.money(r.limit, r.currency)} (${this.esc(flt(r.percent_used, 2))}%)</div>
					</div>`;
				})
				.join("");
			const more = f.truncated
				? `<div class="td-note text-muted small">${this.esc(__("and {0} more", [f.truncated]))}</div>`
				: "";
			body += rows ? rows + more : this.empty(__("Nothing to show."));
		}
		if (i) {
			const cards = [
				this.card(__("Overdue", null, "Treasury"), this.money(i.overdue.amount), i.overdue, this.esc(i.overdue.count)),
				this.card(__("Due in 30 Days", null, TD_CTX), this.money(i.due_30.amount), i.due_30, this.esc(i.due_30.count)),
			].join("");
			const next = i.next
				.map(
					(r) => `<tr><td>${this.esc(frappe.datetime.str_to_user(r.due_date))}</td>
					<td>${this.esc(r.lender_name || "")}</td><td>${this.esc(r.drawdown)} #${this.esc(r.installment_no)}</td>
					<td class="td-num">${this.money(r.amount_company)}</td></tr>`
				)
				.join("");
			body += `<div class="td-cards">${cards}</div>
				<div class="td-subtitle">${this.esc(__("Next Installments"))}</div>
				${next ? `<table class="table table-sm td-table"><tbody>${next}</tbody></table>` : this.empty(__("Nothing to show."))}`;
		}
		return this.section("facilities", __("Bank Facilities"), body, true);
	}

	render_instruments(g, c) {
		const cards = [];
		if (g) cards.push(this.card(__("Expiring Letters of Guarantee"), this.money(g.amount), g, this.esc(g.count)));
		if (c) cards.push(this.card(__("Expiring Letters of Credit"), this.money(c.amount), c, this.esc(c.count)));
		return this.section(
			"instruments",
			__("Letters of Guarantee and Credit", null, TD_CTX),
			`<div class="td-cards">${cards.join("")}</div>`,
			true
		);
	}
};
