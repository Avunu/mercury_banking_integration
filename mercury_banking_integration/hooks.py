# Copyright (c) 2026, Avunu LLC and contributors
# For license information, please see license.txt

app_name = "mercury_banking_integration"
app_title = "Mercury Banking for ERPNext"
app_publisher = "Avunu LLC"
app_description = "Mercury bank sync, ACH payouts, invoicing and GL coding for ERPNext"
app_email = "mail@avu.nu"
app_license = "MIT"

required_apps = ["erpnext", "payments", "hrms"]

# regenerate doctype controller type annotations on migrate (strong typing)
export_python_type_annotations = True

doc_events = {
	"Bank Transaction": {
		"on_submit": [
			"mercury_banking_integration.ar.funding.process_ar_funding_transaction",
			"mercury_banking_integration.payouts.reconcile.reconcile_payout_bank_transaction",
		],
	},
	"Payment Request": {
		"on_cancel": "mercury_banking_integration.ar.invoices.cancel_mercury_invoice",
	},
	"Salary Slip": {
		"on_cancel": "mercury_banking_integration.payouts.payroll.block_cancel_if_active_payout",
	},
}

scheduler_events = {
	"cron": {
		"*/15 * * * *": [
			"mercury_banking_integration.tasks.poll_events",
			"mercury_banking_integration.ar.invoices.poll_open_invoices",
			"mercury_banking_integration.payouts.send.poll_approval_requests",
		],
	},
	"hourly": [
		"mercury_banking_integration.ar.invoices.retry_failed_invoice_creation",
	],
	"hourly_long": [
		"mercury_banking_integration.tasks.sync_all_accounts",
	],
	"daily": [
		"mercury_banking_integration.tasks.backfill_auto_journals",
		"mercury_banking_integration.tasks.check_webhook_health",
		"mercury_banking_integration.ar.invoices.send_overdue_reminders",
		"mercury_banking_integration.payouts.recipients.sync_recipients",
		"mercury_banking_integration.payouts.reconcile.reconcile_pending_payouts",
	],
}

doctype_js = {
	"Payment Request": "public/js/payment_request.js",
	"Payroll Entry": "public/js/payroll_entry.js",
	"Payment Entry": "public/js/payment_entry.js",
	"Employee": ["public/js/mercury_recipient.js", "public/js/employee.js"],
	"Supplier": ["public/js/mercury_recipient.js", "public/js/supplier.js"],
}

# NOTE: doc_events and doctype_js registrations are added incrementally as
# their target modules land (plan phases P4-P7).


# frappe-nix:rename-shim-begin mercury_integration
# mercury_integration's whitelisted methods under their old dotted paths, for callers that still use
# them (cached bundles, webhooks, bookmarks). Written by frappe-rename-app; keep it for
# at least one minor release after the rename.
override_whitelisted_methods = {
	**globals().get("override_whitelisted_methods", {}),
	"mercury_integration.ar.invoices.recreate_mercury_invoice": "mercury_banking_integration.ar.invoices.recreate_mercury_invoice",
	"mercury_integration.payouts.payroll.get_payroll_payout_preview": "mercury_banking_integration.payouts.payroll.get_payroll_payout_preview",
	"mercury_integration.payouts.payroll.initiate_payroll_payouts": "mercury_banking_integration.payouts.payroll.initiate_payroll_payouts",
	"mercury_integration.payouts.recipients.send_invite": "mercury_banking_integration.payouts.recipients.send_invite",
	"mercury_integration.payouts.recipients.refresh_recipient": "mercury_banking_integration.payouts.recipients.refresh_recipient",
	"mercury_integration.payouts.recipients.create_recipient_directly": "mercury_banking_integration.payouts.recipients.create_recipient_directly",
	"mercury_integration.payouts.recipients.list_unmatched_recipients": "mercury_banking_integration.payouts.recipients.list_unmatched_recipients",
	"mercury_integration.payouts.recipients.match_recipient": "mercury_banking_integration.payouts.recipients.match_recipient",
	"mercury_integration.payouts.recipients.link_existing_recipients": "mercury_banking_integration.payouts.recipients.link_existing_recipients",
	"mercury_integration.payouts.vendor.pay_payment_entry_via_mercury": "mercury_banking_integration.payouts.vendor.pay_payment_entry_via_mercury",
	"mercury_integration.sync.accounts.sync_mercury_accounts": "mercury_banking_integration.sync.accounts.sync_mercury_accounts",
	"mercury_integration.sync.events.retry_event": "mercury_banking_integration.sync.events.retry_event",
	"mercury_integration.sync.events.register_webhook_endpoint": "mercury_banking_integration.sync.events.register_webhook_endpoint",
	"mercury_integration.sync.gl_codes.preview_gl_codes": "mercury_banking_integration.sync.gl_codes.preview_gl_codes",
	"mercury_integration.sync.gl_codes.export_gl_codes": "mercury_banking_integration.sync.gl_codes.export_gl_codes",
	"mercury_integration.webhooks.webhook": "mercury_banking_integration.webhooks.webhook",
}
# frappe-nix:rename-shim-end
