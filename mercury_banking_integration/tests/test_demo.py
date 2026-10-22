# Copyright (c) 2026, Avunu LLC and contributors
# For license information, please see license.txt

"""The demo hook frappe-demo calls (`mercury_banking_integration.demo.setup`)."""

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING, cast

import frappe

from mercury_banking_integration import demo
from mercury_banking_integration.sync.accounts import get_bank_account_name
from mercury_banking_integration.tests.utils import COMPANY, MercuryTestCase

if TYPE_CHECKING:
	from mercury_banking_integration.mercury_integration.doctype.mercury_settings.mercury_settings import (
		MercurySettings,
	)

CTX = {"today": datetime.date(2026, 3, 13), "seed": 1, "company": COMPANY, "erpnext_demo": False}


def _bank_transaction(index: int) -> frappe._dict:
	row = frappe.db.get_value(
		"Bank Transaction",
		{"transaction_id": demo._transaction_id(index)},
		["name", "status", "date", "deposit", "withdrawal"],
		as_dict=True,
	)
	assert isinstance(row, dict)
	return frappe._dict(row)


class TestDemo(MercuryTestCase):
	def test_setup_syncs_and_books_without_reaching_mercury(self):
		demo.setup(CTX)

		self.assertEqual(self.api.calls(), [])  # every client call went to the offline stand-in
		checking = get_bank_account_name(demo.CHECKING_ID)
		self.assertTrue(checking)
		self.assertTrue(get_bank_account_name(demo.SAVINGS_ID))
		self.assertEqual(
			frappe.db.count("Bank Transaction", {"transaction_id": ("like", "7a1c9e44-%")}),
			len(demo.TRANSACTIONS),
		)

		rent = _bank_transaction(1)  # GL-coded: booked and reconciled
		self.assertEqual(rent.status, "Reconciled")
		self.assertEqual(str(rent.date), "2026-03-01")
		self.assertEqual(rent.withdrawal, 3200)
		self.assertEqual(_bank_transaction(2).status, "Unreconciled")  # no GL code
		self.assertEqual(_bank_transaction(6).status, "Reconciled")  # the internal transfer, both sides
		self.assertEqual(_bank_transaction(7).status, "Reconciled")

		self.assertEqual(
			frappe.db.get_single_value("Mercury Settings", "ar_destination_bank_account"), checking
		)
		self.assertTrue(frappe.db.exists("Payment Gateway Account", {"payment_gateway": "Mercury"}))
		settings = cast("MercurySettings", frappe.get_doc("Mercury Settings"))
		self.assertEqual(settings.get("sandbox_api_token"), "*" * len(demo.DEMO_TOKEN))  # shows as saved
		self.assertEqual(settings.get_api_token(), demo.DEMO_TOKEN)
		self.assertEqual(
			frappe.db.count("Salary Slip", {"payroll_entry": demo.PAYROLL_ENTRY, "docstatus": 1}),
			len(demo.EMPLOYEES),
		)
		statuses = frappe.get_all(
			"Employee",
			filters={"company_email": ("in", [employee[2] for employee in demo.EMPLOYEES])},
			pluck="mercury_recipient_status",
		)
		self.assertEqual(sorted(statuses), ["", "Active", "Active", "Invite Sent"])

	def test_setup_is_idempotent(self):
		demo.setup(CTX)
		demo.setup(CTX)
		self.assertEqual(
			frappe.db.count("Bank Transaction", {"transaction_id": ("like", "7a1c9e44-%")}),
			len(demo.TRANSACTIONS),
		)
		self.assertEqual(
			frappe.db.count("Journal Entry", {"cheque_no": demo._transaction_id(1), "docstatus": 1}), 1
		)
		self.assertEqual(
			frappe.db.count("Salary Slip", {"payroll_entry": demo.PAYROLL_ENTRY}), len(demo.EMPLOYEES)
		)

	def test_setup_needs_a_company(self):
		demo.setup({**CTX, "company": None})
		self.assertFalse(get_bank_account_name(demo.CHECKING_ID))
