# Copyright (c) 2026, Avunu LLC and contributors
# For license information, please see license.txt

"""Reconciliation: payouts, internal transfers and duplicate cleanup, Mercury API mocked."""

from __future__ import annotations

import json
from typing import Any, cast

import frappe
from erpnext.accounts.doctype.bank_reconciliation_tool.bank_reconciliation_tool import reconcile_vouchers
from frappe.utils import today

from mercury_banking_integration.client.models import MercuryTransaction
from mercury_banking_integration.patches.v1_3 import remove_duplicate_bank_transactions as cleanup_patch
from mercury_banking_integration.payouts import reconcile
from mercury_banking_integration.sync import duplicates, transfers
from mercury_banking_integration.tests.utils import (
	ABBR,
	COMPANY,
	EMPLOYEE,
	PAYROLL_ENTRY,
	Fixtures,
	MercuryTestCase,
	make_payroll,
	make_supplier_payment,
	set_settings,
	transaction_payload,
	uuid,
)


def make_bank_transaction(bank_account: str, submit: bool = True, **values: Any) -> str:
	doc = frappe.get_doc(
		{
			"doctype": "Bank Transaction",
			"date": today(),
			"bank_account": bank_account,
			"currency": "USD",
			**values,
		}
	)
	doc.insert(ignore_permissions=True)
	if submit:
		doc.submit()
	return str(doc.name)


def make_payroll_bank_journal(amount: float, bank_gl: str, payroll_entry: str = PAYROLL_ENTRY) -> str:
	"""The consolidated payroll bank entry hrms books from Payroll Entry.make_bank_entry."""
	journal_entry = frappe.get_doc(
		{
			"doctype": "Journal Entry",
			"voucher_type": "Bank Entry",
			"company": COMPANY,
			"posting_date": today(),
			"cheque_no": f"payroll {payroll_entry}",
			"cheque_date": today(),
			"accounts": [
				{
					"account": f"Administrative Expenses - {ABBR}",
					"debit_in_account_currency": amount,
					"cost_center": f"Main - {ABBR}",
					"reference_type": "Payroll Entry",
					"reference_name": payroll_entry,
				},
				{"account": bank_gl, "credit_in_account_currency": amount},
			],
		}
	)
	journal_entry.insert(ignore_permissions=True)
	journal_entry.submit()
	return str(journal_entry.name)


def make_transfer_legs(
	fixtures: Fixtures, amount: float = 500.0, out_id: int = 60, in_id: int = 61
) -> tuple[str, str]:
	"""Both legs of a checking → savings internal transfer, as the sync writes them."""
	set_settings(auto_reconcile_transfers=0)  # book through the call under test, not on submit
	out_leg = make_bank_transaction(
		fixtures.checking,
		withdrawal=amount,
		transaction_id=uuid(out_id),
		transaction_type="internalTransfer",
		bank_party_name="Mercury Savings ••5202",
	)
	in_leg = make_bank_transaction(
		fixtures.savings,
		deposit=amount,
		transaction_id=uuid(in_id),
		transaction_type="internalTransfer",
		bank_party_name="Mercury Checking ••4101",
	)
	set_settings(auto_reconcile_transfers=1)
	return out_leg, in_leg


class TestPayoutReconciliation(MercuryTestCase):
	def setUp(self) -> None:
		super().setUp()
		self.configure(
			enable_payouts=1,
			auto_reconcile_payouts=1,
			payroll_funding_bank_account=self.fixtures.checking,
			vendor_funding_bank_account=self.fixtures.checking,
		)

	def test_vendor_payout_reconciles_when_its_transaction_lands(self):
		payment_entry = make_supplier_payment(250.0)
		frappe.db.set_value(
			"Payment Entry",
			payment_entry,
			{"mercury_transaction_id": uuid(50), "mercury_payment_status": "Sent"},
		)
		bank_transaction = make_bank_transaction(
			self.fixtures.checking, withdrawal=250.0, transaction_id=uuid(50)
		)
		self.assertEqual(frappe.db.get_value("Bank Transaction", bank_transaction, "status"), "Reconciled")
		self.assertEqual(
			frappe.db.get_value("Payment Entry", payment_entry, "mercury_payment_status"), "Reconciled"
		)

	def test_payroll_payouts_reconcile_against_the_single_bank_journal(self):
		first, second = make_payroll([(EMPLOYEE, 100.0), ("_T-Employee-00002", 150.0)])
		journal_entry = make_payroll_bank_journal(250.0, self.fixtures.checking_gl)
		for slip, txn in ((first, uuid(51)), (second, uuid(52))):
			frappe.db.set_value(
				"Salary Slip", slip, {"mercury_transaction_id": txn, "mercury_payment_status": "Sent"}
			)

		make_bank_transaction(self.fixtures.checking, withdrawal=100.0, transaction_id=uuid(51))
		self.assertEqual(frappe.db.get_value("Salary Slip", first, "mercury_payment_status"), "Reconciled")
		self.assertFalse(frappe.db.get_value("Journal Entry", journal_entry, "clearance_date"))

		make_bank_transaction(self.fixtures.checking, withdrawal=150.0, transaction_id=uuid(52))
		self.assertEqual(frappe.db.get_value("Salary Slip", second, "mercury_payment_status"), "Reconciled")

	def test_the_daily_sweep_reconciles_payouts_whose_voucher_came_late(self):
		(slip,) = make_payroll([(EMPLOYEE, 100.0)])
		frappe.db.set_value(
			"Salary Slip", slip, {"mercury_transaction_id": uuid(53), "mercury_payment_status": "Sent"}
		)
		# the ACH lands before the payroll bank entry exists
		make_bank_transaction(self.fixtures.checking, withdrawal=100.0, transaction_id=uuid(53))
		self.assertEqual(frappe.db.get_value("Salary Slip", slip, "mercury_payment_status"), "Sent")

		make_payroll_bank_journal(100.0, self.fixtures.checking_gl)
		payment_entry = make_supplier_payment(80.0)
		frappe.db.set_value(
			"Payment Entry",
			payment_entry,
			{"mercury_transaction_id": uuid(54), "mercury_payment_status": "Posted"},
		)
		set_settings(auto_reconcile_payouts=0)
		make_bank_transaction(self.fixtures.checking, withdrawal=80.0, transaction_id=uuid(54))
		set_settings(auto_reconcile_payouts=1)

		reconcile.reconcile_pending_payouts()
		self.assertEqual(frappe.db.get_value("Salary Slip", slip, "mercury_payment_status"), "Reconciled")
		self.assertEqual(
			frappe.db.get_value("Payment Entry", payment_entry, "mercury_payment_status"), "Reconciled"
		)
		self.commit.assert_called()

	def test_failures_are_logged(self):
		log_error = self._patch("frappe.log_error")
		self._patch(
			"mercury_banking_integration.payouts.reconcile._reconcile_payment_entry",
			side_effect=RuntimeError("x"),
		)
		self._patch(
			"mercury_banking_integration.payouts.reconcile._reconcile_salary_slip",
			side_effect=RuntimeError("x"),
		)
		payment_entry = make_supplier_payment(80.0)
		frappe.db.set_value(
			"Payment Entry",
			payment_entry,
			{"mercury_transaction_id": uuid(55), "mercury_payment_status": "Sent"},
		)
		(slip,) = make_payroll([(EMPLOYEE, 70.0)])
		frappe.db.set_value(
			"Salary Slip", slip, {"mercury_transaction_id": uuid(56), "mercury_payment_status": "Sent"}
		)
		make_bank_transaction(self.fixtures.checking, withdrawal=80.0, transaction_id=uuid(55))
		make_bank_transaction(self.fixtures.checking, withdrawal=70.0, transaction_id=uuid(56))
		self.assertEqual(log_error.call_count, 2)

		reconcile.reconcile_pending_payouts()
		self.assertEqual(log_error.call_count, 4)
		self.rollback.assert_called()

	def test_skips(self):
		(slip,) = make_payroll([(EMPLOYEE, 70.0)])
		settings = frappe._dict(payroll_funding_bank_account=self.fixtures.checking)
		bank_transaction = frappe._dict(name="BT-X", withdrawal=70.0)
		# no bank journal for the payroll yet
		self.assertFalse(reconcile._reconcile_salary_slip(bank_transaction, slip, settings))
		self.assertFalse(reconcile._reconcile_salary_slip(bank_transaction, "no-such-slip", settings))
		# without a funding account any payroll bank journal will do
		journal_entry = make_payroll_bank_journal(70.0, self.fixtures.savings_gl)
		self.assertEqual(reconcile._payroll_bank_journal(PAYROLL_ENTRY, None), journal_entry)
		self.assertIsNone(reconcile._payroll_bank_journal(PAYROLL_ENTRY, self.fixtures.checking))

		reconcile.reconcile_payout_bank_transaction(frappe._dict(withdrawal=0, transaction_id=uuid(57)))
		set_settings(auto_reconcile_payouts=0)
		reconcile.reconcile_payout_bank_transaction(frappe._dict(withdrawal=5, transaction_id=uuid(57)))
		reconcile.reconcile_pending_payouts()


class TestInternalTransfers(MercuryTestCase):
	def setUp(self) -> None:
		super().setUp()
		self.configure(auto_reconcile_transfers=1)
		self.notify = self._patch("mercury_banking_integration.sync.transfers.notify_failure")

	def transfer_legs(self, amount: float = 500.0, out_id: int = 60, in_id: int = 61) -> tuple[str, str]:
		return make_transfer_legs(self.fixtures, amount, out_id, in_id)

	@staticmethod
	def txn(n: int, amount: float) -> MercuryTransaction:
		return MercuryTransaction.model_validate(
			transaction_payload(id=uuid(n), amount=amount, kind="internalTransfer")
		)

	def test_the_second_leg_books_one_journal_and_reconciles_both(self):
		out_leg, in_leg = self.transfer_legs()
		journal_entry = transfers.reconcile_internal_transfer(in_leg, self.txn(61, 500))
		self.assertTrue(journal_entry)
		self.assertEqual(frappe.db.get_value("Journal Entry", journal_entry, "cheque_no"), uuid(60))
		for leg in (out_leg, in_leg):
			self.assertEqual(frappe.db.get_value("Bank Transaction", leg, "status"), "Reconciled")

		# both legs now reconciled: nothing more to do
		self.assertIsNone(transfers.reconcile_internal_transfer(out_leg, self.txn(60, -500)))

	def test_backfill_dry_run_then_apply(self):
		self.transfer_legs()
		dry = transfers.reconcile_existing_transfers(dry_run=True)
		self.assertEqual(dry["pairs_would_book"], 1)
		self.assertEqual(dry["pairs_booked"], 0)

		applied = transfers.reconcile_existing_transfers(dry_run=False)
		self.assertEqual(applied["pairs_booked"], 1)
		again = transfers.reconcile_existing_transfers(dry_run=True)
		self.assertEqual(again["pairs_would_book"], 0)

		set_settings(auto_reconcile_transfers=0)
		with self.assertRaisesRegex(frappe.ValidationError, "Enable Mercury Settings"):
			transfers.reconcile_existing_transfers(dry_run=False)

	def test_unresolvable_legs_are_reported(self):
		pending = make_bank_transaction(
			self.fixtures.checking,
			withdrawal=10.0,
			transaction_id=uuid(62),
			transaction_type="internalTransfer",
			bank_party_name="Mercury Savings ••5202",
		)
		make_bank_transaction(
			self.fixtures.checking,
			withdrawal=11.0,
			transaction_id=uuid(63),
			transaction_type="internalTransfer",
			bank_party_name="Unknown account ••9999",
		)
		report = transfers.reconcile_existing_transfers(dry_run=True)
		self.assertEqual(report["pending_unsynced_counter"], 1)
		self.assertEqual(len(report["no_counter_account"]), 1)
		self.assertIsNone(transfers.reconcile_internal_transfer(pending, self.txn(62, -10)))

	def test_ambiguous_counter_side_alerts_once(self):
		self.transfer_legs(out_id=64, in_id=65)
		make_bank_transaction(
			self.fixtures.savings,
			deposit=500.0,
			transaction_id=uuid(66),
			transaction_type="internalTransfer",
			bank_party_name="Mercury Checking ••4101",
		)
		out_leg = cast("str", frappe.db.get_value("Bank Transaction", {"transaction_id": uuid(64)}))
		self.assertIsNone(transfers.reconcile_internal_transfer(out_leg, self.txn(64, -500)))
		self.notify.assert_called_once()
		self.assertIn("Ambiguous", self.notify.call_args.args[0])
		self.assertTrue(
			frappe.db.exists("Comment", {"reference_name": out_leg, "content": transfers.AMBIGUOUS_NOTE})
		)
		# every re-sync of the leg re-checks it, but the flag keeps the alert to one
		self.assertIsNone(transfers.reconcile_internal_transfer(out_leg, self.txn(64, -500)))
		self.notify.assert_called_once()
		self.assertEqual(transfers.reconcile_existing_transfers()["ambiguous"], [out_leg])

	def test_the_counter_side_names_this_account_back(self):
		out_leg, in_leg = self.transfer_legs(out_id=70, in_id=71)
		# the same amount into savings on the same day, but from a third account
		make_bank_transaction(
			self.fixtures.savings,
			deposit=500.0,
			transaction_id=uuid(72),
			transaction_type="internalTransfer",
			bank_party_name="Mercury Treasury ••6303",
		)
		self.assertTrue(transfers.reconcile_internal_transfer(out_leg, self.txn(70, -500)))
		self.assertEqual(frappe.db.get_value("Bank Transaction", in_leg, "status"), "Reconciled")
		self.notify.assert_not_called()

	def test_a_leg_booked_into_another_pair_is_not_a_candidate(self):
		_out_leg, in_leg = self.transfer_legs(out_id=73, in_id=74)
		transfers.reconcile_internal_transfer(in_leg, self.txn(74, 500))
		# an identical second transfer the same day pairs with the one open leg
		out_again, in_again = self.transfer_legs(out_id=75, in_id=76)
		journal_entry = transfers.reconcile_internal_transfer(out_again, self.txn(75, -500))
		self.assertEqual(frappe.db.get_value("Journal Entry", journal_entry, "cheque_no"), uuid(75))
		self.assertEqual(frappe.db.get_value("Bank Transaction", in_again, "status"), "Reconciled")
		self.notify.assert_not_called()

	def test_booking_failure_alerts(self):
		_out_leg, in_leg = self.transfer_legs(out_id=67, in_id=68)
		self._patch(
			"mercury_banking_integration.sync.transfers._book_transfer", side_effect=RuntimeError("x")
		)
		log_error = self._patch("frappe.log_error")
		self.assertIsNone(transfers.reconcile_internal_transfer(in_leg, self.txn(68, 500)))
		self.notify.assert_called_once()
		self.assertEqual(transfers.reconcile_existing_transfers(dry_run=False)["failed"], [_out_leg, in_leg])
		self.assertEqual(log_error.call_count, 3)

	def test_only_posted_internal_transfers_when_enabled(self):
		self.assertIsNone(
			transfers.reconcile_internal_transfer(
				"BT-X", MercuryTransaction.model_validate(transaction_payload(kind="externalTransfer"))
			)
		)
		set_settings(auto_reconcile_transfers=0)
		self.assertIsNone(transfers.reconcile_internal_transfer("BT-X", self.txn(69, 5)))
		set_settings(auto_reconcile_transfers=1)
		self.assertIsNone(transfers.reconcile_internal_transfer("no-such-bt", self.txn(69, 5)))

	def test_last4(self):
		self.assertEqual(transfers._last4("Mercury Checking ••4101"), "4101")
		self.assertIsNone(transfers._last4("abc"))
		self.assertIsNone(transfers._last4(None))


class TestDuplicateCleanup(MercuryTestCase):
	"""Unwinding the Bank Transaction copies the concurrent-upsert race left behind."""

	def setUp(self) -> None:
		super().setUp()
		self.configure(auto_reconcile_transfers=1)

	def own_journal(self, bank_transaction: str, transaction_id: str, amount: float) -> str:
		"""An auto-journal booking: a JE keyed on the Mercury id, created at reconciliation."""
		journal_entry = frappe.get_doc(
			{
				"doctype": "Journal Entry",
				"company": COMPANY,
				"posting_date": today(),
				"cheque_no": transaction_id,
				"cheque_date": today(),
				"accounts": [
					{
						"account": self.fixtures.fees_gl,
						"debit_in_account_currency": amount,
						"cost_center": f"Main - {ABBR}",
					},
					{"account": self.fixtures.checking_gl, "credit_in_account_currency": amount},
				],
			}
		)
		journal_entry.insert(ignore_permissions=True)
		journal_entry.submit()
		reconcile_vouchers(
			bank_transaction,
			json.dumps(
				[{"payment_doctype": "Journal Entry", "payment_name": journal_entry.name, "amount": amount}]
			),
			is_new_voucher=True,
		)
		return str(journal_entry.name)

	@staticmethod
	def allocate_again(bank_transaction: str, journal_entry: str, amount: float) -> None:
		"""Allocate a voucher as a racing job did, past core's over-allocation check."""
		doc = frappe.get_doc("Bank Transaction", bank_transaction)
		doc.append(
			"payment_entries",
			{
				"payment_document": "Journal Entry",
				"payment_entry": journal_entry,
				"allocated_amount": amount,
				"reconciliation_type": "Matched",
			},
		).db_insert()
		doc.db_set({"allocated_amount": amount, "unallocated_amount": 0, "status": "Reconciled"})

	def test_transfer_copies_are_cancelled_and_the_pair_then_books(self):
		first_out, first_in = make_transfer_legs(self.fixtures, out_id=80, in_id=81)
		copies = make_transfer_legs(self.fixtures, out_id=80, in_id=81)
		self.assertEqual(len(transfers.reconcile_existing_transfers()["ambiguous"]), 4)

		dry = duplicates.remove_duplicate_bank_transactions()
		self.assertEqual(dry["duplicated_transactions"], 2)
		self.assertEqual(len(dry["bank_transactions_cancelled"]), 2)
		self.assertEqual(frappe.db.get_value("Bank Transaction", copies[0], "docstatus"), 1)

		cleanup_patch.execute()  # the cleanup, then the transfer backfill
		for copy in copies:
			self.assertEqual(frappe.db.get_value("Bank Transaction", copy, "docstatus"), 2)
		for leg in (first_out, first_in):
			self.assertEqual(frappe.db.get_value("Bank Transaction", leg, "status"), "Reconciled")
		self.assertEqual(duplicates.remove_duplicate_bank_transactions()["duplicated_transactions"], 0)

	def test_a_copy_booked_on_its_own_journal_is_reversed(self):
		first = make_bank_transaction(self.fixtures.checking, withdrawal=21.2, transaction_id=uuid(82))
		copy = make_bank_transaction(self.fixtures.checking, withdrawal=21.2, transaction_id=uuid(82))
		kept, double = (self.own_journal(bt, uuid(82), 21.2) for bt in (first, copy))

		report = duplicates.remove_duplicate_bank_transactions(dry_run=False)
		self.assertEqual(report["journal_entries_cancelled"], [double])
		self.assertEqual(frappe.db.get_value("Journal Entry", double, "docstatus"), 2)
		self.assertEqual(frappe.db.get_value("Journal Entry", kept, "docstatus"), 1)
		self.assertEqual(frappe.db.get_value("Bank Transaction", copy, "docstatus"), 2)
		self.assertEqual(frappe.db.get_value("Bank Transaction", first, "status"), "Reconciled")

	def test_a_copy_sharing_the_payroll_entry_leaves_it_cleared(self):
		set_settings(
			enable_payouts=1, auto_reconcile_payouts=1, payroll_funding_bank_account=self.fixtures.checking
		)
		(slip,) = make_payroll([(EMPLOYEE, 100.0)])
		journal_entry = make_payroll_bank_journal(100.0, self.fixtures.checking_gl)
		frappe.db.set_value(
			"Salary Slip", slip, {"mercury_transaction_id": uuid(83), "mercury_payment_status": "Sent"}
		)
		first = make_bank_transaction(self.fixtures.checking, withdrawal=100.0, transaction_id=uuid(83))
		cleared = frappe.db.get_value("Journal Entry", journal_entry, "clearance_date")
		self.assertTrue(cleared)

		# the racing job: a copy allocated against the same entry, the slip status overwritten
		set_settings(auto_reconcile_payouts=0)
		copy = make_bank_transaction(self.fixtures.checking, withdrawal=100.0, transaction_id=uuid(83))
		self.allocate_again(copy, journal_entry, 100.0)
		frappe.db.set_value("Salary Slip", slip, "mercury_payment_status", "Posted")

		report = duplicates.remove_duplicate_bank_transactions(dry_run=False)
		self.assertEqual(report["payouts_reconciled"], [f"Salary Slip {slip}"])
		self.assertEqual(frappe.db.get_value("Bank Transaction", copy, "docstatus"), 2)
		self.assertEqual(frappe.db.get_value("Bank Transaction", first, "status"), "Reconciled")
		self.assertEqual(frappe.db.get_value("Journal Entry", journal_entry, "clearance_date"), cleared)
		self.assertEqual(frappe.db.get_value("Salary Slip", slip, "mercury_payment_status"), "Reconciled")

	def test_a_copy_matched_elsewhere_is_left_for_review(self):
		first = make_bank_transaction(self.fixtures.checking, withdrawal=80.0, transaction_id=uuid(84))
		copy = make_bank_transaction(self.fixtures.checking, withdrawal=80.0, transaction_id=uuid(84))
		for bank_transaction in (first, copy):
			reconcile._reconcile(bank_transaction, "Payment Entry", make_supplier_payment(80.0), 80.0)
		log_error = self._patch("frappe.log_error")

		cleanup_patch.execute()
		self.assertEqual(frappe.db.get_value("Bank Transaction", copy, "status"), "Reconciled")
		self.assertIn(copy, log_error.call_args.kwargs["message"])

	def test_a_failure_is_logged_and_skipped(self):
		for _copy in range(2):
			make_bank_transaction(self.fixtures.checking, withdrawal=5.0, transaction_id=uuid(85))
		self._patch(
			"mercury_banking_integration.sync.duplicates._unwind_group", side_effect=RuntimeError("x")
		)
		log_error = self._patch("frappe.log_error")
		report = duplicates.remove_duplicate_bank_transactions(dry_run=False)
		self.assertEqual(report["failed"], [uuid(85)])
		log_error.assert_called_once()
