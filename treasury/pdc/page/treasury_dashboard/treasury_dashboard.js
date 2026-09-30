// Copyright (c) 2026, Ahmed Abdelmageed and contributors
// For license information, please see license.txt

// Treasury Dashboard (spec 7h §3.2, design 7h §1): one read-only call, sections the user may read only. Plain page
// script (no bundle): Page.load_assets serves this file and treasury_dashboard.css. Every figure comes from the
// server (treasury.pdc.overview) and opens the report or list that proves it; this script only lays them out.

const TD_CTX = "Treasury Dashboard";
const TD_ALERT_CTX = "Treasury Alert Log";
const TD_TREASURY_CTX = "Treasury";
const TD_STORAGE_KEY = "treasury_dashboard_filters";
const TD_BANNER_KEY = "treasury_dashboard_banner_dismissed";
const TD_HORIZONS = [7, 14, 30];
const TD_DEFAULT_HORIZON = 7;
const TD_DUE_DAYS = 7; // the "this week" convention of the cards and PDC Register (spec §8 Q1)
const TD_STALE_MINUTES = 10;
const TD_MAX_BANK_ROWS = 8;
const TD_MAX_CURVE_BANKS = 6;
const TD_FACILITY_WARNING = 80;
const TD_FACILITY_CRITICAL = 100;
const TD_PHONE_QUERY = "(max-width: 767px)";
// fixed colour per meaning in every chart (design §1.7); levels (red / orange / green) are for statuses
const TD_CATEGORY_ORDER = ["Cheques In", "Cheques Out", "Transfers", "Installments", "Letters of Credit", "Other"];
const TD_CATEGORY_COLORS = {
	"Cheques In": "green",
	"Cheques Out": "red",
	Transfers: "purple",
	Installments: "orange",
	"Letters of Credit": "yellow",
	Other: "dark-grey",
};
const TD_FLOW_COLORS = ["green", "red", "blue"];
const TD_BANK_COLORS = ["blue", "purple", "#0ea5b7", "#e34aa6", "dark-grey"];
const TD_TOTAL_COLOR = "grey";
const TD_LEVEL_PILLS = { Critical: "red", Warning: "orange", OK: "green" };
const TD_INSTALLMENT_PILLS = { Overdue: "red", Due: "orange", Upcoming: "blue" };
// drill-downs that take a single bank: with several banks selected they open company-wide (spec §8 Q6)
const TD_SINGLE_BANK_ROUTES = ["PDC Register", "Bounced and Postponed Cheques", "Bank Charges Analysis", "Bank Transfer"];
const TD_MANAGER_ROLES = ["Treasury Manager", "System Manager"];

frappe.provide("treasury");

frappe.pages["treasury-dashboard"].on_page_load = (wrapper) => {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("Treasury Dashboard"),
		single_column: true,
	});
	frappe.breadcrumbs.add("PDC");
	// the filter bar sits outside page.main: scope every rule to the wrapper (design §1.2)
	$(wrapper).addClass("treasury-dashboard");
	wrapper.treasury_dashboard = new treasury.TreasuryDashboard(page, wrapper);
};

frappe.pages["treasury-dashboard"].on_page_show = (wrapper) => {
	const dashboard = wrapper.treasury_dashboard;
	if (!dashboard) {
		return;
	}
	const company = frappe.route_options && frappe.route_options.company;
	frappe.route_options = null;
	if (company && company !== dashboard.company_field.get_value()) {
		dashboard.company_field.set_value(company);
		return;
	}
	dashboard.show_staleness();
};

treasury.TreasuryDashboard = class TreasuryDashboard {
	constructor(page, wrapper) {
		this.page = page;
		this.wrapper = $(wrapper);
		this.ready = false;
		this.request = 0;
		this.data = null;
		this.updated_at = null;
		this.drills = [];
		this.charts = [];

		this.$root = $('<div class="trd-root"></div>').appendTo(this.page.main);
		this.$effective = $('<div class="trd-effective text-muted small hidden"></div>').appendTo(this.$root);
		this.$alert = $('<div class="trd-alert-area"></div>').appendTo(this.$root);
		this.$body = $('<div class="trd-body"></div>').appendTo(this.$root);
		this.$footer = $('<div class="trd-footer text-muted small"></div>').appendTo(this.$root);

		this.make_filters();
		this.make_actions();
		this.bind_events();
		this.ready = true;
		this.refresh();
	}

	// ------------------------------------------------------------------ filters

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

	default_company() {
		return frappe.defaults.get_user_default("Company") || frappe.defaults.get_default("company");
	}

	make_filters() {
		const stored = this.stored_filters();
		const company =
			(frappe.route_options && frappe.route_options.company) || stored.company || this.default_company();
		const reload = frappe.utils.debounce(() => this.ready && this.refresh(), 300);

		this.company_field = this.page.add_field({
			fieldname: "company",
			label: __("Company"),
			fieldtype: "Link",
			options: "Company",
			reqd: 1,
			default: company,
			change: () => {
				if (!this.ready) {
					return;
				}
				if (this.company_field.get_value() !== this.last_company) {
					// another company's banks do not apply
					this.bank_field.set_value([]);
				}
				reload();
			},
		});
		this.bank_field = this.page.add_field({
			fieldname: "bank_accounts",
			label: __("Bank Accounts", null, TD_CTX),
			placeholder: __("All bank accounts", null, TD_CTX),
			fieldtype: "MultiSelectList",
			get_data: (txt) =>
				frappe.db.get_link_options("Bank Account", txt, {
					company: this.company_field.get_value(),
					is_company_account: 1,
					disabled: 0,
				}),
			change: reload,
		});
		const horizon = TD_HORIZONS.includes(cint(stored.horizon)) ? cint(stored.horizon) : TD_DEFAULT_HORIZON;
		this.horizon_field = this.page.add_field({
			fieldname: "horizon",
			label: __("Horizon", null, TD_CTX),
			fieldtype: "Select",
			options: TD_HORIZONS.map((n) => ({ value: String(n), label: __("{0} days", [n], TD_CTX) })),
			default: String(horizon),
			change: reload,
		});
		$(this.company_field.wrapper).addClass("trd-filter-company");
		$(this.bank_field.wrapper).addClass("trd-filter-banks");
		$(this.horizon_field.wrapper).addClass("trd-filter-horizon");

		// a saved bank list only applies to the company it was saved for
		if (stored.company === company && Array.isArray(stored.bank_accounts) && stored.bank_accounts.length) {
			this.bank_field.set_value(stored.bank_accounts);
		}
		frappe.route_options = null;
	}

	filters() {
		return {
			company: this.company_field.get_value(),
			bank_accounts: this.bank_field.get_value() || [],
			horizon: cint(this.horizon_field.get_value()) || TD_DEFAULT_HORIZON,
		};
	}

	reset_filters() {
		try {
			localStorage.removeItem(TD_STORAGE_KEY);
		} catch (e) {
			// nothing stored
		}
		this.ready = false;
		this.company_field.set_value(this.default_company() || "");
		this.bank_field.set_value([]);
		this.horizon_field.set_value(String(TD_DEFAULT_HORIZON));
		this.ready = true;
		this.refresh();
	}

	// ------------------------------------------------------------------ actions

	make_actions() {
		const open = __("Open", null, TD_CTX);
		this.page.set_primary_action(__("Refresh"), () => this.refresh(), "refresh");
		this.page.add_inner_button(
			__("Bank Balance Outlook"),
			() => this.go(["query-report", "Bank Balance Outlook"], this.outlook_options()),
			open
		);
		this.page.add_inner_button(
			__("Bank Position Forecast"),
			() => {
				const positions = this.data && this.data.sections && this.data.sections.positions;
				if (positions) {
					this.go(positions.route, positions.route_options);
				} else {
					this.go(["query-report", "Bank Position Forecast"], { company: this.filters().company });
				}
			},
			open
		);
		this.page.add_menu_item(__("Reset Filters", null, TD_CTX), () => this.reset_filters());
		if (frappe.user.has_role(TD_MANAGER_ROLES)) {
			this.page.add_menu_item(__("Treasury Settings"), () => frappe.set_route("Form", "Treasury Settings"));
		}
		frappe.ui.keys.add_shortcut({
			shortcut: "shift+r",
			action: () => this.refresh(),
			description: __("Refresh the Treasury Dashboard"),
			page: this.page,
		});
	}

	outlook_options() {
		const filters = this.filters();
		const options = { company: filters.company, horizon_days: filters.horizon };
		if (filters.bank_accounts.length) {
			options.bank_account = filters.bank_accounts;
		}
		return options;
	}

	set_setup_button(setup) {
		// only for a user who may read the company's settings (spec: `setup` is null otherwise) and, without
		// settings, create them (`route` is null otherwise)
		const show = !!(setup && setup.route);
		if (show && !this.$setup_button) {
			this.$setup_button = this.page.add_inner_button(
				__("Setup Checklist"),
				() => this.data && this.data.setup && this.open_setup(this.data.setup),
				__("Open", null, TD_CTX)
			);
		}
		if (this.$setup_button) {
			// add_inner_button also adds a mobile menu entry ("Open > Setup Checklist"): toggle both
			this.$setup_button.toggleClass("hidden", !show);
			this.setup_menu_item().toggleClass("hidden", !show);
		}
	}

	setup_menu_item() {
		const label = `${__("Open", null, TD_CTX)} > ${__("Setup Checklist")}`;
		return this.page.menu
			.find(".menu-item-label")
			.filter((_i, el) => $(el).text().trim() === label)
			.closest("li");
	}

	open_setup(setup) {
		if (setup && setup.route) {
			this.go(setup.route, setup.route_options || {});
		}
	}

	go(route, route_options) {
		if (route && route.length) {
			// Frappe 16 set_route() does not take route options as a trailing argument (an object
			// becomes a "[object Object]" route segment): pass them through frappe.route_options.
			if (route_options && Object.keys(route_options).length) {
				frappe.route_options = route_options;
			}
			frappe.set_route(...route);
		}
	}

	bind_events() {
		const follow = (e) => {
			const drill = this.drills[cint($(e.currentTarget).attr("data-drill"))];
			if (!drill) {
				return;
			}
			e.preventDefault();
			e.stopPropagation();
			this.go(drill.route, drill.route_options);
		};
		this.$root.on("click", "[data-drill]", follow);
		this.$root.on("keydown", "[data-drill]", (e) => {
			if (e.key === "Enter" || e.key === " ") {
				follow(e);
			}
		});
		this.$root.on("click", ".trd-retry", (e) => {
			e.preventDefault();
			this.refresh();
		});
		this.$root.on("click", ".trd-dismiss", (e) => {
			e.preventDefault();
			try {
				sessionStorage.setItem(TD_BANNER_KEY, this.filters().company);
			} catch (err) {
				// the banner simply comes back
			}
			$(e.currentTarget).closest(".trd-banner").remove();
		});
		this.$root.on("click", ".trd-show-all", (e) => {
			e.preventDefault();
			const $section = $(e.currentTarget).closest(".trd-section");
			$section.find(".trd-extra").removeClass("trd-extra");
			$(e.currentTarget).remove();
		});
	}

	// ------------------------------------------------------------------ loading

	refresh() {
		const filters = this.filters();
		this.$alert.empty();
		if (!filters.company) {
			this.data = null;
			this.page.clear_indicator();
			this.page.set_title_sub("");
			this.$effective.addClass("hidden");
			this.$footer.empty();
			this.$body.html(this.state("building", __("Select a company to see its treasury position.")));
			return;
		}
		this.last_company = filters.company;
		this.store_filters();
		const token = ++this.request;
		if (this.data) {
			this.$root.addClass("trd-busy");
		} else {
			this.$body.html(this.skeleton());
		}
		this.page.set_title_sub(
			`<span class="trd-loading">${frappe.utils.escape_html(__("Loading…", null, TD_CTX))}</span>`
		);
		this.fetch(filters)
			.then((data) => {
				if (token !== this.request) {
					return;
				}
				this.updated_at = new Date();
				this.render(data);
			})
			.catch((error) => {
				if (token === this.request) {
					this.render_error(error, filters);
				}
			})
			.finally(() => {
				if (token === this.request) {
					this.$root.removeClass("trd-busy");
				}
			});
	}

	fetch(filters) {
		// frappe.call rather than xcall: the error type decides between the inline alert and the locked state
		return new Promise((resolve, reject) => {
			let settled = false;
			frappe.call({
				method: "treasury.pdc.page.treasury_dashboard.treasury_dashboard.get_dashboard_data",
				type: "GET",
				args: {
					company: filters.company,
					bank_accounts: JSON.stringify(filters.bank_accounts),
					horizon: filters.horizon,
				},
				callback: (r) => {
					settled = true;
					resolve(r.message);
				},
				error: () => {},
				always: (data) => {
					if (!settled) {
						settled = true;
						reject({
							exc_type: data && data.exc_type,
							message: this.server_message(data),
						});
					}
				},
			});
		});
	}

	server_message(data) {
		// the server messages as plain text (they may carry HTML such as a bold company name)
		let messages = [];
		try {
			messages = JSON.parse((data && data._server_messages) || "[]").map((m) => {
				try {
					return JSON.parse(m).message || "";
				} catch (e) {
					return m;
				}
			});
		} catch (e) {
			messages = [];
		}
		return messages
			.map((m) => new DOMParser().parseFromString(String(m), "text/html").body.textContent || "")
			.filter(Boolean)
			.join(" ");
	}

	render_error(error, filters) {
		const esc = frappe.utils.escape_html;
		const message = (error && error.message) || __("Nothing to show.");
		this.page.set_title_sub("");
		if (error && error.exc_type === "PermissionError" && !filters.bank_accounts.length) {
			// every section is forbidden for this company
			this.data = null;
			this.page.clear_indicator();
			this.$effective.addClass("hidden");
			this.$footer.empty();
			this.$body.html(this.state("lock", message, __("Ask your Treasury Manager for access.")));
			return;
		}
		// the last good render stays (design P13); a first load drops its skeleton
		if (!this.data) {
			this.$body.empty();
		}
		this.$alert.html(`<div class="trd-alert" role="alert">
			<span>${esc(message)}</span>
			<a href="#" class="trd-retry">${esc(__("Retry", null, TD_CTX))}</a>
		</div>`);
	}

	show_staleness() {
		// design P16: no auto-refresh, but no silent staleness either
		if (!this.updated_at || !this.data) {
			return;
		}
		const minutes = (new Date() - this.updated_at) / 60000;
		if (minutes > TD_STALE_MINUTES) {
			this.set_title_sub(true);
		}
	}

	set_title_sub(stale) {
		const esc = frappe.utils.escape_html;
		const time = moment(this.updated_at).format("HH:mm");
		const horizon = __("{0} days", [this.data.horizon], TD_CTX);
		const updated = stale
			? `<span class="trd-stale">${esc(__("Updated {0} — click Refresh", [time]))}</span>`
			: esc(__("Updated {0}", [time]));
		this.page.set_title_sub(
			`<span class="trd-title-sub"><bdi>${esc(this.data.company)}</bdi> · ${esc(horizon)} · ${updated}</span>`
		);
	}

	// ------------------------------------------------------------------ helpers

	esc(value) {
		return frappe.utils.escape_html(value == null ? "" : String(value));
	}

	is_phone() {
		return window.matchMedia && window.matchMedia(TD_PHONE_QUERY).matches;
	}

	arrow() {
		return frappe.utils.is_rtl() ? "←" : "→";
	}

	date(value) {
		return value ? frappe.datetime.str_to_user(value) : "";
	}

	money(value, currency) {
		const cur = currency || this.data.company_currency;
		return `<bdi>${this.esc(format_currency(flt(value), cur))}</bdi>`;
	}

	short_money(value, currency) {
		// design P4: phones show 1.2M EGP, the full amount stays in the title
		const cur = currency || this.data.company_currency;
		const symbol = (window.get_currency_symbol && get_currency_symbol(cur)) || cur;
		const sign = flt(value) < 0 ? "-" : "";
		return `<bdi>${this.esc(`${sign}${frappe.utils.shorten_number(Math.abs(flt(value)))} ${symbol}`)}</bdi>`;
	}

	drill(target) {
		if (!target || !target.route) {
			return "";
		}
		return ` data-drill="${this.drills.push(target) - 1}"`;
	}

	pill(color, text) {
		return `<span class="indicator-pill ${color} no-indicator-dot">${this.esc(text)}</span>`;
	}

	level_pill(level, label) {
		return this.pill(TD_LEVEL_PILLS[level] || "gray", label || level);
	}

	empty(message) {
		return `<div class="trd-empty text-muted">${this.esc(message || __("Nothing to show."))}</div>`;
	}

	state(icon, message, hint) {
		return `<div class="trd-state">
			<div class="trd-state-icon">${frappe.utils.icon(icon, "lg")}</div>
			<div class="trd-state-message">${this.esc(message)}</div>
			${hint ? `<div class="text-muted">${this.esc(hint)}</div>` : ""}
		</div>`;
	}

	skeleton() {
		// drawn synchronously before the call; the group headings are real text (design §1.11)
		const card = '<div class="trd-kpi trd-skel-card"><div class="trd-skel"></div><div class="trd-skel trd-skel-lg"></div></div>';
		const kpis = Array.from({ length: 4 }, () => card).join("");
		const lines = Array.from({ length: 4 }, () => '<div class="trd-skel"></div>').join("");
		return `<div class="trd-group-title">${this.esc(__("Cash Position", null, TD_CTX))}</div>
			<div class="trd-grid">
				<div class="trd-span-12"><div class="trd-kpis">${kpis}</div></div>
				<div class="trd-section trd-span-12">${lines}</div>
				<div class="trd-section trd-span-7"><div class="trd-skel trd-skel-chart"></div></div>
				<div class="trd-section trd-span-5"><div class="trd-skel trd-skel-chart"></div></div>
			</div>
			<div class="trd-group-title">${this.esc(__("Coming Up", null, TD_CTX))}</div>`;
	}

	company_wide() {
		// the security, facility and LG / LC documents are not per bank account (spec §8 Q4)
		return this.data.bank_accounts.length
			? `<span class="trd-scope">${frappe.utils.icon("globe", "xs")} ${this.esc(
					__("All bank accounts of the company")
			  )}</span>`
			: "";
	}

	multi_bank_note(route) {
		// the effective banks (the server adds the GL siblings): one selected bank may count as two, and then
		// the single-bank report opens without its bank filter
		const effective = (this.data.bank_accounts || []).length;
		return effective > 1 && route && TD_SINGLE_BANK_ROUTES.includes(route[1])
			? __("The report opens for all banks of the company.")
			: "";
	}

	/**
	 * A section card: `title` (a link when `target` has a route), the company-wide note, a muted subtitle and
	 * the "View report" link on the end side.
	 */
	section({ key, span, title, target, subtitle, company_wide, body, view }) {
		const title_html = target
			? `<a href="#" class="trd-title-link"${this.drill(target)}>${this.esc(title)}</a>`
			: this.esc(title);
		const view_html =
			view && target
				? `<a href="#" class="trd-view"${this.drill(target)}>${this.esc(__("View report", null, TD_CTX))} ${this.arrow()}</a>`
				: "";
		return `<section class="trd-section trd-span-${span || 12}" data-section="${this.esc(key)}">
			<div class="trd-section-head">
				<h5 class="trd-title">${title_html}</h5>
				${company_wide ? this.company_wide() : ""}
				${subtitle ? `<span class="trd-subtitle text-muted small">${this.esc(subtitle)}</span>` : ""}
				<span class="trd-head-end">${view_html}</span>
			</div>
			${body}
		</section>`;
	}

	/**
	 * A KPI card (design §1.5): an <a> opening `target`, a value (`amount` in the company currency or a
	 * `count`), a subtitle, an optional trend line and a status (critical / warning / good) as border + pill.
	 * The chevron "›" is a bidi-mirrored character: in RTL it already points to the end side.
	 */
	kpi({ label, amount, count, currency, sub, trend, status, target, tooltip }) {
		let value;
		let full = "";
		if (amount !== undefined) {
			full = format_currency(flt(amount), currency || this.data.company_currency);
			value = `<span class="trd-full">${this.money(amount, currency)}</span><span class="trd-short">${this.short_money(
				amount,
				currency
			)}</span>`;
		} else {
			value = `<bdi>${this.esc(cint(count))}</bdi>`;
		}
		const pills = {
			critical: ["red", __("Critical", null, TD_ALERT_CTX)],
			warning: ["orange", __("Warning", null, TD_ALERT_CTX)],
			good: ["green", __("OK", null, TD_ALERT_CTX)],
		};
		const pill = pills[status] ? this.pill(...pills[status]) : "";
		const trend_html = trend
			? `<span class="trd-trend ${trend.color ? `trd-text-${trend.color}` : "text-muted"}">${this.esc(trend.text)}</span>`
			: "<span></span>";
		const title = [tooltip, full, target && this.multi_bank_note(target.route)].filter(Boolean).join("\n");
		return `<a href="#" role="button" tabindex="0" class="trd-kpi${status ? ` trd-kpi-${status}` : ""}"${this.drill(
			target
		)} title="${this.esc(title)}">
			<span class="trd-kpi-label">${this.esc(label)}</span>
			<span class="trd-kpi-value">${value}</span>
			${sub ? `<span class="trd-kpi-sub">${this.esc(sub)}</span>` : ""}
			${trend || pill ? `<span class="trd-kpi-foot">${trend_html}${pill}</span>` : ""}
			<span class="trd-chevron" aria-hidden="true">›</span>
		</a>`;
	}

	kpis(cards) {
		return `<div class="trd-kpis">${cards.filter(Boolean).join("")}</div>`;
	}

	// ------------------------------------------------------------------ rendering

	render(data) {
		this.data = data;
		this.drills = [];
		this.charts = [];
		const s = data.sections || {};
		const hidden = new Set(data.hidden || []);
		const has = (key) => !!s[key] && !hidden.has(key);

		this.set_title_sub(false);
		this.set_indicator(has("positions") ? s.positions : null);
		this.set_setup_button(data.setup);
		this.render_effective_banks();

		const pair = (left, right, spans) => {
			// one half hidden: the other takes the full width (design §1.4)
			if (left && right) {
				return [left(spans[0]), right(spans[1])];
			}
			return [left ? left(12) : "", right ? right(12) : ""];
		};

		const cash = [];
		if (has("positions")) {
			cash.push(this.render_position_kpis(s.positions), this.render_banks(s.positions));
			if (s.positions.banks.length) {
				cash.push(
					...pair(
						(span) => this.render_balance_curve(s.positions, span),
						(span) => this.render_cash_flows(s.positions, span),
						[7, 5]
					)
				);
			}
		}

		const coming = [];
		if (has("cheques")) {
			coming.push(this.render_cheques(s.cheques));
		}
		coming.push(
			...pair(
				has("transfers") && ((span) => this.render_transfers(s.transfers, span)),
				has("charges") && ((span) => this.render_charges(s.charges, span)),
				[4, 8]
			),
			...pair(
				has("facilities") && ((span) => this.render_facilities(s.facilities, span)),
				has("installments") && ((span) => this.render_installments(s.installments, span)),
				[7, 5]
			),
			...pair(
				has("guarantees") && ((span) => this.render_guarantees(s.guarantees, span)),
				has("credits") && ((span) => this.render_credits(s.credits, span)),
				[6, 6]
			)
		);
		if (has("positions") && s.positions.banks.length) {
			coming.push(this.render_maturities(s.positions.charts.maturities));
		}

		const security = has("security") ? [this.render_security(s.security)] : [];
		const group = (title, blocks) => {
			const html = blocks.filter(Boolean).join("");
			return html
				? `<div class="trd-group-title">${this.esc(title)}</div><div class="trd-grid">${html}</div>`
				: "";
		};

		this.$body.html(
			this.render_setup_banner(data.setup) +
				group(__("Cash Position", null, TD_CTX), cash) +
				group(__("Coming Up", null, TD_CTX), coming) +
				group(__("Security Cheques", null, TD_CTX), security)
		);
		this.$footer.html(
			hidden.size ? this.esc(__("Some sections are hidden because of your permissions.")) : ""
		);
		this.mount_charts();
	}

	set_indicator(positions) {
		if (!positions || !positions.banks.length) {
			this.page.clear_indicator();
			return;
		}
		const esc = frappe.utils.escape_html;
		const critical = positions.totals.below_minimum.count;
		const warning = positions.totals.below_warning.count;
		if (critical) {
			this.page.set_indicator(esc(__("{0} bank(s) below minimum", [critical])), "red");
		} else if (warning) {
			this.page.set_indicator(esc(__("{0} bank(s) below warning", [warning])), "orange");
		} else {
			this.page.set_indicator(esc(__("All banks above their thresholds")), "green");
		}
	}

	render_effective_banks() {
		// the server adds the bank accounts sharing a GL account with a selected one (design §1.2)
		const selected = new Set(this.filters().bank_accounts);
		const added = (this.data.bank_accounts || []).filter((b) => !selected.has(b));
		this.$effective
			.toggleClass("hidden", !added.length || !selected.size)
			.text(added.length ? __("Also included (same GL account): {0}", [added.join(", ")]) : "");
	}

	render_setup_banner(setup) {
		if (!setup || !cint(setup.missing)) {
			return "";
		}
		const company = this.data.company;
		let dismissed = false;
		try {
			dismissed = sessionStorage.getItem(TD_BANNER_KEY) === company;
		} catch (e) {
			dismissed = false;
		}
		let color = "red";
		let text;
		let button = __("Open Setup Checklist");
		if (!cint(setup.exists)) {
			text = __("Treasury is not set up for {0} yet.", [company]);
			button = __("Create Company Settings");
		} else if (cint(setup.required_missing)) {
			text = __("{0} required setup step(s) missing: amounts may be incomplete.", [setup.required_missing]);
		} else {
			if (dismissed) {
				return "";
			}
			color = "orange";
			text = __("{0} recommended setup step(s) open.", [setup.missing]);
		}
		// design P12: only the Recommended-only banner can be dismissed (for the session)
		const dismiss =
			color === "orange"
				? `<a href="#" class="trd-dismiss" aria-label="${this.esc(__("Dismiss", null, TD_CTX))}">×</a>`
				: "";
		// no route: the user may not create the settings, so no button leading to a form they cannot save
		const action = setup.route
			? `<a href="#" class="btn btn-xs btn-default"${this.drill({
					route: setup.route,
					route_options: setup.route_options,
			  })}>${this.esc(button)}</a>`
			: `<span class="text-muted small">${this.esc(__("Ask your Treasury Manager to set it up."))}</span>`;
		return `<div class="trd-banner trd-banner-${color}" role="status">
			<span class="trd-banner-text">${this.esc(text)}</span>
			${action}
			${dismiss}
		</div>`;
	}

	// ------------------------------------------------------------------ group A: cash position

	render_position_kpis(p) {
		const t = p.totals;
		const banks = p.banks;
		if (!banks.length) {
			return `<div class="trd-span-12">${this.empty_positions()}</div>`;
		}
		const horizon = this.data.horizon;
		const worst = banks[0];
		const any_critical = banks.some((b) => b.level === "Critical");
		const delta = flt(t.closing_horizon_company) - flt(t.current_company);
		const trend = {
			text: `${delta >= 0 ? "▲" : "▼"} ${format_currency(Math.abs(delta), this.data.company_currency)}`,
			color: delta >= 0 ? "green" : "red",
		};
		const unassigned = cint(p.unassigned_flows)
			? `<div class="trd-note text-muted small"><a href="#"${this.drill({
					route: ["query-report", "PDC Register"],
					route_options: { company: this.data.company },
			  })}>${this.esc(
					__("{0} expected cheque(s) have no bank account and are not in the balances.", [
						cint(p.unassigned_flows),
					])
			  )}</a></div>`
			: "";
		const worst_name = (level) => {
			const bank = banks.find((b) => b.level === level);
			return bank ? bank.bank_account : __("None", null, TD_CTX);
		};
		return `<div class="trd-span-12">${this.kpis([
			this.kpi({
				label: __("Cash Today"),
				amount: t.current_company,
				sub: __("{0} bank account(s) · company currency", [banks.length]),
				target: t.cash_route,
			}),
			this.kpi({
				label: __("Projected in {0} Days", [horizon]),
				amount: t.closing_horizon_company,
				sub: worst.lowest.date
					? __("Lowest {0} on {1}", [
							format_currency(worst.lowest.closing_company, this.data.company_currency),
							this.date(worst.lowest.date),
					  ])
					: "",
				trend,
				status: flt(t.closing_horizon_company) < 0 || any_critical ? "critical" : null,
				target: t.horizon_route,
			}),
			this.kpi({
				label: __("Banks Below Minimum"),
				count: t.below_minimum.count,
				sub: worst_name("Critical"),
				status: t.below_minimum.count ? "critical" : t.below_warning.count ? null : "good",
				target: t.below_minimum,
			}),
			this.kpi({
				label: __("Banks Below Warning"),
				count: t.below_warning.count,
				sub: worst_name("Warning"),
				status: t.below_warning.count ? "warning" : t.below_minimum.count ? null : "good",
				target: t.below_warning,
			}),
		])}${unassigned}</div>`;
	}

	empty_positions() {
		const setup = this.data.setup;
		const open = __("Open Setup Checklist");
		const link =
			setup && setup.route
				? ` <a href="#"${this.drill({ route: setup.route, route_options: setup.route_options })}>${this.esc(open)}</a>`
				: "";
		return `<div class="trd-section"><div class="trd-empty text-muted">${this.esc(
			__("No company bank account with a GL account.")
		)}${link}</div></div>`;
	}

	render_banks(p) {
		if (!p.banks.length) {
			return "";
		}
		const cur = this.data.company_currency;
		const show_company = p.banks.some((b) => b.currency !== cur);
		const breach_class = { Critical: "trd-text-red", Warning: "trd-text-orange" };
		const projected_class = (b) => {
			if (b.minimum !== null && b.minimum !== undefined && flt(b.projected_7) < flt(b.minimum)) {
				return "trd-text-red";
			}
			if (b.warning !== null && b.warning !== undefined && flt(b.projected_7) < flt(b.warning)) {
				return "trd-text-orange";
			}
			return "";
		};
		const also = (b) => {
			const others = (b.bank_accounts || []).filter((name) => name !== b.bank_account);
			return others.length ? ` · ${this.esc(__("also: {0}", [others.join(", ")]))}` : "";
		};
		const rows = p.banks.map((b, index) => {
			const extra = index >= TD_MAX_BANK_ROWS ? " trd-extra" : "";
			const drill = this.drill(b);
			return `<tr class="trd-clickable${extra}"${drill}>
				<td><a href="#" class="trd-bank-name"${drill}><b>${this.esc(b.bank_account)}</b></a>
					<div class="text-muted small">${this.esc(b.bank || "")}${also(b)}</div></td>
				<td>${this.level_pill(b.level, b.level_label)}</td>
				<td class="trd-num">${this.money(b.current_balance, b.currency)}</td>
				<td class="trd-num ${projected_class(b)}">${this.money(b.projected_7, b.currency)}</td>
				<td class="trd-num">${this.money(b.lowest.closing, b.currency)}
					<div class="text-muted small">${b.lowest.date ? this.esc(__("on {0}", [this.date(b.lowest.date)], TD_CTX)) : ""}</div></td>
				<td class="${breach_class[b.level] || ""}">${this.esc(this.date(b.first_breach))}</td>
				<td class="trd-num">${flt(b.shortfall) > 0 ? `<b class="trd-text-red">${this.money(b.shortfall, b.currency)}</b>` : ""}</td>
				<td class="trd-num">${this.money(b.closing_horizon, b.currency)}</td>
				${show_company ? `<td class="trd-num text-muted">${this.money(b.current_company, cur)}</td>` : ""}
			</tr>`;
		});
		const cards = p.banks.map((b, index) => {
			const extra = index >= TD_MAX_BANK_ROWS ? " trd-extra" : "";
			const lowest = __("Today {0} → lowest {1} on {2}", [
				format_currency(flt(b.current_balance), b.currency),
				format_currency(flt(b.lowest.closing), b.currency),
				this.date(b.lowest.date) || "—",
			]);
			return `<a href="#" class="trd-bank-card${extra}"${this.drill(b)}>
				<span class="trd-bank-card-head"><b>${this.esc(b.bank_account)}</b>${this.level_pill(b.level, b.level_label)}</span>
				<span class="small"><bdi>${this.esc(lowest)}</bdi></span>
				${flt(b.shortfall) > 0 ? `<span class="small trd-text-red"><b>${this.esc(__("Shortfall"))}: ${this.money(b.shortfall, b.currency)}</b></span>` : ""}
			</a>`;
		});
		const head = [
			[__("Bank Account", null, TD_TREASURY_CTX), ""],
			[__("Level", null, TD_ALERT_CTX), ""],
			[__("Current Balance"), "trd-num"],
			[__("Projected in 7 Days"), "trd-num"],
			[__("Lowest", null, TD_CTX), "trd-num"],
			[__("First Breach"), ""],
			[__("Shortfall"), "trd-num"],
			[__("Closing at Horizon"), "trd-num"],
		];
		if (show_company) {
			head.push([__("Company Currency", null, TD_CTX), "trd-num"]);
		}
		const t = p.totals;
		const footer = `<tr class="trd-total">
			<td colspan="2">${this.esc(__("Total (company currency)"))}</td>
			<td class="trd-num">${this.money(t.current_company, cur)}</td>
			<td class="trd-num">${this.money(t.projected_7_company, cur)}</td>
			<td colspan="${show_company ? 5 : 4}"></td>
		</tr>`;
		const more =
			p.banks.length > TD_MAX_BANK_ROWS
				? `<a href="#" class="trd-show-all">${this.esc(__("Show all {0} banks", [p.banks.length]))}</a>`
				: "";
		return this.section({
			key: "banks",
			span: 12,
			title: __("Bank Positions", null, TD_CTX),
			target: { route: p.route, route_options: p.route_options },
			view: true,
			body: `<div class="trd-table-wrap"><table class="table table-sm trd-table">
					<thead><tr>${head.map(([label, cls]) => `<th class="${cls}">${this.esc(label)}</th>`).join("")}</tr></thead>
					<tbody>${rows.join("")}</tbody><tfoot>${footer}</tfoot>
				</table></div>
				<div class="trd-bank-cards">${cards.join("")}</div>${more}`,
		});
	}

	render_balance_curve(p, span) {
		return this.chart_section({
			key: "balance_curve",
			span,
			title: __("Projected Balance by Bank", null, TD_CTX),
			target: { route: p.route, route_options: p.route_options },
			chart: () => this.balance_curve_options(p.charts.balance_curve),
		});
	}

	balance_curve_options(chart) {
		// design P10: a "Total" line for 2 to 6 banks; beyond 6, the 5 worst and "Others"
		let datasets = (chart.data.datasets || []).map((d) => ({ name: d.name, values: d.values.map(flt) }));
		const sum = (sets) => (sets[0] ? sets[0].values.map((_v, i) => sets.reduce((s, d) => s + d.values[i], 0)) : []);
		const colors = [];
		if (datasets.length > TD_MAX_CURVE_BANKS) {
			const sorted = datasets.slice().sort((a, b) => Math.min(...a.values) - Math.min(...b.values));
			const worst = sorted.slice(0, TD_MAX_CURVE_BANKS - 1);
			datasets = [...worst, { name: __("Others", null, TD_CTX), values: sum(sorted.slice(TD_MAX_CURVE_BANKS - 1)) }];
			worst.forEach((_d, i) => colors.push(TD_BANK_COLORS[i % TD_BANK_COLORS.length]));
			colors.push(TD_TOTAL_COLOR);
		} else {
			datasets.forEach((_d, i) => colors.push(TD_BANK_COLORS[i % TD_BANK_COLORS.length]));
			if (datasets.length >= 2) {
				datasets.push({ name: __("Total", null, TD_TREASURY_CTX), values: sum(datasets) });
				colors.push(TD_TOTAL_COLOR);
			}
		}
		const values = datasets.flatMap((d) => d.values);
		return {
			data: {
				labels: chart.data.labels,
				datasets,
				// design P11: the zero line shows when a bank dips below zero
				yMarkers: values.some((v) => v < 0)
					? [{ label: __("Zero", null, TD_CTX), value: 0, options: { labelPos: "left" } }]
					: undefined,
			},
			type: "line",
			colors,
		};
	}

	render_cash_flows(p, span) {
		const chart = p.charts.cash_flows;
		const first = chart.data.labels[0];
		return this.chart_section({
			key: "cash_flows",
			span,
			title: __("Inflows and Outflows", null, TD_CTX),
			subtitle: this.data.horizon <= 14 ? __("Daily", null, TD_CTX) : __("Weekly", null, TD_CTX),
			target: { route: p.route, route_options: p.route_options },
			chart: () => ({
				data: chart.data,
				type: chart.type,
				colors: TD_FLOW_COLORS,
				// day 0 carries the overdue expected flows, as the positions do
				format_x: (label) =>
					label === first ? `${label} (${__("includes overdue expected flows")})` : label,
			}),
		});
	}

	// ------------------------------------------------------------------ group B: coming up

	render_cheques(c) {
		const esc_date = (days) => this.date(frappe.datetime.add_days(this.data.today, days));
		const due_sub = (value) => __("{0} cheque(s) · today → {1}", [cint(value.count), esc_date(TD_DUE_DAYS)]);
		const count_sub = (value) => __("{0} cheque(s)", [cint(value.count)]);
		const due_tip = `${__("Due This Week")}: ${__("today to today + 7 days")}`;
		const cash = this.data.sections.positions && this.data.sections.positions.totals.current_company;
		const share = flt(cash) > 0 ? Math.round((flt(c.due_out.amount) * 100) / flt(cash)) : null;
		const month_start = moment(this.data.today).startOf("month").format("YYYY-MM-DD");
		return this.section({
			key: "cheques",
			span: 12,
			title: __("PDC Cheques", null, TD_CTX),
			body: this.kpis([
				this.kpi({
					label: __("Due Out This Week"),
					amount: c.due_out.amount,
					sub: due_sub(c.due_out),
					trend:
						share === null
							? null
							: {
									text: __("{0}% of cash today", [share]),
									color: share > 100 ? "red" : share > 50 ? "orange" : null,
							  },
					target: c.due_out,
					tooltip: due_tip,
				}),
				this.kpi({
					label: __("Due In This Week"),
					amount: c.due_in.amount,
					sub: due_sub(c.due_in),
					target: c.due_in,
					tooltip: due_tip,
				}),
				this.kpi({
					label: __("Post-dated Issued"),
					amount: c.issued.amount,
					sub: count_sub(c.issued),
					target: c.issued,
				}),
				this.kpi({
					label: __("Post-dated in Hand"),
					amount: c.in_hand.amount,
					sub: count_sub(c.in_hand),
					target: c.in_hand,
				}),
				this.kpi({
					label: __("Bounced This Month"),
					amount: c.bounced_month.amount,
					sub: __("{0} bounce(s) since {1}", [cint(c.bounced_month.count), this.date(month_start)]),
					status: cint(c.bounced_month.count) ? "warning" : null,
					target: c.bounced_month,
				}),
			]),
		});
	}

	render_transfers(t, span) {
		const awaiting = t.awaiting;
		return this.section({
			key: "transfers",
			span,
			title: __("Bank Transfers", null, TD_CTX),
			target: awaiting,
			body: this.kpis([
				this.kpi({
					label: __("Transfers Awaiting Confirmation"),
					amount: awaiting.amount,
					sub: __("{0} transfer(s) sent", [cint(awaiting.count)]),
					// design P7: sent but unconfirmed needs a follow-up
					status: cint(awaiting.count) ? "warning" : null,
					target: awaiting,
				}),
			]),
		});
	}

	render_charges(c, span) {
		const month = c.month;
		const cur = this.data.company_currency;
		const kpi = this.kpis([
			this.kpi({
				label: __("Bank Charges This Month"),
				amount: month.net,
				sub: __("Charges {0} · Refunds {1} · {2} voucher(s)", [
					format_currency(month.charges, cur),
					format_currency(month.refunds, cur),
					cint(month.vouchers),
				]),
				target: month,
			}),
		]);
		const chart = c.by_category;
		const labels = chart.data.labels || [];
		const nets = ((chart.data.datasets || [])[0] || { values: [] }).values.map(flt);
		// design §1.7.4: a percentage bar for 1 to 6 positive groups, else a bar chart
		const percentage = labels.length && labels.length <= 6 && nets.every((v) => v > 0);
		const legend = percentage
			? `<div class="trd-legend small">${labels
					.map((label, i) => `<span>${this.esc(label)} · ${this.money(nets[i], cur)}</span>`)
					.join("")}</div>`
			: "";
		return this.chart_section({
			key: "charges",
			span,
			title: __("Bank Charges", null, TD_CTX),
			target: month,
			before: kpi,
			after: legend,
			subtitle: __("Charges by Category", null, TD_CTX),
			chart: () => ({
				data: percentage
					? { labels, datasets: [{ name: chart.data.datasets[0].name, values: nets }] }
					: chart.data,
				type: percentage ? "percentage" : "bar",
				colors: percentage ? undefined : ["orange"],
			}),
		});
	}

	render_facilities(f, span) {
		const cur = this.data.company_currency;
		const t = f.totals;
		const bar = (percent) => {
			const value = Math.min(Math.max(flt(percent), 0), 100);
			const color =
				flt(percent) >= TD_FACILITY_CRITICAL ? "red" : flt(percent) >= TD_FACILITY_WARNING ? "orange" : "green";
			return `<span class="trd-bar"><span class="trd-bar-fill trd-bg-${color}" style="inline-size: ${value}%"></span></span>`;
		};
		if (!f.rows.length) {
			return this.section({
				key: "facilities",
				span,
				title: __("Facility Utilisation"),
				target: t,
				company_wide: true,
				body: this.empty(),
			});
		}
		const total = `<div class="trd-facility trd-facility-total">
			<div class="trd-facility-line"><b>${this.esc(
				__("{0} of {1} ({2}% used)", [
					format_currency(t.utilised_company, cur),
					format_currency(t.limit_company, cur),
					flt(t.percent_used, 1),
				])
			)}</b></div>
			${bar(t.percent_used)}
		</div>`;
		const rows = f.rows
			.map(
				(r) => `<div class="trd-facility">
				<div class="trd-facility-line">
					<a href="#"${this.drill(r)}>${this.esc(r.facility_name || r.facility)}</a>
					<span class="text-muted small">${this.esc(r.lender || "")}</span>
					<span class="trd-facility-amount small">${this.money(r.utilised, r.currency)} / ${this.money(
					r.limit,
					r.currency
				)} · ${this.esc(__("{0}% used", [flt(r.percent_used, 1)]))}</span>
				</div>
				${bar(r.percent_used)}
			</div>`
			)
			.join("");
		const more = cint(f.truncated)
			? `<a href="#" class="small"${this.drill(t)}>${this.esc(__("and {0} more", [cint(f.truncated)]))} ${this.arrow()}</a>`
			: "";
		return this.section({
			key: "facilities",
			span,
			title: __("Facility Utilisation"),
			target: t,
			company_wide: true,
			view: true,
			body: total + rows + more,
		});
	}

	render_installments(i, span) {
		const pills = TD_INSTALLMENT_PILLS;
		const rows = (i.next || [])
			.map(
				(r) => `<tr>
				<td>${this.esc(this.date(r.due_date))}</td>
				<td><a href="#"${this.drill({ route: ["Form", "Facility Drawdown", r.drawdown] })}>${this.esc(r.drawdown)}</a></td>
				<td>${this.esc(r.lender_name || "")}</td>
				<td>${this.esc(r.installment_no)}</td>
				<td class="trd-num">${this.money(r.amount_company)}</td>
				<td>${r.display_status ? this.pill(pills[r.display_status] || "gray", __(r.display_status, null, TD_TREASURY_CTX)) : ""}</td>
			</tr>`
			)
			.join("");
		const table = rows
			? `<div class="trd-table-wrap"><table class="table table-sm trd-table"><thead><tr>
					<th>${this.esc(__("Due Date", null, TD_TREASURY_CTX))}</th>
					<th>${this.esc(__("Drawdown", null, TD_CTX))}</th>
					<th>${this.esc(__("Lender", null, TD_CTX))}</th>
					<th>#</th>
					<th class="trd-num">${this.esc(__("Amount", null, TD_TREASURY_CTX))}</th>
					<th>${this.esc(__("Status", null, TD_TREASURY_CTX))}</th>
				</tr></thead><tbody>${rows}</tbody></table></div>`
			: this.empty();
		return this.section({
			key: "installments",
			span,
			title: __("Next Installments"),
			target: i.due_30,
			company_wide: true,
			body:
				this.kpis([
					this.kpi({
						label: __("Overdue Installments"),
						amount: i.overdue.amount,
						sub: __("{0} installment(s)", [cint(i.overdue.count)]),
						status: cint(i.overdue.count) ? "critical" : null,
						target: i.overdue,
					}),
					this.kpi({
						label: __("Due in 30 Days", null, TD_CTX),
						amount: i.due_30.amount,
						sub: __("{0} installment(s)", [cint(i.due_30.count)]),
						target: i.due_30,
					}),
				]) + table,
		});
	}

	render_instruments({ key, span, value, title, doctype, number, party, party_label }) {
		const days_color = (days) => (cint(days) <= 7 ? "red" : cint(days) <= 30 ? "orange" : "gray");
		const rows = (value.next || [])
			.map(
				(r) => `<tr>
				<td><a href="#"${this.drill({ route: ["Form", doctype, r.name] })}><bdi>${this.esc(r[number] || r.name)}</bdi></a></td>
				<td>${this.esc(r[party] || "")}</td>
				<td>${this.esc(this.date(r.effective_expiry_date))}</td>
				<td>${this.pill(days_color(r.days_to_expiry), __("{0} d", [cint(r.days_to_expiry)], TD_CTX))}</td>
				<td class="trd-num">${this.money(r.open_company)}</td>
			</tr>`
			)
			.join("");
		const footer = cint(value.count)
			? `<a href="#" class="small"${this.drill(value)}>${this.esc(__("View all {0}", [cint(value.count)], TD_CTX))} ${this.arrow()}</a>`
			: "";
		const body =
			this.kpis([
				this.kpi({
					label: title,
					amount: value.amount,
					sub: __("{0} expiring within 30 days", [cint(value.count)]),
					status: cint(value.count) ? "warning" : null,
					target: value,
				}),
			]) +
			(rows
				? `<div class="trd-table-wrap"><table class="table table-sm trd-table"><thead><tr>
					<th>${this.esc(__("No.", null, TD_CTX))}</th>
					<th>${this.esc(party_label)}</th>
					<th>${this.esc(__("Expires", null, TD_CTX))}</th>
					<th>${this.esc(__("Days", null, TD_CTX))}</th>
					<th class="trd-num">${this.esc(__("Open Amount", null, TD_CTX))}</th>
				</tr></thead><tbody>${rows}</tbody></table></div>${footer}`
				: this.empty());
		return this.section({ key, span, title, target: value, company_wide: true, body });
	}

	render_guarantees(g, span) {
		return this.render_instruments({
			key: "guarantees",
			span,
			value: g,
			title: __("Expiring Letters of Guarantee"),
			doctype: "Letter of Guarantee",
			number: "lg_no",
			party: "beneficiary_name",
			party_label: __("Beneficiary", null, TD_CTX),
		});
	}

	render_credits(c, span) {
		return this.render_instruments({
			key: "credits",
			span,
			value: c,
			title: __("Expiring Letters of Credit"),
			doctype: "Letter of Credit",
			number: "lc_no",
			party: "supplier_name",
			party_label: __("Supplier"),
		});
	}

	render_maturities(chart) {
		return this.chart_section({
			key: "maturities",
			span: 12,
			title: __("Maturities (30 Days)"),
			target: { route: chart.route, route_options: chart.route_options },
			chart: () => {
				// the legend order is fixed (design §1.7.3); the server drops the all-zero categories
				const keys = chart.keys || [];
				const order = TD_CATEGORY_ORDER.filter((key) => keys.includes(key));
				return {
					data: {
						labels: chart.data.labels,
						datasets: order.map((key) => chart.data.datasets[keys.indexOf(key)]),
					},
					type: "bar",
					bar_options: chart.barOptions,
					colors: order.map((key) => TD_CATEGORY_COLORS[key]),
				};
			},
		});
	}

	// ------------------------------------------------------------------ group C: security cheques

	render_security(s) {
		const target = (direction) => ({
			route: s.route,
			route_options: Object.assign({}, s.route_options, direction ? { direction } : {}),
		});
		const sub = (count, open) => __("{0} cheque(s) · {1} open (no amount)", [cint(count), cint(open)]);
		return this.section({
			key: "security",
			span: 12,
			title: __("Security Exposure"),
			target: target(null),
			company_wide: true,
			view: true,
			body: this.kpis([
				this.kpi({
					label: __("Security Held"),
					amount: s.held,
					sub: sub(s.count_held, s.open_held),
					target: target("Incoming"),
				}),
				this.kpi({
					label: __("Security Given"),
					amount: s.given,
					sub: sub(s.count_given, s.open_given),
					target: target("Outgoing"),
				}),
				this.kpi({ label: __("Open Held", null, TD_CTX), count: s.open_held, target: target("Incoming") }),
				this.kpi({ label: __("Open Given", null, TD_CTX), count: s.open_given, target: target("Outgoing") }),
			]),
		});
	}

	// ------------------------------------------------------------------ charts

	chart_section({ key, span, title, subtitle, target, chart, before, after }) {
		const index = this.charts.push(chart) - 1;
		return this.section({
			key,
			span,
			title,
			subtitle,
			target,
			body: `${before || ""}<div class="trd-chart" dir="ltr" data-chart="${index}"></div>${after || ""}`,
		});
	}

	/**
	 * frappe-charts writes axis labels, legend (dataset names), marker labels and tooltip titles as innerHTML:
	 * every text given to it is escaped here (bank account and category names are user data). The chart
	 * builders keep the raw texts; this is the single place where they reach the chart.
	 */
	safe_chart_data(data) {
		const safe = Object.assign({}, data, {
			labels: (data.labels || []).map((label) => this.esc(label)),
			datasets: (data.datasets || []).map((d) => Object.assign({}, d, { name: this.esc(d.name) })),
		});
		if (data.yMarkers) {
			safe.yMarkers = data.yMarkers.map((m) => Object.assign({}, m, { label: this.esc(m.label) }));
		}
		return safe;
	}

	safe_tooltip(options) {
		// the chart hands back the escaped label: format the raw one, then escape the result
		const raw = new Map();
		(options.data.labels || []).forEach((label) => raw.set(this.esc(label), label));
		const format_x = options.format_x || ((label) => label);
		return {
			formatTooltipX: (label) => this.esc(format_x(raw.has(label) ? raw.get(label) : label)),
			formatTooltipY: (value) => this.esc(format_currency(value, this.data.company_currency)),
		};
	}

	mount_charts() {
		const phone = this.is_phone();
		this.$body.find("[data-chart]").each((_i, el) => {
			const build = this.charts[cint($(el).attr("data-chart"))];
			const options = build && build();
			const datasets = (options && options.data.datasets) || [];
			const has_values = datasets.some((d) => (d.values || []).some((v) => flt(v)));
			if (!options || !(options.data.labels || []).length || !has_values) {
				// a flat chart says nothing: the empty state instead
				$(el).replaceWith(this.empty());
				return;
			}
			try {
				new frappe.Chart(el, {
					data: this.safe_chart_data(options.data),
					type: options.type,
					height: phone ? 200 : 240,
					colors: options.colors,
					barOptions: options.bar_options,
					lineOptions: { regionFill: 0, hideDots: this.data.horizon > 14 ? 1 : 0 },
					axisOptions: { xIsSeries: 1, shortenYAxisNumbers: 1 },
					tooltipOptions: this.safe_tooltip(options),
				});
			} catch (e) {
				console.error(e);
				$(el).replaceWith(this.empty());
			}
		});
	}
};
