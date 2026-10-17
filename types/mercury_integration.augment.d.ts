// Copyright (c) 2026, Avunu LLC and contributors
// For license information, please see license.txt

// Globals this app defines for its own desk scripts. A script file (no imports or
// exports), so these declarations are global. frappe's globals are not declared
// here: they come from frappe-types.

/** The party types that can hold a Mercury recipient. */
type MercuryRecipientPartyType = "Employee" | "Supplier";

/** `window.mercury_integration`, set up by `public/js/mercury_recipient.js`. */
interface MercuryIntegrationDesk {
	/** Adds the Mercury recipient buttons to an Employee or Supplier form. */
	add_recipient_buttons(
		frm: frappe.ui.form.Form<"Employee"> | frappe.ui.form.Form<"Supplier">,
		party_type: MercuryRecipientPartyType,
	): void;
}

interface Window {
	// app-owned: set by mercury_recipient.js, which hooks.py loads before employee.js and supplier.js
	mercury_integration: MercuryIntegrationDesk;
}
