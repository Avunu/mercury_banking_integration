# Copyright (c) 2026, Avunu LLC and contributors
# For license information, please see license.txt

"""Demo data for frappe-demo and the screenshots in docs/screenshots/.

The demo runs the app's own sync against an offline stand-in for the Mercury
API: Sync Accounts creates two Mercury Bank Accounts, and the transaction sync
imports three weeks of activity, which books the GL-coded transactions and the
internal transfer and reconciles them. Mercury Settings, four employees with
their Mercury payee state, and a submitted payroll run complete the picture.
Nothing here reaches Mercury, and every date comes from ``ctx["today"]``.
"""

from __future__ import annotations

import datetime
from collections.abc import Iterator
from typing import Any, cast
from unittest.mock import patch

import frappe
from frappe.utils import add_days
from frappe.utils.password import set_encrypted_password

from mercury_banking_integration.client.models import MercuryAccount, MercuryTransaction

CHECKING_ID = "5d2b7f0e-3c41-4a6e-9b1d-000000004101"
SAVINGS_ID = "5d2b7f0e-3c41-4a6e-9b1d-000000005202"
CHECKING_NUMBER = "202245184101"
SAVINGS_NUMBER = "202245185202"
ROUTING_NUMBER = "091311229"

ACCOUNTS = (
	{
		"id": CHECKING_ID,
		"accountNumber": CHECKING_NUMBER,
		"routingNumber": ROUTING_NUMBER,
		"name": "Mercury Checking ••4101",
		"nickname": "Mercury Checking",
		"status": "active",
		"type": "mercury",
		"kind": "checking",
	},
	{
		"id": SAVINGS_ID,
		"accountNumber": SAVINGS_NUMBER,
		"routingNumber": ROUTING_NUMBER,
		"name": "Mercury Savings ••5202",
		"nickname": "Mercury Savings",
		"status": "active",
		"type": "mercury",
		"kind": "savings",
	},
)

# (days before today, account, signed amount, kind, counterparty, GL codes, extra fields)
# A negative amount is money out, as in Mercury. GL codes are account names in the
# standard chart of accounts; a transaction without one stays unreconciled.
TRANSACTIONS: tuple[tuple[int, str, str, str, str, tuple[tuple[str, str], ...], dict[str, Any]], ...] = (
	(
		12,
		CHECKING_ID,
		"-3200.00",
		"externalTransfer",
		"Riverside Office Partners",
		(("Office Rent", "-3200.00"),),
		{},
	),
	(10, CHECKING_ID, "18450.00", "externalTransfer", "Northwind Traders", (), {}),
	(
		9,
		CHECKING_ID,
		"-486.20",
		"debitCardTransaction",
		"Staples",
		(("Print and Stationery", "-312.40"), ("Office Maintenance Expenses", "-173.80")),
		{},
	),
	(
		7,
		CHECKING_ID,
		"-642.30",
		"debitCardTransaction",
		"Delta Air Lines",
		(("Travel Expenses", "-642.30"),),
		{},
	),
	(
		5,
		CHECKING_ID,
		"-189.99",
		"externalTransfer",
		"Comcast Business",
		(("Telephone Expenses", "-189.99"),),
		{},
	),
	(4, CHECKING_ID, "-20000.00", "internalTransfer", "Mercury Savings ••5202", (), {}),
	(4, SAVINGS_ID, "20000.00", "internalTransfer", "Mercury Checking ••4101", (), {}),
	(3, CHECKING_ID, "-850.00", "checkPayment", "York Water Company", (), {"checkNumber": "006520"}),
	(
		2,
		CHECKING_ID,
		"-2150.00",
		"externalTransfer",
		"Brightline Marketing",
		(("Marketing Expenses", "-2150.00"),),
		{},
	),
	(1, CHECKING_ID, "7325.00", "externalTransfer", "Contoso Pharmacy", (), {}),
)

# (first name, last name, email, Mercury recipient status, net pay)
EMPLOYEES = (
	("Maria", "Lopez", "maria.lopez@example.com", "Active", 2846.15),
	("James", "Carter", "james.carter@example.com", "Active", 3120.50),
	("Priya", "Shah", "priya.shah@example.com", "Invite Sent", 2975.00),
	("Daniel", "Kim", "daniel.kim@example.com", "", 2410.85),
)
PAYROLL_ENTRY = "HR-PRUN-DEMO-00001"
DEMO_TOKEN = "secret-token:demo-sandbox"


def _transaction_id(index: int) -> str:
	return f"7a1c9e44-0b5d-4f2a-8c3e-{index:012d}"


def _transactions(today: datetime.date) -> list[MercuryTransaction]:
	transactions = []
	for index, (days, account_id, amount, kind, counterparty, codes, extra) in enumerate(TRANSACTIONS, 1):
		day = cast("datetime.date", add_days(today, -days))
		moment = datetime.datetime.combine(day, datetime.time(15, 0), tzinfo=datetime.UTC)
		payload: dict[str, Any] = {
			"id": _transaction_id(index),
			"accountId": account_id,
			"amount": amount,
			"status": "sent",
			"kind": kind,
			"createdAt": moment.isoformat(),
			"postedAt": moment.isoformat(),
			"counterpartyName": counterparty,
			"glAllocations": [{"glCodeName": code, "amount": value} for code, value in codes],
			**extra,
		}
		transactions.append(MercuryTransaction.model_validate(payload))
	return transactions


class OfflineMercury:
	"""Answers the few client calls the demo's sync makes, from the data above."""

	def __init__(self, today: datetime.date) -> None:
		self.transactions = _transactions(today)

	def list_accounts(self) -> Iterator[MercuryAccount]:
		return iter(MercuryAccount.model_validate(account) for account in ACCOUNTS)

	def list_transactions(
		self, *, account_id: str | None = None, **_filters: Any
	) -> Iterator[MercuryTransaction]:
		return iter(txn for txn in self.transactions if txn.account_id == account_id)

	def get_transaction(self, transaction_id: str) -> MercuryTransaction:
		return next(txn for txn in self.transactions if txn.id == transaction_id)


def _set_settings(values: dict[str, Any]) -> None:
	frappe.db.set_single_value("Mercury Settings", values)
	frappe.clear_document_cache("Mercury Settings", "Mercury Settings")


def _company_account(company: str, filters: dict[str, Any]) -> str | None:
	return cast("str | None", frappe.db.get_value("Account", {"company": company, **filters}))


def _ensure_account(company: str, account_name: str, parent: str, account_type: str | None = None) -> str:
	existing = _company_account(company, {"account_name": account_name, "is_group": 0})
	if existing:
		return existing
	account = frappe.get_doc(
		{
			"doctype": "Account",
			"account_name": account_name,
			"parent_account": parent,
			"company": company,
			"account_type": account_type,
		}
	).insert(ignore_permissions=True)
	return str(account.name)


def _ensure_gl_code_accounts(company: str) -> None:
	"""The demo's GL codes are standard account names; make any the chart lacks."""
	expenses = _company_account(company, {"account_name": "Indirect Expenses", "is_group": 1})
	if not expenses:
		return
	for *_, codes, _extra in TRANSACTIONS:
		for code, _amount in codes:
			_ensure_account(company, code, expenses)


def _configure(company: str, today: datetime.date) -> None:
	_set_settings(
		{
			"enabled": 1,
			"use_sandbox": 1,
			"company": company,
			"automatic_sync": 1,
			"sync_start_date": add_days(today, -90),
			"enable_auto_journal": 1,
			"require_attachment": 0,
			"auto_journal_backfill_days": 90,
			"auto_reconcile_transfers": 1,
			"default_cost_center": frappe.get_cached_value("Company", company, "cost_center"),
			"alert_recipients": "finance@example.com",
		}
	)
	set_encrypted_password("Mercury Settings", "Mercury Settings", DEMO_TOKEN, "sandbox_api_token")
	# Saving the form leaves asterisks in the field; without them it shows as empty.
	_set_settings({"sandbox_api_token": "*" * len(DEMO_TOKEN)})


def _configure_ar_and_payouts(company: str) -> None:
	from mercury_banking_integration.ar.gateway import register_mercury_gateway
	from mercury_banking_integration.sync.accounts import get_bank_account_name
	from mercury_banking_integration.sync.client_factory import get_settings

	checking = get_bank_account_name(CHECKING_ID)
	bank_group = _company_account(company, {"account_type": "Bank", "is_group": 1, "root_type": "Asset"})
	clearing = _ensure_account(company, "Mercury AR Clearing", str(bank_group), "Bank")
	_set_settings(
		{
			"enable_ar_gateway": 1,
			"ar_destination_bank_account": checking,
			"ar_clearing_account": clearing,
			"ar_email_sender": "ERP",
			"enable_payouts": 1,
			"payout_mode": "Request Approval",
			"payroll_funding_bank_account": checking,
			"vendor_funding_bank_account": checking,
		}
	)
	register_mercury_gateway(get_settings())


def _sync(today: datetime.date) -> None:
	from mercury_banking_integration.sync.accounts import get_bank_account_name, sync_mercury_accounts
	from mercury_banking_integration.sync.transactions import sync_account_transactions

	offline = OfflineMercury(today)
	with patch("mercury_banking_integration.sync.client_factory.MercuryClient", return_value=offline):
		sync_mercury_accounts()
		# checking first: the transfer's second side, on savings, books it
		for account_id in (CHECKING_ID, SAVINGS_ID):
			bank_account = get_bank_account_name(account_id)
			if bank_account:
				sync_account_transactions(bank_account)


def _employees(company: str, today: datetime.date) -> list[tuple[str, float]]:
	gender = cast("str | None", frappe.db.get_value("Gender", {}))
	employees = []
	for index, (first, last, email, status, net_pay) in enumerate(EMPLOYEES, 1):
		name = cast("str | None", frappe.db.get_value("Employee", {"company_email": email}))
		if not name:
			employee = frappe.get_doc(
				{
					"doctype": "Employee",
					"first_name": first,
					"last_name": last,
					"gender": gender,
					"date_of_birth": add_days(today, -365 * (28 + index * 3)),
					"date_of_joining": add_days(today, -365 * index),
					"company": company,
					"company_email": email,
					"prefered_contact_email": "Company Email",
					"status": "Active",
				}
			).insert(ignore_permissions=True)
			name = str(employee.name)
			values: dict[str, Any] = {"mercury_recipient_status": status}
			if status == "Active":
				values["mercury_recipient_id"] = f"0e8a2f6c-9d14-4b7a-a3c5-{index:012d}"
			elif status == "Invite Sent":
				values["mercury_invite_id"] = f"5b3d71e0-2a8f-4c69-b0d4-{index:012d}"
			frappe.db.set_value("Employee", name, values, update_modified=False)
		employees.append((name, net_pay))
	return employees


def _payroll(company: str, today: datetime.date, employees: list[tuple[str, float]]) -> None:
	"""A submitted payroll run, written straight to the database.

	_Pay via Mercury_ reads only the run's submitted salary slips, so hrms's payroll
	calculation (salary structures, components, attendance) is left out.
	"""
	if frappe.db.exists("Payroll Entry", PAYROLL_ENTRY):
		return
	end = cast("datetime.date", add_days(today, -1))
	start = cast("datetime.date", add_days(end, -13))
	frappe.get_doc(
		{
			"doctype": "Payroll Entry",
			"name": PAYROLL_ENTRY,
			"company": company,
			"posting_date": today,
			"payroll_frequency": "Bimonthly",
			"start_date": start,
			"end_date": end,
			"currency": frappe.get_cached_value("Company", company, "default_currency"),
			"exchange_rate": 1,
			"status": "Submitted",
			"docstatus": 1,
		}
	).db_insert()
	for index, (employee, net_pay) in enumerate(employees, 1):
		frappe.get_doc(
			{
				"doctype": "Salary Slip",
				"name": f"Sal Slip/{employee}/{index:05d}",
				"employee": employee,
				"employee_name": frappe.db.get_value("Employee", employee, "employee_name"),
				"company": company,
				"payroll_entry": PAYROLL_ENTRY,
				"posting_date": today,
				"start_date": start,
				"end_date": end,
				"currency": "USD",
				"net_pay": net_pay,
				"rounded_total": round(net_pay),
				"status": "Submitted",
				"docstatus": 1,
			}
		).db_insert()


def setup(ctx: dict[str, Any]) -> None:
	"""Build the demo once; a second run changes nothing."""
	company = ctx.get("company")
	if not company:
		return  # every part of the app needs erpnext's company
	today = cast("datetime.date", ctx["today"])

	_configure(company, today)
	_ensure_gl_code_accounts(company)
	_sync(today)
	_configure_ar_and_payouts(company)
	_payroll(company, today, _employees(company, today))
