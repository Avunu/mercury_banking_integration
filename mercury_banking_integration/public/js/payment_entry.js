// Copyright (c) 2026, Avunu LLC and contributors
// For license information, please see license.txt

/**
 * What `pay_payment_entry_via_mercury` returns (mercury_banking_integration/payouts/send.py `send_payout`):
 * one of these keys says what happened.
 * @typedef {object} PayoutResult
 * @property {string} [sent] the Mercury transaction id
 * @property {string} [pending_approval] the Mercury send-money request id
 * @property {string} [skipped] why nothing was sent
 * @property {string} [failed] the error
 */

frappe.ui.form.on("Payment Entry", {
	refresh(frm) {
		const doc = frm.doc;
		const sendable =
			doc.docstatus === 1 &&
			doc.payment_type === "Pay" &&
			doc.party_type === "Supplier" &&
			!doc.clearance_date &&
			// send.py SENDABLE_STATUSES: the server turns a Blocked - Duplicate retry away until 24h have passed
			["", "Failed", "Rejected", "Cancelled", "Blocked - Duplicate"].includes(
				doc.mercury_payment_status || "",
			);
		if (!sendable) {
			return;
		}
		frm.add_custom_button(
			__("Pay via Mercury"),
			() => {
				// @ts-expect-error format_currency is a frappe global (number_format.js:145) frappe-types 16.5.0 does not declare
				const amount = format_currency(doc.paid_amount, doc.paid_from_account_currency || "USD");
				frappe.confirm(__("Send {0} to {1} via Mercury ACH?", [amount, doc.party_name || doc.party]), () => {
					frappe
						.call("mercury_banking_integration.payouts.vendor.pay_payment_entry_via_mercury", {
							payment_entry: doc.name,
						})
						.then((/** @type {import("frappe-types").FrappeResponse<PayoutResult>} */ response) => {
							const result = response.message || {};
							if (result.sent || result.pending_approval) {
								frappe.show_alert({
									message: result.sent
										? __("Payment sent: {0}", [result.sent])
										: __("Queued for approval in Mercury"),
									indicator: "green",
								});
							} else {
								frappe.msgprint(result.failed || result.skipped || __("No action taken"));
							}
							frm.reload_doc();
						});
				});
			},
			__("Mercury"),
		);
	},
});
