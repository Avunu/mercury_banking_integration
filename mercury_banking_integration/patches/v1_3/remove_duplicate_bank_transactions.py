# Copyright (c) 2026, Avunu LLC and contributors
# For license information, please see license.txt

"""Unwind the duplicate Bank Transactions the concurrent-upsert race created.

See ``sync/duplicates.py``. Once each Mercury transaction has one Bank Transaction
again, the internal transfers the copies had made ambiguous have a single
counter-side, and the transfer backfill books them (when that feature is on).
Whatever the cleanup could not settle on its own lands in the Error Log.
"""

from __future__ import annotations

import json

import frappe

from mercury_banking_integration.sync.client_factory import get_settings
from mercury_banking_integration.sync.duplicates import remove_duplicate_bank_transactions
from mercury_banking_integration.sync.transfers import reconcile_existing_transfers


def execute() -> None:
	report = remove_duplicate_bank_transactions(dry_run=False)
	if report["needs_review"] or report["failed"]:
		frappe.log_error(
			title="Mercury duplicate Bank Transactions need review",
			message=json.dumps(report, indent=1, default=str),
		)
	if not report["bank_transactions_cancelled"]:
		return

	settings = get_settings()
	if settings.enabled and settings.auto_reconcile_transfers:
		reconcile_existing_transfers(dry_run=False)
