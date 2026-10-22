// Copyright (c) 2026, Avunu LLC and contributors
// For license information, please see license.txt

/**
 * A row of `get_payroll_payout_preview` (mercury_banking_integration/payouts/payroll.py).
 * @typedef {object} PayrollPayoutPreviewRow
 * @property {string} salary_slip
 * @property {string} employee
 * @property {string | null} employee_name
 * @property {number} amount
 * @property {string} recipient_status
 * @property {string} payout_status
 * @property {boolean} sendable
 * @property {string | null} warning
 */

/**
 * `mercury_payroll_progress`, published by `run_payroll_payouts` after each slip.
 * @typedef {object} PayrollProgressEvent
 * @property {string} payroll_entry
 * @property {number} done
 * @property {number} total
 * @property {string} slip
 */

/**
 * `mercury_payroll_done`, published by `run_payroll_payouts` at the end of the run.
 * @typedef {object} PayrollDoneEvent
 * @property {string} payroll_entry
 * @property {string} summary
 */

frappe.ui.form.on("Payroll Entry", {
	// setup runs once per form, so the handlers are registered once. Registered on
	// refresh, they piled up and each run's alert and reload repeated per refresh.
	setup(frm) {
		frappe.realtime.on("mercury_payroll_progress", (/** @type {PayrollProgressEvent} */ data) => {
			if (data.payroll_entry === frm.doc?.name) {
				frappe.show_progress(__("Mercury Payouts"), data.done, data.total, data.slip);
			}
		});
		frappe.realtime.on("mercury_payroll_done", (/** @type {PayrollDoneEvent} */ data) => {
			if (data.payroll_entry === frm.doc?.name) {
				frappe.hide_progress();
				frappe.show_alert({ message: data.summary, indicator: "green" });
				frm.reload_doc();
			}
		});
	},
	refresh(frm) {
		if (frm.doc.docstatus !== 1) {
			return;
		}
		frm.add_custom_button(__("Pay via Mercury"), () => mercury_payroll_dialog(frm), __("Mercury"));
	},
});

/**
 * Previews the submitted slips and sends the ones the user ticks.
 * @param {frappe.ui.form.Form<"Payroll Entry">} frm
 */
function mercury_payroll_dialog(frm) {
	frappe
		.call("mercury_banking_integration.payouts.payroll.get_payroll_payout_preview", {
			payroll_entry: frm.doc.name,
		})
		.then((/** @type {import("frappe-types").FrappeResponse<PayrollPayoutPreviewRow[]>} */ response) => {
			const rows = response.message || [];
			if (!rows.length) {
				frappe.msgprint(__("No submitted salary slips found for this Payroll Entry."));
				return;
			}
			const dialog = new frappe.ui.Dialog({
				title: __("Pay via Mercury"),
				size: "extra-large",
				fields: [{ fieldname: "preview_html", fieldtype: "HTML" }],
				primary_action_label: __("Send Payouts"),
				primary_action() {
					/** @type {string[]} */
					const selected = [];
					dialog.$wrapper.find("input.mercury-slip:checked").each(function () {
						selected.push(String($(this).val()));
					});
					if (!selected.length) {
						frappe.msgprint(__("Select at least one salary slip."));
						return;
					}
					frappe.confirm(__("Send {0} ACH payment(s) via Mercury now?", [selected.length]), () => {
						frappe
							.call("mercury_banking_integration.payouts.payroll.initiate_payroll_payouts", {
								payroll_entry: frm.doc.name,
								salary_slips: selected,
							})
							.then(() => dialog.hide());
					});
				},
			});
			// an HTML field is a control (get_field's union also covers sections and columns)
			const preview = /** @type {frappe.ui.form.Control} */ (dialog.get_field("preview_html"));
			preview.$wrapper.html(render_mercury_preview(rows));
			dialog.show();
		});
}

/**
 * @param {PayrollPayoutPreviewRow[]} rows
 * @returns {string} the preview table's HTML
 */
function render_mercury_preview(rows) {
	const body = rows
		.map((row) => {
			const checkbox = row.sendable
				? `<input type="checkbox" class="mercury-slip" value="${row.salary_slip}" checked>`
				: `<input type="checkbox" disabled>`;
			const warning = row.warning
				? `<div class="text-danger small">${frappe.utils.escape_html(row.warning)}</div>`
				: "";
			// @ts-expect-error format_currency is a frappe global (number_format.js:145) frappe-types 16.5.0 does not declare
			const amount = format_currency(row.amount, "USD");
			const recipient =
				row.recipient_status === "Active"
					? `<span class="indicator-pill green">${__("Active")}</span>`
					: `<span class="indicator-pill red">${frappe.utils.escape_html(row.recipient_status || __("No recipient"))}</span>`;
			return `<tr>
				<td>${checkbox}</td>
				<td><a href="/app/salary-slip/${row.salary_slip}">${row.salary_slip}</a></td>
				<td>${frappe.utils.escape_html(row.employee_name || row.employee)}</td>
				<td class="text-right">${amount}</td>
				<td>${recipient}</td>
				<td>${frappe.utils.escape_html(row.payout_status || "")}${warning}</td>
			</tr>`;
		})
		.join("");
	return `<table class="table table-bordered">
		<thead><tr>
			<th></th><th>${__("Salary Slip")}</th><th>${__("Employee")}</th>
			<th class="text-right">${__("Net Pay")}</th><th>${__("Recipient")}</th><th>${__("Status")}</th>
		</tr></thead>
		<tbody>${body}</tbody>
	</table>`;
}
