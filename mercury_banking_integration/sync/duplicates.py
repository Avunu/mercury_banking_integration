# Copyright (c) 2026, Avunu LLC and contributors
# For license information, please see license.txt

"""Unwind duplicate Bank Transactions left by the concurrent-upsert race.

Until ``upsert_transaction`` committed inside its per-transaction lock, the jobs
for one Mercury transaction's create and update events, run in parallel, could
each insert a Bank Transaction for it. Every downstream matcher keys on the
Mercury transaction id, so each copy was then booked or reconciled on its own:
an auto Journal Entry per copy, a payroll payout allocated twice against the
payroll bank entry, and internal transfers left "ambiguous" between the copies.

Per Mercury transaction id with more than one live Bank Transaction, one copy
survives — the first reconciled, else the first submitted, else the oldest — and
every other copy is unwound:

- a voucher the survivor also reconciles (the payroll bank entry, a transfer's
  Bank Entry) is only unallocated: the copy is cancelled, and since cancelling a
  Bank Transaction clears its vouchers' clearance date, that date is put back;
- the copy's own auto Journal Entry (created at reconciliation, ``cheque_no`` =
  the Mercury id) books the bank movement a second time and is cancelled;
- a copy reconciled against anything else is left alone and reported.

Payouts a stale job left at Sent/Posted are set Reconciled when the surviving
Bank Transaction is. Idempotent; dry-run by default:

	bench execute \\
		mercury_banking_integration.sync.duplicates.remove_duplicate_bank_transactions --kwargs '{"dry_run": False}'
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple, cast

import frappe
from frappe.query_builder import DocType
from frappe.query_builder.functions import Count

from mercury_banking_integration.payouts.send import PAYOUT_DOCTYPES
from mercury_banking_integration.sync.transfers import UUID_SQL

if TYPE_CHECKING:
	from erpnext.accounts.doctype.bank_transaction.bank_transaction import (  # frappe-vulture:ignore
		BankTransaction,
	)

SAVEPOINT = "mercury_duplicate"

Voucher = tuple[str, str]


class Unwound(NamedTuple):
	"""What unwinding one Mercury transaction's copies did (or, in a dry run, would do)."""

	survivor: str
	cancelled: list[str]
	journal_entries: list[str]
	review: list[str]


def _duplicate_groups() -> dict[str, list[frappe._dict]]:
	"""{Mercury transaction id: its live Bank Transactions, oldest first}."""
	bank_transaction = DocType("Bank Transaction")
	transaction_ids = (
		frappe.qb.from_(bank_transaction)
		.select(bank_transaction.transaction_id)
		.where((bank_transaction.docstatus < 2) & bank_transaction.transaction_id.regexp(UUID_SQL))
		.groupby(bank_transaction.transaction_id)
		.having(Count("*") > 1)
		.run(pluck=True)
	)
	groups: dict[str, list[frappe._dict]] = {}
	if not transaction_ids:
		return groups
	for row in frappe.get_all(
		"Bank Transaction",
		filters={"transaction_id": ("in", transaction_ids), "docstatus": ("<", 2)},
		fields=["name", "docstatus", "transaction_id"],
		order_by="creation asc, name asc",
	):
		groups.setdefault(row.transaction_id, []).append(row)
	return groups


def _payments(bank_transaction: str) -> list[frappe._dict]:
	return frappe.get_all(
		"Bank Transaction Payments",
		filters={"parent": bank_transaction, "parenttype": "Bank Transaction"},
		fields=["payment_document", "payment_entry", "reconciliation_type"],
	)


def _voucher(payment: frappe._dict) -> Voucher:
	return str(payment.payment_document), str(payment.payment_entry)


def _clearance_date(voucher: Voucher):
	doctype, name = voucher
	if not frappe.get_meta(doctype).has_field("clearance_date"):
		return None
	return frappe.db.get_value(doctype, name, "clearance_date")


def _cancel_copy(copy: frappe._dict, own_journals: list[str], shared: list[Voucher]) -> None:
	for journal_entry in own_journals:
		# on_cancel also drops it from the copy (remove_from_bank_transaction)
		frappe.get_doc("Journal Entry", journal_entry).cancel()

	cleared = {voucher: _clearance_date(voucher) for voucher in shared}
	if copy.docstatus == 0:
		frappe.delete_doc("Bank Transaction", str(copy.name), ignore_permissions=True, force=True)
	else:
		doc = cast("BankTransaction", frappe.get_doc("Bank Transaction", str(copy.name)))
		doc.flags.ignore_permissions = True
		doc.cancel()  # delinks the shared vouchers, clearing their clearance date
	for (doctype, name), clearance_date in cleared.items():
		if clearance_date:
			frappe.db.set_value(doctype, name, "clearance_date", clearance_date, update_modified=False)


def _unwind_group(transaction_id: str, group: list[frappe._dict], dry_run: bool) -> Unwound:
	payments = {str(row.name): _payments(str(row.name)) for row in group}
	survivor = next((row for row in group if payments[str(row.name)]), None) or next(
		(row for row in group if row.docstatus == 1), group[0]
	)
	kept = {_voucher(payment) for payment in payments[str(survivor.name)]}

	result = Unwound(survivor=str(survivor.name), cancelled=[], journal_entries=[], review=[])
	for copy in group:
		if copy.name == survivor.name:
			continue
		own_journals: list[str] = []
		shared: list[Voucher] = []
		foreign = False
		for payment in payments[str(copy.name)]:
			voucher = _voucher(payment)
			if voucher in kept:
				shared.append(voucher)
			elif (
				payment.payment_document == "Journal Entry"
				and payment.reconciliation_type == "Voucher Created"
				and frappe.db.get_value("Journal Entry", payment.payment_entry, "cheque_no") == transaction_id
			):
				own_journals.append(str(payment.payment_entry))
			else:
				foreign = True
		if foreign:
			result.review.append(str(copy.name))
			continue

		if not dry_run:
			_cancel_copy(copy, own_journals, shared)
		result.cancelled.append(str(copy.name))
		result.journal_entries.extend(own_journals)
	return result


def _settle_payouts(transaction_id: str) -> list[str]:
	"""Payouts a stale job left at Sent/Posted though their Bank Transaction reconciled."""
	settled: list[str] = []
	if (
		frappe.db.get_value("Bank Transaction", {"transaction_id": transaction_id, "docstatus": 1}, "status")
		!= "Reconciled"
	):
		return settled
	for doctype in PAYOUT_DOCTYPES:
		for name in frappe.get_all(
			doctype,
			filters={
				"mercury_transaction_id": transaction_id,
				"mercury_payment_status": ("in", ("Sent", "Posted")),
			},
			pluck="name",
		):
			frappe.db.set_value(doctype, name, "mercury_payment_status", "Reconciled", update_modified=False)
			settled.append(f"{doctype} {name}")
	return settled


def remove_duplicate_bank_transactions(dry_run: bool = True) -> dict:
	"""Keep one Bank Transaction per Mercury transaction id; unwind and cancel the rest.

	Each Mercury transaction is unwound under its own savepoint: one that fails (a
	closed period, say) is logged and reported, and the rest still go through.
	"""
	cancelled: list[str] = []
	journal_entries: list[str] = []
	needs_review: list[str] = []
	payouts_reconciled: list[str] = []
	failed: list[str] = []
	groups = _duplicate_groups()
	for transaction_id, group in groups.items():
		frappe.db.savepoint(SAVEPOINT)
		try:
			result = _unwind_group(transaction_id, group, dry_run)
			settled = _settle_payouts(transaction_id) if result.cancelled and not dry_run else []
		except Exception:
			frappe.db.rollback(save_point=SAVEPOINT)
			frappe.log_error(title=f"Mercury duplicate cleanup failed for {transaction_id}")
			failed.append(transaction_id)
			continue
		cancelled.extend(f"{name} (kept {result.survivor})" for name in result.cancelled)
		journal_entries.extend(result.journal_entries)
		needs_review.extend(result.review)
		payouts_reconciled.extend(settled)

	return {
		"dry_run": dry_run,
		"duplicated_transactions": len(groups),
		"bank_transactions_cancelled": cancelled,
		"journal_entries_cancelled": journal_entries,
		"needs_review": needs_review,
		"payouts_reconciled": payouts_reconciled,
		"failed": failed,
	}
