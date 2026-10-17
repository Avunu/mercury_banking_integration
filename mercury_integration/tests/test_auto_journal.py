# Copyright (c) 2026, Avunu LLC and contributors
# For license information, please see license.txt

"""GL-coded transactions booked as Journal Entries, and the GL code export, Mercury API mocked."""

from __future__ import annotations

from typing import Any, cast

import frappe

from mercury_integration.client.models import MercuryTransaction
from mercury_integration.sync import auto_journal, gl_codes, transactions
from mercury_integration.tests.utils import (
	ABBR,
	COMPANY,
	SUPPLIER,
	MercuryTestCase,
	set_settings,
	transaction_payload,
	uuid,
)

EXPENSE = "Administrative Expenses"
RECEIPT_URL = "https://mercury-attachments.example/receipt.txt"


def coded(**overrides: Any) -> dict[str, Any]:
	return transaction_payload(glAllocations=[{"glCodeName": EXPENSE, "amount": -125.5}], **overrides)


class AutoJournalTestCase(MercuryTestCase):
	def setUp(self) -> None:
		super().setUp()
		self.configure(enable_auto_journal=1, require_attachment=0)
		self.notify = self._patch("mercury_integration.sync.auto_journal.notify_failure")

	def journal_for(self, transaction_id: str) -> str | None:
		return cast(
			"str | None", frappe.db.get_value("Journal Entry", {"cheque_no": transaction_id, "docstatus": 1})
		)

	def sync(self, payload: dict[str, Any]) -> str:
		"""Sync a transaction; the auto-journal path refetches it for its attachments."""
		self.api.replace("GET", f"transaction/{payload['id']}", payload)
		return cast("str", transactions.upsert_transaction(MercuryTransaction.model_validate(payload)))


class TestAutoJournal(AutoJournalTestCase):
	def test_coded_transaction_is_booked_reconciled_and_its_receipt_attached(self):
		self.api.mock.add("GET", RECEIPT_URL, body=b"receipt: 125.50")
		bank_transaction = self.sync(
			coded(attachments=[{"fileName": "receipt.txt", "url": RECEIPT_URL, "attachmentType": "receipt"}])
		)
		journal_entry = self.journal_for(uuid(1))
		self.assertTrue(journal_entry)
		rows = frappe.get_all(
			"Journal Entry Account",
			filters={"parent": journal_entry},
			fields=["account", "debit_in_account_currency", "credit_in_account_currency", "cost_center"],
			order_by="idx",
		)
		self.assertEqual(rows[0].account, f"{EXPENSE} - {ABBR}")
		self.assertEqual(rows[0].debit_in_account_currency, 125.5)
		self.assertEqual(rows[0].cost_center, f"Main - {ABBR}")
		self.assertEqual(rows[1].account, self.fixtures.checking_gl)
		self.assertEqual(rows[1].credit_in_account_currency, 125.5)
		self.assertEqual(frappe.db.get_value("Bank Transaction", bank_transaction, "status"), "Reconciled")
		for doctype, name in (("Bank Transaction", bank_transaction), ("Journal Entry", journal_entry)):
			self.assertTrue(
				frappe.db.exists(
					"File",
					{"attached_to_doctype": doctype, "attached_to_name": name, "file_name": "receipt.txt"},
				)
			)
		# importing again adds nothing
		self.assertEqual(auto_journal.import_attachments(uuid(1), bank_transaction, journal_entry), 0)

	def test_deposit_credits_the_coded_account(self):
		self.sync(
			transaction_payload(id=uuid(2), amount=40, glAllocations=[{"glCodeName": EXPENSE, "amount": 40}])
		)
		journal_entry = self.journal_for(uuid(2))
		credit = frappe.db.get_value(
			"Journal Entry Account",
			{"parent": journal_entry, "account": f"{EXPENSE} - {ABBR}"},
			"credit_in_account_currency",
		)
		self.assertEqual(credit, 40)

	def test_payable_code_carries_the_recipient_party(self):
		frappe.db.set_value(
			"Supplier", SUPPLIER, {"mercury_recipient_id": "rec-v", "mercury_recipient_status": "Active"}
		)
		self.sync(
			transaction_payload(
				id=uuid(3),
				counterpartyId="rec-v",
				glAllocations=[{"glCodeName": "Creditors", "amount": -125.5}],
			)
		)
		party = frappe.db.get_value(
			"Journal Entry Account",
			{"parent": self.journal_for(uuid(3)), "account": f"Creditors - {ABBR}"},
			["party_type", "party"],
		)
		self.assertEqual(party, ("Supplier", SUPPLIER))

	def test_gates(self):
		set_settings(require_attachment=1)
		self.sync(coded(id=uuid(4)))
		self.assertIsNone(self.journal_for(uuid(4)))

		set_settings(require_attachment=0, auto_journal_max_amount=100)
		self.sync(coded(id=uuid(5)))
		self.assertIsNone(self.journal_for(uuid(5)))

		set_settings(auto_journal_max_amount=0)
		self.sync(coded(id=uuid(6), kind="internalTransfer"))
		self.assertIsNone(self.journal_for(uuid(6)))

		settings = auto_journal.get_settings()
		pending = MercuryTransaction.model_validate(coded(id=uuid(7), status="pending"))
		self.assertEqual(auto_journal.plan_journal("BT-X", pending, settings), ([], None))
		posted = MercuryTransaction.model_validate(coded(id=uuid(7)))
		self.assertEqual(auto_journal.plan_journal("no-such-bt", posted, settings), ([], None))

		set_settings(enable_auto_journal=0)
		self.sync(coded(id=uuid(8)))
		self.assertIsNone(self.journal_for(uuid(8)))
		self.notify.assert_not_called()

	def test_unbookable_coding_alerts(self):
		self.sync(
			transaction_payload(id=uuid(9), glAllocations=[{"glCodeName": "Nonesuch", "amount": -125.5}])
		)
		self.notify.assert_called_once()
		self.assertIn("matches no account name", self.notify.call_args.args[1])

	def test_a_failed_booking_rolls_back_to_its_savepoint_and_alerts(self):
		self._patch("mercury_integration.sync.auto_journal._book", side_effect=RuntimeError("boom"))
		log_error = self._patch("frappe.log_error")
		bank_transaction = self.sync(coded(id=uuid(10)))
		self.assertTrue(frappe.db.exists("Bank Transaction", bank_transaction))
		log_error.assert_called_once()
		self.notify.assert_called_once()

	def test_cost_center_skips_group_centers(self):
		settings = auto_journal.get_settings()
		settings.default_cost_center = f"{COMPANY} - {ABBR}"  # the group root
		self.assertEqual(auto_journal._cost_center(COMPANY, settings), f"Main - {ABBR}")
		self._patch("mercury_integration.sync.auto_journal.get_default_cost_center", return_value=None)
		self.assertIsNone(auto_journal._cost_center(COMPANY, settings))

	def test_attachment_failures(self):
		log_error = self._patch("frappe.log_error")
		self.api.get(f"transaction/{uuid(11)}", {}, status=404)
		self.assertEqual(auto_journal.import_attachments(uuid(11), "BT-X"), 0)

		self.api.get(
			f"transaction/{uuid(12)}",
			transaction_payload(
				id=uuid(12),
				attachments=[
					{"fileName": "missing.pdf", "url": RECEIPT_URL},
					{"fileName": None, "url": RECEIPT_URL},
				],
			),
		)
		self.api.mock.add("GET", RECEIPT_URL, status=403)
		self.assertEqual(auto_journal.import_attachments(uuid(12), "BT-X"), 0)
		self.assertEqual(log_error.call_count, 2)

		self._patch_object(auto_journal, "MAX_ATTACHMENT_BYTES", 3)
		self.api.mock.add("GET", RECEIPT_URL, body=b"too large")
		self.assertIsNone(auto_journal._download(RECEIPT_URL))


class TestReevaluate(AutoJournalTestCase):
	def test_a_late_gl_code_is_booked_by_the_sweep(self):
		bank_transaction = self.sync(transaction_payload(id=uuid(20)))  # not coded yet
		self.assertIsNone(self.journal_for(uuid(20)))
		self.api.replace("GET", f"transaction/{uuid(20)}", coded(id=uuid(20)))

		dry = gl_codes.reevaluate_unreconciled(dry_run=True)
		self.assertEqual(dry["would_book"], [f"{bank_transaction} -> {EXPENSE} - {ABBR}"])

		self._patch("mercury_integration.sync.gl_codes.notify_failure")
		applied = gl_codes.reevaluate_unreconciled(dry_run=False)
		self.assertEqual(applied["booked"], [bank_transaction])

	def test_the_daily_sweep_mails_one_digest(self):
		blocked = self.sync(transaction_payload(id=uuid(21)))
		self.api.replace(
			"GET",
			f"transaction/{uuid(21)}",
			transaction_payload(id=uuid(21), glAllocations=[{"glCodeName": "Nonesuch", "amount": -125.5}]),
		)
		failed = self.sync(transaction_payload(id=uuid(22)))
		self.api.replace("GET", f"transaction/{uuid(22)}", {}, status=404)
		uncoded = self.sync(transaction_payload(id=uuid(23)))
		self.api.replace("GET", f"transaction/{uuid(23)}", transaction_payload(id=uuid(23)))
		digest = self._patch("mercury_integration.sync.gl_codes.notify_failure")

		gl_codes.run_reevaluate(days=10000)
		digest.assert_called_once()
		self.assertIn(blocked, digest.call_args.args[1])
		self.assertIn(failed, digest.call_args.args[1])
		self.assertNotIn(uncoded, digest.call_args.args[1])

	def test_sweep_quiet_when_nothing_is_wrong_and_guarded_when_off(self):
		digest = self._patch("mercury_integration.sync.gl_codes.notify_failure")
		gl_codes.run_reevaluate(days=1)
		digest.assert_not_called()

		set_settings(enable_auto_journal=0)
		with self.assertRaisesRegex(frappe.ValidationError, "Enable Mercury Settings"):
			gl_codes.reevaluate_unreconciled(dry_run=False)

		enqueue = self._patch("frappe.enqueue")
		gl_codes.enqueue_reevaluate(3)
		self.assertEqual(enqueue.call_args.kwargs["days"], 3)

	def test_journal_creation_failure_is_reported(self):
		self.sync(transaction_payload(id=uuid(24)))
		self.api.replace("GET", f"transaction/{uuid(24)}", coded(id=uuid(24)))
		self._patch("mercury_integration.sync.auto_journal.evaluate", return_value=None)
		result = gl_codes.reevaluate_unreconciled(dry_run=False)
		self.assertIn("journal entry creation failed", result["failed"][0])


class TestGlCodeExport(MercuryTestCase):
	def setUp(self) -> None:
		super().setUp()
		self.configure()

	def test_preview_and_export(self):
		frappe.get_doc(
			{
				"doctype": "Account",
				"account_name": "Meals, Entertainment",
				"parent_account": self.fixtures.expense_group,
				"company": COMPANY,
			}
		).insert(ignore_permissions=True)
		preview = gl_codes.preview_gl_codes()
		self.assertIn("Meals, Entertainment", preview["rejected"])
		self.assertGreater(preview["count"], 0)

		gl_codes.export_gl_codes()
		self.assertEqual(frappe.response["type"], "csv")
		codes = cast("str", frappe.response["result"]).split("\n")
		self.assertIn(EXPENSE, codes)
		self.assertNotIn("Meals, Entertainment", codes)
		self.assertEqual(len(codes), preview["count"])

	def test_no_company_no_codes(self):
		settings = gl_codes.get_settings()
		settings.company = None
		self.assertEqual(gl_codes.exportable_account_names(settings), ([], []))

	def test_export_is_role_gated(self):
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			gl_codes.preview_gl_codes()
		with self.assertRaises(frappe.PermissionError):
			gl_codes.export_gl_codes()
