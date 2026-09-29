app_name = "treasury"
app_title = "Treasury"
app_publisher = "Ahmed Abdelmageed"
app_description = (
	"Post-dated cheques, security cheques, multi-cheque vouchers and bank facilities for ERPNext"
)
app_email = "ahmed3mageed@gmail.com"
app_license = "mit"
app_logo_url = "/assets/treasury/icons/desktop_icons/solid/treasury.svg"

# Apps
# ------------------

required_apps = ["erpnext"]

# Each item in the list will be shown as an app in the apps page
# add_to_apps_screen = [
# 	{
# 		"name": "treasury",
# 		"logo": "/assets/treasury/logo.png",
# 		"title": "Treasury",
# 		"route": "/treasury",
# 		"has_permission": "treasury.api.permission.has_app_permission"
# 	}
# ]

# Includes in <head>
# ------------------

# include js, css files in header of desk.html
# app_include_css = "/assets/treasury/css/treasury.css"
# app_include_js = "/assets/treasury/js/treasury.js"

# include js, css files in header of web template
# web_include_css = "/assets/treasury/css/treasury.css"
# web_include_js = "/assets/treasury/js/treasury.js"

# include custom scss in every website theme (without file extension ".scss")
# website_theme_scss = "treasury/public/scss/website"

# include js, css files in header of web form
# webform_include_js = {"doctype": "public/js/doctype.js"}
# webform_include_css = {"doctype": "public/css/doctype.css"}

# include js in page
# page_js = {"page" : "public/js/file.js"}

# include js in doctype views
# Plain files (no bundle): a "Managed by Treasury" intro and a link back to the source document.
doctype_js = {
	"Payment Entry": "public/js/voucher_treasury_link.js",
	"Journal Entry": "public/js/voucher_treasury_link.js",
}
# doctype_list_js = {"doctype" : "public/js/doctype_list.js"}
# doctype_tree_js = {"doctype" : "public/js/doctype_tree.js"}
# doctype_calendar_js = {"doctype" : "public/js/doctype_calendar.js"}

# Svg Icons
# ------------------
# include app icons in desk
# app_include_icons = "treasury/public/icons.svg"

# Home Pages
# ----------

# application home page (will override Website Settings)
# home_page = "login"

# website user home page (by Role)
# role_home_page = {
# 	"Role": "home_page"
# }

# Generators
# ----------

# automatically create page for each record of this doctype
# website_generators = ["Web Page"]

# automatically load and sync documents of this doctype from downstream apps
# importable_doctypes = [doctype_1]

# Jinja
# ----------

# add methods and filters to jinja environment (used by the PDC print formats)
jinja = {
	"methods": [
		"treasury.utils.amount_in_words.treasury_amount_in_words",
		"treasury.utils.print_helpers.treasury_print_direction",
		"treasury.utils.print_helpers.treasury_print_date",
		"treasury.utils.print_helpers.treasury_batch_receipt",
	],
}

# Installation
# ------------

# before_install = "treasury.install.before_install"
after_install = "treasury.install.after_install"
after_migrate = "treasury.install.after_migrate"

# Uninstallation
# ------------

before_uninstall = "treasury.install.before_uninstall"
# after_uninstall = "treasury.uninstall.after_uninstall"

# Integration Setup
# ------------------
# To set up dependencies/integrations with other apps
# Name of the app being installed is passed as an argument

# before_app_install = "treasury.utils.before_app_install"
# after_app_install = "treasury.utils.after_app_install"

# Integration Cleanup
# -------------------
# To clean up dependencies/integrations with other apps
# Name of the app being uninstalled is passed as an argument

# before_app_uninstall = "treasury.utils.before_app_uninstall"
# after_app_uninstall = "treasury.utils.after_app_uninstall"

# Build
# ------------------
# To hook into the build process

# after_build = "treasury.build.after_build"

# Desk Notifications
# ------------------
# See frappe.core.notifications.get_notification_config

# notification_config = "treasury.notifications.get_notification_config"

# Awesome Bar
# -----------
# Extra search results: list of dicts with label, description, route, index.
# route: ["List", "ToDo"], "/desk/docs/some/page", or "https://example.com"
# awesomebar_search = ["treasury.search.awesomebar_results"]

# Permissions
# -----------
# Permissions evaluated in scripted ways

# permission_query_conditions = {
# 	"Event": "frappe.desk.doctype.event.event.get_permission_query_conditions",
# }
#
# has_permission = {
# 	"Event": "frappe.desk.doctype.event.event.has_permission",
# }

# Document Events
# ---------------
# Hook on document methods and events

doc_events = {
	"Payment Entry": {
		"before_cancel": "treasury.overrides.voucher_guard.before_cancel",
		"on_trash": "treasury.overrides.voucher_guard.on_trash",
	},
	"Journal Entry": {
		"before_cancel": "treasury.overrides.voucher_guard.before_cancel",
		"on_trash": "treasury.overrides.voucher_guard.on_trash",
	},
}

# Scheduled Tasks
# ---------------

# `daily_long`: the posting job may submit many Payment Entries (catch-up after days without a run).
# Bank facilities: expiry / schedule statuses / utilisation first, then the month-end interest accruals
# (idempotent per drawdown and month, catches up missed months), then the alert digest.
scheduler_events = {
	"daily": [
		"treasury.security_cheques.scheduler.send_security_expiry_alerts",
	],
	"daily_long": [
		"treasury.pdc.scheduler.post_due_party_postings",
		"treasury.pdc.scheduler.send_due_alerts",
		"treasury.bank_facilities.scheduler.refresh_facilities",
		"treasury.bank_facilities.interest.post_interest_accruals",
		"treasury.bank_facilities.scheduler.send_facility_alerts",
	],
}

# Accounting dimensions (Cost Center-like Link fields ERPNext adds per Accounting Dimension), merged with
# ERPNext's own list; the doctypes have the `accounting_dimensions_section` / `dimension_col_break` fields.
accounting_dimension_doctypes = ["PDC Cheque", "Cheque Batch"]

# Extra expected cash flows and summary cards for the Bank Position Forecast report (dotted paths; see
# treasury.pdc.forecast_sources).
treasury_forecast_sources = [
	"treasury.bank_facilities.forecast.get_flows",
	"treasury.bank_facilities.lc.get_flows",
]
treasury_forecast_summary_sources = ["treasury.bank_facilities.forecast.get_summary"]

# Utilisation of Bank Facility sub-limits from other documents (Letters of Guarantee / Credit, Cheque
# Discounting); see treasury.bank_facilities.utilisation.
treasury_facility_utilisation_sources = [
	"treasury.bank_facilities.lg.get_lg_utilisation",
	"treasury.bank_facilities.lc.get_lc_utilisation",
	"treasury.bank_facilities.discounting.get_discounting_utilisation",
]

# scheduler_events = {
# 	"all": [
# 		"treasury.tasks.all"
# 	],
# 	"daily": [
# 		"treasury.tasks.daily"
# 	],
# 	"hourly": [
# 		"treasury.tasks.hourly"
# 	],
# 	"weekly": [
# 		"treasury.tasks.weekly"
# 	],
# 	"monthly": [
# 		"treasury.tasks.monthly"
# 	],
# }

# Testing
# -------

# before_tests = "treasury.install.before_tests"

# Extend DocType Class
# ------------------------------
#
# Specify custom mixins to extend the standard doctype controller.
# extend_doctype_class = {
# 	"Task": "treasury.custom.task.CustomTaskMixin"
# }

# Overriding Methods
# ------------------------------
#
# override_whitelisted_methods = {
# 	"frappe.desk.doctype.event.event.get_events": "treasury.event.get_events"
# }
#
# each overriding function accepts a `data` argument;
# generated from the base implementation of the doctype dashboard,
# along with any modifications made in other Frappe apps
# override_doctype_dashboards = {
# 	"Task": "treasury.task.get_dashboard_data"
# }

# exempt linked doctypes from being automatically cancelled
#
# auto_cancel_exempted_doctypes = ["Auto Repeat"]

# Ignore links to specified DocTypes when deleting documents
# -----------------------------------------------------------

# ignore_links_on_delete = ["Communication", "ToDo"]

# Request Events
# ----------------
# before_request = ["treasury.utils.before_request"]
# after_request = ["treasury.utils.after_request"]

# Job Events
# ----------
# before_job = ["treasury.utils.before_job"]
# after_job = ["treasury.utils.after_job"]

# User Data Protection
# --------------------

# user_data_fields = [
# 	{
# 		"doctype": "{doctype_1}",
# 		"filter_by": "{filter_by}",
# 		"redact_fields": ["{field_1}", "{field_2}"],
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_2}",
# 		"filter_by": "{filter_by}",
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_3}",
# 		"strict": False,
# 	},
# 	{
# 		"doctype": "{doctype_4}"
# 	}
# ]

# Authentication and authorization
# --------------------------------

# auth_hooks = [
# 	"treasury.auth.validate"
# ]

# Automatically update python controller files with type annotations for this app.
# export_python_type_annotations = True

# default_log_clearing_doctypes = {
# 	"Logging DocType Name": 30  # days to retain logs
# }

# Translation
# ------------
# List of apps whose translatable strings should be excluded from this app's translations.
# ignore_translatable_strings_from = []
