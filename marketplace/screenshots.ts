// Copyright (c) 2026, Avunu LLC and contributors
// For license information, please see license.txt

import type { ShotSpec } from "./shots.d.ts";

// The demo from mercury_banking_integration/demo.py: two synced Mercury accounts with three
// weeks of transactions, Mercury Settings filled in, and a submitted payroll run.
const CHECKING = "Mercury Checking - Mercury";

const GROUP = '.inner-group-button[data-label="Mercury"]';

// Runs one item of the form's "Mercury" button group, the way choosing it from the menu does.
const runMenuItem = (label: string) => ({
	eval: `$(${JSON.stringify(`${GROUP} .dropdown-menu a`)}).filter((_, a) => $(a).text().trim() === ${JSON.stringify(label)}).trigger("click");`,
});

export default {
	demo: { date: "2026-03-13", seed: 1 },
	shots: [
		{
			name: "settings",
			route: "/app/mercury-settings",
			alt: "Mercury Settings with sandbox credentials, automatic sync and auto journal entries turned on, and the Mercury menu open",
			waitForFn:
				'window.cur_frm && cur_frm.doctype === "Mercury Settings" && cur_frm.doc.enabled === 1',
			actions: [{ waitFor: GROUP }, { click: `${GROUP} > button` }, { wait: 500 }],
			mask: [".frappe-timestamp"],
			readme: "hero",
			featured: true,
		},
		{
			name: "bank-transactions",
			route: `/app/bank-transaction?bank_account=${encodeURIComponent(CHECKING)}`,
			alt: "Bank Transactions imported from Mercury Checking, the GL-coded ones and the internal transfer already reconciled",
			waitFor: ".list-row-container",
			mask: [".frappe-timestamp"],
			readme: "feature",
			featured: true,
		},
		{
			name: "pay-via-mercury",
			route: "/app/payroll-entry/HR-PRUN-DEMO-00001",
			alt: "The Pay via Mercury preview on a payroll run: two employees ready to pay by ACH, one with an open invite and one without a Mercury payee",
			waitForFn: 'window.cur_frm && cur_frm.doc.name === "HR-PRUN-DEMO-00001"',
			actions: [
				{ waitFor: GROUP },
				runMenuItem("Pay via Mercury"),
				{ waitFor: ".modal.show input.mercury-slip" },
				{ wait: 500 },
			],
			// The timestamps sit under the dialog's backdrop; masking them would paint over the dialog.
			hide: [".frappe-timestamp"],
			readme: "feature",
			featured: true,
		},
	],
} satisfies ShotSpec;
