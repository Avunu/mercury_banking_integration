# Copyright (c) 2026, Avunu LLC and contributors
# For license information, please see license.txt

"""The one-shot cutover tools (bench execute), Mercury API mocked."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any, cast

import frappe

from mercury_banking_integration import migrate
from mercury_banking_integration.tests.utils import (
	CHECKING_ID,
	COMPANY,
	MercuryTestCase,
	transaction_payload,
	uuid,
)

if TYPE_CHECKING:
	from frappe.core.doctype.file.file import File  # frappe-vulture:ignore

DAY = "2026-08-01"


class MigrateTestCase(MercuryTestCase):
	def setUp(self) -> None:
		super().setUp()
		self.configure()

	def bank_transaction(self, transaction_id: str, amount: float, day: str = DAY, **values: Any) -> str:
		doc = frappe.get_doc(
			{
				"doctype": "Bank Transaction",
				"date": day,
				"bank_account": self.fixtures.checking,
				"currency": "USD",
				"withdrawal": amount,
				"transaction_id": transaction_id,
				**values,
			}
		)
		doc.insert(ignore_permissions=True)
		doc.submit()
		return str(doc.name)

	def reconcile_against_journal(self, bank_transaction: str, cheque_no: str) -> None:
		"""Mark a Bank Transaction reconciled against a voucher carrying ``cheque_no``."""
		journal_entry = frappe.get_doc(
			{"doctype": "Journal Entry", "company": COMPANY, "posting_date": DAY, "cheque_no": cheque_no}
		)
		journal_entry.db_insert()
		frappe.get_doc(
			{
				"doctype": "Bank Transaction Payments",
				"parent": bank_transaction,
				"parenttype": "Bank Transaction",
				"parentfield": "payment_entries",
				"payment_document": "Journal Entry",
				"payment_entry": journal_entry.name,
				"allocated_amount": 1,
			}
		).db_insert()


class TestGatewayCutover(MigrateTestCase):
	def test_repoint_subscription_plans(self):
		frappe.get_doc(
			{
				"doctype": "Subscription Plan",
				"name": "_Test Mercury Plan",
				"plan_name": "_Test Mercury Plan",
				"item": "_Test Item",
				"currency": "USD",
				"payment_gateway": "GoCardless - USD",
			}
		).db_insert()
		dry = migrate.repoint_subscription_plans("GoCardless - USD", "Mercury - USD", dry_run=True)
		self.assertEqual(dry, {"would_repoint": ["_Test Mercury Plan"], "count": 1})
		migrate.repoint_subscription_plans("GoCardless - USD", "Mercury - USD", dry_run=False)
		self.assertEqual(
			frappe.db.get_value("Subscription Plan", "_Test Mercury Plan", "payment_gateway"), "Mercury - USD"
		)

	def test_set_default_gateway_account(self):
		for name, default in (("_Test Old PGA", 1), ("_Test Mercury PGA", 0)):
			frappe.get_doc(
				{
					"doctype": "Payment Gateway Account",
					"name": name,
					"payment_gateway": "Mercury",
					"company": COMPANY,
					"payment_account": self.fixtures.clearing_gl,
					"is_default": default,
				}
			).db_insert()
		dry = migrate.set_default_gateway_account("_Test Mercury PGA", dry_run=True)
		self.assertIn("_Test Old PGA", dry["current_defaults"])
		applied = migrate.set_default_gateway_account("_Test Mercury PGA", dry_run=False)
		self.assertIn("_Test Old PGA", applied["unset"])
		self.assertEqual(frappe.db.get_value("Payment Gateway Account", "_Test Old PGA", "is_default"), 0)
		self.assertEqual(frappe.db.get_value("Payment Gateway Account", "_Test Mercury PGA", "is_default"), 1)

	def test_snapshot_plaid_integration_ids(self):
		frappe.db.set_value("Bank Account", self.fixtures.savings, "integration_id", "plaid-123")
		snapshot = migrate.snapshot_plaid_integration_ids()
		self.assertEqual(snapshot[self.fixtures.savings], "plaid-123")


class TestPlaidMercuryMerge(MigrateTestCase):
	def setUp(self) -> None:
		super().setUp()
		self.reports: list[str] = []
		self.addCleanup(self.remove_reports)

	def remove_reports(self) -> None:
		# the test's rollback drops the File rows but leaves their CSVs in private/files
		for file_url in self.reports:
			path = frappe.get_site_path(file_url.lstrip("/"))
			if os.path.exists(path):
				os.remove(path)

	def merge(self, **kwargs: Any) -> frappe._dict:
		summary = migrate.merge_plaid_mercury_duplicates(**kwargs)
		self.reports.append(cast("str", summary.report))
		return summary

	def test_pairs_by_every_tier_and_reconciled_side_survives(self):
		# check number (Mercury zero-pads it, Plaid stored it bare)
		check_plaid = self.bank_transaction("plaid-check", 10, reference_number="6520")
		self.bank_transaction(uuid(1), 10)
		# voucher reference: the Plaid row was reconciled against a voucher carrying the Mercury id
		voucher_plaid = self.bank_transaction("plaid-voucher", 20, description="Payment")
		self.reconcile_against_journal(voucher_plaid, uuid(2))
		voucher_mercury = self.bank_transaction(uuid(2), 20, bank_party_name="Vendor")
		# unique party on both sides
		self.bank_transaction("plaid-party-a", 30, description="DEBIT; Merchant name: Acme")
		self.bank_transaction("plaid-party-b", 30, description="DEBIT; Merchant name: Zenith")
		self.bank_transaction(uuid(3), 30, bank_party_name="Acme")
		self.bank_transaction(uuid(4), 30, bank_party_name="Zenith")
		# raw bank text
		self.bank_transaction("plaid-desc-a", 40, description="ACH ONE")
		self.bank_transaction("plaid-desc-b", 40, description="ACH TWO")
		self.bank_transaction(uuid(5), 40, description="Mercury — ach one")
		self.bank_transaction(uuid(6), 40, description="Mercury — ach two")
		# same party twice: interchangeable
		self.bank_transaction("plaid-same-a", 50, description="Payroll")
		self.bank_transaction("plaid-same-b", 50, description="Payroll")
		self.bank_transaction(uuid(7), 50, bank_party_name="Payroll")
		self.bank_transaction(uuid(8), 50, bank_party_name="Payroll")
		# one each on the key alone
		self.bank_transaction("plaid-key", 60, description="Something")
		self.bank_transaction(uuid(9), 60, bank_party_name="Other")
		# both reconciled: a conflict for a human
		both_plaid = self.bank_transaction("plaid-both", 70)
		both_mercury = self.bank_transaction(uuid(10), 70)
		self.reconcile_against_journal(both_plaid, "x")
		self.reconcile_against_journal(both_mercury, "y")
		# a Plaid row with no Mercury twin
		self.bank_transaction("plaid-alone", 80)

		self.api.get(
			"transactions", {"transactions": [transaction_payload(id=uuid(1), checkNumber="006520")]}
		)
		dry = self.merge(dry_run=True)
		report = cast("File", frappe.get_last_doc("File", filters={"file_url": dry.report}))
		self.assertTrue(report.is_private)
		self.assertTrue(cast("str", report.file_name).startswith("plaid-mercury-merge-dry-run-"))
		content = report.get_content()
		self.assertTrue(
			(content.decode() if isinstance(content, bytes) else content).startswith("action,tier,")
		)
		self.assertEqual(
			dry.by_tier,
			{
				"check-number": 1,
				"voucher-ref": 1,
				"party": 2,
				"description": 2,
				"interchangeable": 2,
				"key-only": 1,
			},
		)
		self.assertEqual(dry.conflicts, 1)
		self.assertEqual(dry.unmatched_plaid, 1)
		self.assertTrue(frappe.db.exists("Bank Transaction", voucher_mercury))
		self.assertTrue(
			any(CHECKING_ID in call.request.url for call in self.api.calls("GET", "transactions"))
		)

		self.api.get(
			"transactions", {"transactions": [transaction_payload(id=uuid(1), checkNumber="006520")]}
		)
		applied = self.merge(dry_run=False)
		self.assertEqual(applied.pairs, 9)
		# the reconciled Plaid row survives and takes the Mercury identity
		self.assertFalse(frappe.db.exists("Bank Transaction", voucher_mercury))
		self.assertEqual(frappe.db.get_value("Bank Transaction", voucher_plaid, "transaction_id"), uuid(2))
		# otherwise the Mercury row survives
		self.assertFalse(frappe.db.exists("Bank Transaction", check_plaid))
		self.assertEqual(
			frappe.db.get_value("Bank Transaction", {"transaction_id": uuid(1)}, "reference_number"), "6520"
		)

	def test_references_and_redundant_ids(self):
		self.bank_transaction(uuid(11), 15, reference_number=uuid(11))
		self.bank_transaction(uuid(12), 16)
		self.api.get("transactions", {"transactions": [transaction_payload(id=uuid(12), checkNumber="77")]})
		summary = self.merge(dry_run=False)
		self.assertEqual(summary.cleared_redundant_refs, 1)
		self.assertEqual(summary.backported_check_numbers, 1)
		self.assertIsNone(
			frappe.db.get_value("Bank Transaction", {"transaction_id": uuid(11)}, "reference_number")
		)
		self.assertEqual(
			frappe.db.get_value("Bank Transaction", {"transaction_id": uuid(12)}, "reference_number"), "77"
		)

	def test_survivor_reference(self):
		row = frappe._dict
		self.assertEqual(
			migrate._survivor_reference(row(reference_number="12"), row(transaction_id="m"), {"m": "13"}),
			("13", "check number mismatch: Mercury API 13 vs Plaid 12"),
		)
		self.assertEqual(
			migrate._survivor_reference(
				row(reference_number="REF-9", description="x"), row(transaction_id="m"), {}
			),
			("REF-9", ""),
		)
		self.assertEqual(
			migrate._survivor_reference(
				row(reference_number="Desc", description="desc"), row(transaction_id="m"), {}
			),
			(None, ""),
		)

	def test_no_mercury_accounts(self):
		frappe.db.set_value("Bank Account", {"mercury_account_id": ("is", "set")}, "mercury_account_id", None)
		with self.assertRaisesRegex(frappe.ValidationError, "No Bank Accounts"):
			migrate.merge_plaid_mercury_duplicates(fetch_check_numbers=False)

	def test_find_duplicate_bank_transactions(self):
		self.bank_transaction("plaid-1", 99)
		self.bank_transaction(uuid(13), 99)
		rows = migrate.find_duplicate_bank_transactions(around=DAY)
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0].copies, 2)
