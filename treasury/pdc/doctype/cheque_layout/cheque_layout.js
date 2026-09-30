// Copyright (c) 2026, Ahmed Abdelmageed and contributors
// For license information, please see license.txt

// Design 7f §4. The preview is rendered by the server (treasury.pdc.cheque_printing.preview_layout) from the
// unsaved form, in an <iframe srcdoc>: its `.print-format` / `@page` rules must not leak into the desk. The iframe
// sits under a dir="ltr" wrapper, so the physical leaf is never mirrored in the Arabic desk; only the text inside
// each box follows the language. The iframe runs no script (sandbox); this script reads it (same origin).
// Form scripts share the global scope: the names below are prefixed.
const CHEQUE_LAYOUT_PREVIEW = "treasury.pdc.cheque_printing.preview_layout";
const CHEQUE_LAYOUT_LANGUAGES = { Arabic: "ar", English: "en" };
const CHEQUE_LAYOUT_PX_PER_MM = 96 / 25.4;
const CHEQUE_LAYOUT_MAX_FIT = 1.6;
const CHEQUE_LAYOUT_STICKY_MIN_WIDTH = 1200;
const CHEQUE_LAYOUT_PIN_KEY = "treasury_cheque_layout_pin";
// Every printed box: [field prefix, class in the preview, section] (cheque_layout.py BOXES)
const CHEQUE_LAYOUT_BOXES = [
	["date", "tcl-date", "date_section"],
	["payee", "tcl-payee", "payee_section"],
	["figures", "tcl-figures", "figures_section"],
	["words1", "tcl-words-1", "words_section"],
	["words2", "tcl-words-2", "words_section"],
	["crossing", "tcl-crossing", "crossing_section"],
	["signature", "tcl-signature", "signature_section"],
];
// The fields whose change refreshes the preview: every field except the name, default / disabled flags and the
// default language (the preview has its own language toggle); the bank account sets currency and company.
const CHEQUE_LAYOUT_PREVIEW_FIELDS = [
	"bank_account",
	"test_amount",
	"date_format",
	"digits",
	"add_only_suffix",
	"show_currency_name",
	"fraction_style",
	"figures_guard",
	"currency_name_en",
	"currency_name_ar",
	"fraction_name_en",
	"fraction_name_ar",
	"leaf_width_mm",
	"leaf_height_mm",
	"paper",
	"offset_top_mm",
	"offset_left_mm",
	"nudge_x_mm",
	"nudge_y_mm",
	"font_family",
	"base_font_size_pt",
	"date_top_mm",
	"date_left_mm",
	"date_width_mm",
	"date_font_size_pt",
	"date_char_spacing_mm",
	"payee_top_mm",
	"payee_left_mm",
	"payee_width_mm",
	"payee_font_size_pt",
	"figures_top_mm",
	"figures_left_mm",
	"figures_width_mm",
	"figures_font_size_pt",
	"words1_top_mm",
	"words1_left_mm",
	"words1_width_mm",
	"words1_max_chars",
	"words2_top_mm",
	"words2_left_mm",
	"words2_width_mm",
	"words_font_size_pt",
	"print_crossing",
	"crossing_top_mm",
	"crossing_left_mm",
	"crossing_width_mm",
	"crossing_rotation_deg",
	"crossing_font_size_pt",
	"crossing_text_en",
	"crossing_text_ar",
	"signature_top_mm",
	"signature_left_mm",
	"signature_width_mm",
	"signature_height_mm",
	"signature_label",
	"signature_text",
];
const CHEQUE_LAYOUT_POSITION_SECTIONS = [
	"date_section",
	"payee_section",
	"figures_section",
	"words_section",
	"crossing_section",
	"signature_section",
];

// the preview choices are kept for the session (not per document)
let cheque_layout_preview_lang = null;
let cheque_layout_zoom = "fit";

frappe.ui.form.on("Cheque Layout", {
	setup(frm) {
		frm.set_query("bank_account", () => ({ filters: { is_company_account: 1 } }));
	},

	onload_post_render(frm) {
		cheque_layout_watch_focus(frm);
		$(window)
			.off("resize.cheque_layout")
			.on(
				"resize.cheque_layout",
				frappe.utils.debounce(() => {
					if (window.cur_frm === frm) {
						cheque_layout_render(frm, frm.__cl_html);
						cheque_layout_update_sticky(frm);
					}
				}, 200)
			);
	},

	refresh(frm) {
		cheque_layout_set_intro(frm);
		cheque_layout_build_stage(frm);
		frm.events.render_preview(frm);
		cheque_layout_add_preview_links(frm);
		if (!frm.is_new()) {
			const $btn = frm.add_custom_button(__("Test Print"), () => cheque_layout_test_print(frm));
			$btn && $btn.removeClass("btn-default").addClass("btn-primary");
		}
	},

	render_preview(frm) {
		cheque_layout_request_preview(frm);
	},

	sample_image(frm) {
		cheque_layout_render(frm, frm.__cl_html);
	},
});

// Any position, size, wording or paper change refreshes the preview (debounced, design 7f §4.3).
const cheque_layout_refresh = frappe.utils.debounce((frm) => cheque_layout_request_preview(frm), 400);
frappe.ui.form.on(
	"Cheque Layout",
	Object.fromEntries(CHEQUE_LAYOUT_PREVIEW_FIELDS.map((fieldname) => [fieldname, (frm) => cheque_layout_refresh(frm)]))
);

// ------------------------------------------------------------------------------------ intro and headline

function cheque_layout_set_intro(frm) {
	if (frm.is_new()) {
		frm.set_intro(
			frappe.utils.escape_html(
				__(
					"1. Measure a blank cheque and enter its size under Paper. 2. Place each box using the preview. 3. Save and use Test Print on plain paper; hold it over a blank cheque against the light. 4. If every field is shifted the same way, adjust Printer Calibration instead of each box."
				)
			),
			"blue"
		);
		return;
	}
	frm.set_intro();
	cheque_layout_headline(frm);
}

function cheque_layout_headline(frm) {
	// design 7f P13: which layout really prints (cheque_printing.get_layout: the enabled default, else the newest)
	const doc = frm.doc;
	const esc = frappe.utils.escape_html;
	const link = (name, title) =>
		`<a href="/app/cheque-layout/${encodeURIComponent(name)}"><b>${esc(title || name)}</b></a>`;
	const bank = `<b><bdi>${esc(doc.bank_account)}</bdi></b>`;
	frappe.db
		.get_list("Cheque Layout", {
			filters: { bank_account: doc.bank_account, disabled: 0 },
			fields: ["name", "layout_name"],
			order_by: "is_default desc, creation desc",
			limit: 1,
		})
		.then((rows) => {
			const effective = (rows || [])[0];
			if (cint(doc.disabled)) {
				const other = effective
					? link(effective.name, effective.layout_name)
					: esc(__("the PDC Account Mapping format"));
				frm.dashboard.set_headline(__("Disabled: cheques of {0} print with {1}.", [bank, other]), "orange");
			} else if (effective && effective.name !== doc.name) {
				frm.dashboard.set_headline(
					__("Not the default: {0} is used for this bank account.", [link(effective.name, effective.layout_name)]),
					"blue"
				);
			} else if (effective) {
				frm.dashboard.set_headline(
					__("Cheques of {0} print with this layout ({1} by default).", [
						bank,
						esc(__(doc.default_language, null, "Cheque Layout")),
					]),
					"green"
				);
			}
		});
}

// ------------------------------------------------------------------------------------ preview

function cheque_layout_build_stage(frm) {
	const field = frm.get_field("preview_html");
	if (!field) {
		return;
	}
	const esc = frappe.utils.escape_html;
	if (!cheque_layout_preview_lang) {
		cheque_layout_preview_lang = frm.doc.default_language || "Arabic";
	}
	const lang_btn = (value) =>
		`<button type="button" class="btn btn-xs ${
			cheque_layout_preview_lang === value ? "btn-primary" : "btn-default"
		} cl-lang" data-value="${value}">${esc(__(value, null, "Cheque Layout"))}</button>`;
	const zoom_btn = (value, label) =>
		`<button type="button" class="btn btn-xs ${
			cheque_layout_zoom === value ? "btn-primary" : "btn-default"
		} cl-zoom" data-value="${value}" title="${esc(
			__("Screen millimetres are approximate; use Test Print to check real sizes.")
		)}">${esc(label)}</button>`;
	const pinned = cheque_layout_pinned();
	field.$wrapper.html(`
		<div class="cl-toolbar" style="display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin-bottom:8px">
			<b>${esc(__("Preview", null, "Cheque Layout"))}</b>
			<span class="btn-group">${lang_btn("Arabic")}${lang_btn("English")}</span>
			<span class="text-muted small">${esc(__("Zoom", null, "Cheque Layout"))}</span>
			<span class="btn-group">${zoom_btn("fit", __("Fit", null, "Cheque Layout"))}${zoom_btn("1:1", "1:1")}</span>
			<button type="button" class="btn btn-xs btn-default cl-refresh" title="${esc(__("Refresh"))}">
				<span class="cl-refresh-icon" style="display:inline-block">⟳</span></button>
			<button type="button" class="btn btn-xs ${pinned ? "btn-primary" : "btn-default"} cl-pin"
				title="${esc(__("Keep the preview in view while editing positions"))}">📌</button>
			${frm.is_new() ? "" : `<button type="button" class="btn btn-xs btn-default cl-test-print">${esc(__("Test Print"))}</button>`}
		</div>
		<div class="cl-stage" dir="ltr" style="overflow:auto;border:1px solid var(--border-color);border-radius:var(--border-radius);background:var(--control-bg);padding:8px"></div>
		<div class="cl-bounds small text-danger" style="margin-top:4px"></div>
		<div class="cl-error small text-danger" style="margin-top:4px"></div>
		<div class="cl-dirty small text-muted" style="margin-top:4px"></div>
	`);
	field.$wrapper.find(".cl-lang").on("click", (e) => {
		cheque_layout_preview_lang = $(e.currentTarget).attr("data-value");
		cheque_layout_build_stage(frm);
		cheque_layout_request_preview(frm);
	});
	field.$wrapper.find(".cl-zoom").on("click", (e) => {
		cheque_layout_zoom = $(e.currentTarget).attr("data-value");
		cheque_layout_build_stage(frm);
		cheque_layout_render(frm, frm.__cl_html);
	});
	field.$wrapper.find(".cl-refresh").on("click", () => cheque_layout_request_preview(frm));
	field.$wrapper.find(".cl-test-print").on("click", () => cheque_layout_test_print(frm));
	field.$wrapper.find(".cl-pin").on("click", () => {
		cheque_layout_set_pinned(!cheque_layout_pinned());
		cheque_layout_build_stage(frm);
		cheque_layout_render(frm, frm.__cl_html);
		cheque_layout_update_sticky(frm);
	});
}

function cheque_layout_request_preview(frm) {
	const field = frm.get_field("preview_html");
	if (!field || !frm.doc.bank_account) {
		return;
	}
	if (!field.$wrapper.find(".cl-stage").length) {
		cheque_layout_build_stage(frm);
	}
	// the A4 Carrier preview shows the leaf only; the old preview stays until the new one arrives
	const request = (frm.__cl_request = (frm.__cl_request || 0) + 1);
	const $icon = field.$wrapper.find(".cl-refresh-icon");
	$icon.css({ animation: "cl-spin 1s linear infinite" });
	cheque_layout_spin_style();
	frappe
		.call({
			method: CHEQUE_LAYOUT_PREVIEW,
			args: {
				doc: Object.assign({}, frm.doc, { paper: "Cheque Leaf", __onload: undefined }),
				language: cheque_layout_preview_lang || frm.doc.default_language,
			},
			error_handlers: {
				// a request error: a muted red line under the leaf, the old preview kept
				ValidationError: (r) => cheque_layout_show_error(frm, r),
				PermissionError: (r) => cheque_layout_show_error(frm, r),
			},
		})
		.then((r) => {
			if (request !== frm.__cl_request) {
				return; // a newer request is on its way
			}
			frm.__cl_html = r.message || "";
			field.$wrapper.find(".cl-error").text("");
			cheque_layout_render(frm, frm.__cl_html);
		})
		.always(() => {
			if (request === frm.__cl_request) {
				field.$wrapper.find(".cl-refresh-icon").css({ animation: "" });
			}
		});
}

function cheque_layout_show_error(frm, r) {
	let message = "";
	try {
		message = JSON.parse(r._server_messages || "[]")
			.map((m) => new DOMParser().parseFromString(JSON.parse(m).message || "", "text/html").body.textContent)
			.join(" ");
	} catch (e) {
		message = "";
	}
	frm.get_field("preview_html").$wrapper.find(".cl-error").text(message || __("The preview could not be refreshed."));
}

function cheque_layout_spin_style() {
	if (!document.getElementById("cl-spin-style")) {
		$(`<style id="cl-spin-style">@keyframes cl-spin{from{transform:rotate(0)}to{transform:rotate(360deg)}}</style>`).appendTo(
			document.head
		);
	}
}

function cheque_layout_render(frm, html) {
	const field = frm.get_field("preview_html");
	if (!field || html === undefined || html === null) {
		return;
	}
	const $stage = field.$wrapper.find(".cl-stage");
	if (!$stage.length) {
		return;
	}
	const W = flt(frm.doc.leaf_width_mm) * CHEQUE_LAYOUT_PX_PER_MM;
	const H = flt(frm.doc.leaf_height_mm) * CHEQUE_LAYOUT_PX_PER_MM;
	if (!W || !H) {
		return;
	}
	const scale = cheque_layout_zoom === "fit" ? Math.min(CHEQUE_LAYOUT_MAX_FIT, ($stage.width() - 2) / W) : 1;
	// [P10] the scan sits behind the leaf, in the preview only: never sent to the server, never printed
	const image = frm.doc.sample_image
		? `<style>.treasury-cheque-leaf{background:url("${encodeURI(frm.doc.sample_image).replace(/"/g, "%22")}") 0 0/100% 100% no-repeat !important}
			.treasury-cheque-leaf>*{background:rgba(255,255,255,.35)}</style>`
		: "";
	const interactive = "<style>.treasury-cheque-leaf .tcl-box{cursor:pointer}</style>";
	let frame = $stage.find("iframe.cl-preview")[0];
	if (!frame) {
		frame = $(`<iframe class="cl-preview" sandbox="allow-same-origin"></iframe>`).appendTo($stage)[0];
		frame.addEventListener("load", () => cheque_layout_after_load(frm, frame));
	}
	// `html` is the server's render: every text in it is escaped there
	frame.srcdoc = `<!doctype html><html><head><meta charset="utf-8"></head><body style="margin:0;transform:scale(${scale});transform-origin:0 0">${image}${interactive}${html}</body></html>`;
	$(frame).css({ width: `${W * scale}px`, height: `${H * scale}px`, border: "0", display: "block" });
	field.$wrapper.find(".cl-dirty").text(frm.is_dirty() ? __("Preview of the unsaved values.") : "");
}

function cheque_layout_after_load(frm, frame) {
	const doc = frame.contentDocument;
	if (!doc) {
		return;
	}
	// [P12] the server's own box check (ChequeLayout.validate_boxes), as a hint: the save still validates
	const outside = cheque_layout_boxes_outside(frm);
	if (outside.length) {
		const style = doc.createElement("style");
		style.textContent = outside.map((b) => `.treasury-cheque-leaf .${b[1]}{outline:.5mm solid #e03131 !important}`).join("");
		doc.head.appendChild(style);
	}
	const labels = [...new Set(outside.map((b) => cheque_layout_section_label(frm, b[2])))];
	frm.get_field("preview_html").$wrapper.find(".cl-bounds").html(
		labels
			.map((label) =>
				frappe.utils.escape_html(
					__("{0} is outside the cheque leaf ({1} × {2} mm).", [
						label,
						format_number(frm.doc.leaf_width_mm, null, 1),
						format_number(frm.doc.leaf_height_mm, null, 1),
					])
				)
			)
			.join("<br>")
	);
	// [P11] a click on a box goes to its section and focuses its Top field
	for (const [prefix, css, section] of CHEQUE_LAYOUT_BOXES) {
		doc.querySelectorAll(`.${css}`).forEach((el) =>
			el.addEventListener("click", () => cheque_layout_go_to(frm, `${prefix}_top_mm`, section))
		);
	}
}

function cheque_layout_boxes_outside(frm) {
	const doc = frm.doc;
	const width = flt(doc.leaf_width_mm);
	const height = flt(doc.leaf_height_mm);
	return CHEQUE_LAYOUT_BOXES.filter(([prefix]) => {
		const top = flt(doc[`${prefix}_top_mm`]);
		const left = flt(doc[`${prefix}_left_mm`]);
		const box_width = flt(doc[`${prefix}_width_mm`]);
		const bottom = prefix === "signature" ? top + flt(doc.signature_height_mm) : top;
		return top < 0 || left < 0 || box_width <= 0 || left + box_width > width + 0.01 || bottom > height;
	});
}

function cheque_layout_section_label(frm, section) {
	const df = frappe.meta.get_docfield("Cheque Layout", section, frm.doc.name);
	return df ? __(df.label, null, "Cheque Layout") : section;
}

function cheque_layout_go_to(frm, fieldname, section) {
	const field = frm.get_field(fieldname);
	if (!field || !field.$wrapper) {
		return;
	}
	if (field.section && field.section.is_collapsed && field.section.is_collapsed()) {
		field.section.collapse(false);
	}
	// below the pinned preview, when it is pinned
	const $preview = cheque_layout_preview_section(frm);
	const covered = $preview && $preview.hasClass("cl-sticky") ? $preview.outerHeight() : 0;
	const top = field.$wrapper.offset().top - covered - 80;
	$("html, body").animate({ scrollTop: Math.max(top, 0) }, 250, () => {
		field.$wrapper.find("input").first().focus();
	});
	const $head = field.section && field.section.wrapper;
	if ($head) {
		$head.css({ transition: "background-color .6s", "background-color": "var(--yellow-highlight-color, #fff5cc)" });
		setTimeout(() => $head.css({ "background-color": "" }), 1200);
	}
}

// ------------------------------------------------------------------------------------ [P8] pinned preview

function cheque_layout_pinned() {
	try {
		return window.localStorage.getItem(CHEQUE_LAYOUT_PIN_KEY) !== "0";
	} catch (e) {
		return true;
	}
}

function cheque_layout_set_pinned(value) {
	try {
		window.localStorage.setItem(CHEQUE_LAYOUT_PIN_KEY, value ? "1" : "0");
	} catch (e) {
		// storage blocked: the choice lasts until the page is reloaded
	}
}

function cheque_layout_preview_section(frm) {
	const field = frm.get_field("preview_html");
	return field && field.section && field.section.wrapper;
}

function cheque_layout_watch_focus(frm) {
	// the preview sticks under the page head while the cursor is in a position section (wide screens)
	const $form = frm.layout && frm.layout.wrapper;
	if (!$form || frm.__cl_watching) {
		return;
	}
	frm.__cl_watching = true;
	$form.on("focusin focusout", () => setTimeout(() => cheque_layout_update_sticky(frm), 0));
}

function cheque_layout_in_position_section(frm) {
	const active = document.activeElement;
	if (!active) {
		return false;
	}
	return CHEQUE_LAYOUT_POSITION_SECTIONS.some((section) => {
		const field = frm.get_field(section) || {};
		const $wrapper = field.wrapper || (field.section && field.section.wrapper);
		return $wrapper && $wrapper.length && $.contains($wrapper[0], active);
	});
}

function cheque_layout_update_sticky(frm) {
	const $section = cheque_layout_preview_section(frm);
	if (!$section) {
		return;
	}
	const sticky =
		cheque_layout_pinned() &&
		window.innerWidth >= CHEQUE_LAYOUT_STICKY_MIN_WIDTH &&
		cheque_layout_in_position_section(frm);
	const head = $(".page-head:visible").outerHeight() || 0;
	$section.toggleClass("cl-sticky", sticky).css(
		sticky
			? {
					position: "sticky",
					top: `calc(var(--navbar-height, 48px) + ${head}px)`,
					"z-index": 3,
					background: "var(--card-bg)",
					"box-shadow": "var(--shadow-sm)",
			  }
			: { position: "", top: "", "z-index": "", background: "", "box-shadow": "" }
	);
}

function cheque_layout_add_preview_links(frm) {
	// the fallback of the pinned preview: "↑ Preview" at the top of every position section
	for (const section of CHEQUE_LAYOUT_POSITION_SECTIONS) {
		const field = frm.get_field(section) || {};
		const $wrapper = field.wrapper || (field.section && field.section.wrapper);
		if (!$wrapper || !$wrapper.length || $wrapper.find(".cl-to-preview").length) {
			continue;
		}
		$(`<div class="text-right small" style="margin-bottom:-8px"><a class="cl-to-preview">${frappe.utils.escape_html(
			__("↑ Preview")
		)}</a></div>`)
			.prependTo($wrapper.find(".section-body").first().length ? $wrapper.find(".section-body").first() : $wrapper)
			.find("a")
			.on("click", () => {
				const $preview = cheque_layout_preview_section(frm);
				$preview && frappe.utils.scroll_to($preview, true, 60);
			});
	}
}

// ------------------------------------------------------------------------------------ test print

function cheque_layout_test_print(frm) {
	const open = () => {
		const dialog = new frappe.ui.Dialog({
			title: __("Test Print"),
			fields: [
				{
					fieldname: "language",
					fieldtype: "Select",
					label: __("Language", null, "Cheque Layout"),
					options: Object.keys(CHEQUE_LAYOUT_LANGUAGES).map((value) => ({
						value,
						label: __(value, null, "Cheque Layout"),
					})),
					default: cheque_layout_preview_lang || frm.doc.default_language || "Arabic",
					reqd: 1,
				},
				{
					fieldname: "help",
					fieldtype: "HTML",
					options: `<div class="alert alert-info small">${frappe.utils.escape_html(
						__(
							'Print on plain paper at 100 % / Actual size — not "Fit to page" — with margins None and browser headers and footers off. Hold the print over a blank cheque against the light.'
						)
					)}</div>`,
				},
			],
			primary_action_label: __("Open Test Print"),
			primary_action(values) {
				// opened in the click handler, so the browser does not block it (design 7f §6.4)
				const win = treasury.print.open_window();
				dialog.hide();
				const params = new URLSearchParams({
					doctype: frm.doctype,
					name: frm.doc.name,
					format: "Cheque Layout Test Print",
					no_letterhead: 1,
					trigger_print: 1,
					_lang: CHEQUE_LAYOUT_LANGUAGES[values.language] || "en",
				});
				win.go(`/printview?${params.toString()}`);
			},
		});
		dialog.show();
	};
	// the test print is of the saved layout
	if (frm.is_dirty()) {
		frm.save().then(open);
	} else {
		open();
	}
}

// Design 7f §6.4: a print window opened synchronously in the click handler (never blocked as a popup); its URL
// is set once the server answered. Defined once by whichever print form loads first (there are no bundles).
frappe.provide("treasury.print");
if (!treasury.print.open_window) {
	treasury.print.open_window = function () {
		const win = window.open("", "_blank");
		if (win) {
			win.document.write(
				`<p style="font-family:sans-serif">${frappe.utils.escape_html(__("Preparing the print…"))}</p>`
			);
		}
		return {
			go: (url) => (win ? (win.location.href = url) : window.open(url)),
			close: () => win && win.close(),
		};
	};
}
