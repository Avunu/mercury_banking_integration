# Copyright (c) 2026, Avunu LLC and contributors
# For license information, please see license.txt

"""Reconciliation: payout Bank Transactions and internal transfers, Mercury API mocked."""

from __future__ import annotations

from typing import Any, cast

import frappe
from frappe.utils import today

from mercury_banking_integration.client.models import MercuryTransaction
from mercury_banking_integration.payouts import reconcile
from mercury_banking_integration.sync import transfers
from mercury_banking_integration.tests.utils import (
	ABBR,
	COMPANY,
	EMPLOYEE,
	PAYROLL_ENTRY,
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
		set_settings(auto_reconcile_transfers=0)  # book through the call under test, not on submit
		out_leg = make_bank_transaction(
			self.fixtures.checking,
			withdrawal=amount,
			transaction_id=uuid(out_id),
			transaction_type="internalTransfer",
			bank_party_name="Mercury Savings ••5202",
		)
		in_leg = make_bank_transaction(
			self.fixtures.savings,
			deposit=amount,
			transaction_id=uuid(in_id),
			transaction_type="internalTransfer",
			bank_party_name="Mercury Checking ••4101",
		)
		set_settings(auto_reconcile_transfers=1)
		return out_leg, in_leg

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

	def test_ambiguous_counter_side_alerts(self):
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
		self.assertEqual(transfers.reconcile_existing_transfers()["ambiguous"], [out_leg])

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
