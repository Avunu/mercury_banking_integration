# Copyright (c) 2026, Avunu LLC and contributors
# For license information, please see license.txt

"""Shared test fixtures: a mocked Mercury API and a USD company wired to it.

Every Mercury call goes through ``requests``, and :class:`MercuryAPI` answers it
in-process with ``responses``. A request with no registered answer fails as a
connection error, so no test can reach the real bank.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, ClassVar, NamedTuple, cast
from unittest.mock import MagicMock, patch

import frappe
import responses
from frappe.tests import IntegrationTestCase
from frappe.utils import today

from mercury_integration.client.http import PRODUCTION_BASE_URL

if TYPE_CHECKING:
	from frappe.utils.redis_wrapper import RedisWrapper

COMPANY = "_Test Company 1"  # USD, United States (erpnext's bootstrap test data)
ABBR = "_TC1"
CUSTOMER = "_Test Customer"
SUPPLIER = "_Test Supplier"
API_TOKEN = "secret-token:mercury_test_token"

CHECKING_ID = "aaaaaaaa-0000-4000-8000-000000000001"
SAVINGS_ID = "aaaaaaaa-0000-4000-8000-000000000002"
CHECKING_NUMBER = "000011114101"
SAVINGS_NUMBER = "000022225202"

SAVEPOINT = "mercury_test"


def uuid(n: int, prefix: str = "bbbbbbbb") -> str:
	"""A UUID-shaped id; Mercury transaction ids must match the UUID regex the app uses."""
	return f"{prefix}-0000-4000-8000-{n:012d}"


# ------------------------------------------------------------------ payloads


def account_payload(**overrides: Any) -> dict[str, Any]:
	payload = {
		"id": CHECKING_ID,
		"accountNumber": CHECKING_NUMBER,
		"routingNumber": "091311229",
		"name": "Mercury Checking ••4101",
		"nickname": "Mercury Checking",
		"status": "active",
		"type": "mercury",
		"kind": "checking",
	}
	payload.update(overrides)
	return payload


def transaction_payload(**overrides: Any) -> dict[str, Any]:
	payload = {
		"id": uuid(1),
		"accountId": CHECKING_ID,
		"amount": -125.5,
		"status": "sent",
		"kind": "externalTransfer",
		"createdAt": "2026-07-01T12:00:00Z",
		"postedAt": "2026-07-02T12:00:00Z",
		"counterpartyName": "Acme Corp",
		"attachments": [],
		"glAllocations": [],
		"relatedTransactions": [],
	}
	payload.update(overrides)
	return payload


def recipient_payload(**overrides: Any) -> dict[str, Any]:
	payload = {
		"id": "rec-1",
		"name": "Jane Payee",
		"emails": ["jane@example.com"],
		"status": "active",
		"defaultPaymentMethod": "ach",
		"electronicRoutingInfo": {
			"accountNumber": "000123456789",
			"routingNumber": "021000021",
			"electronicAccountType": "personalChecking",
		},
	}
	payload.update(overrides)
	return payload


def invoice_payload(**overrides: Any) -> dict[str, Any]:
	payload = {
		"id": "inv-1",
		"slug": "pay-inv-1",
		"invoiceNumber": "INV-1",
		"status": "Unpaid",
		"amount": 100,
		"customerId": "cust-1",
	}
	payload.update(overrides)
	return payload


def event_payload(**overrides: Any) -> dict[str, Any]:
	payload = {
		"id": "evt-1",
		"resourceType": "transaction",
		"resourceId": uuid(1),
		"operationType": "update",
		"occurredAt": "2026-07-02T12:00:00Z",
	}
	payload.update(overrides)
	return payload


# --------------------------------------------------------------- payroll and AP

EMPLOYEE = "_T-Employee-00001"
PAYROLL_ENTRY = "HR-PRUN-MERCURY-1"


def make_payroll(slips: list[tuple[str, float]], payroll_entry: str = PAYROLL_ENTRY) -> list[str]:
	"""A submitted Payroll Entry and its slips, written straight to the database.

	The app only reads these through the database, so the payroll calculation that
	hrms runs on insert is not needed here.
	"""
	if not frappe.db.exists("Payroll Entry", payroll_entry):
		frappe.get_doc(
			{
				"doctype": "Payroll Entry",
				"name": payroll_entry,
				"company": COMPANY,
				"posting_date": today(),
				"start_date": today(),
				"end_date": today(),
				"docstatus": 1,
			}
		).db_insert()
	names = []
	for index, (employee, net_pay) in enumerate(slips, start=1):
		name = f"Sal Slip/{payroll_entry}/{index:05d}"
		frappe.get_doc(
			{
				"doctype": "Salary Slip",
				"name": name,
				"employee": employee,
				"employee_name": frappe.db.get_value("Employee", employee, "employee_name"),
				"company": COMPANY,
				"payroll_entry": payroll_entry,
				"posting_date": today(),
				"start_date": "2026-07-01",
				"end_date": "2026-07-15",
				"net_pay": net_pay,
				"rounded_total": round(net_pay),
				"docstatus": 1,
			}
		).db_insert()
		names.append(name)
	return names


def make_supplier_payment(amount: float = 250.0, **values: Any) -> str:
	payment_entry = frappe.get_doc(
		{
			"doctype": "Payment Entry",
			"payment_type": "Pay",
			"company": COMPANY,
			"posting_date": today(),
			"party_type": "Supplier",
			"party": SUPPLIER,
			"paid_from": f"Mercury Checking - {ABBR}",
			"paid_to": f"Creditors - {ABBR}",
			"paid_from_account_currency": "USD",
			"paid_to_account_currency": "USD",
			"paid_amount": amount,
			"received_amount": amount,
			"source_exchange_rate": 1,
			"target_exchange_rate": 1,
			"reference_no": "VENDOR-REF",
			"reference_date": today(),
			"mode_of_payment": "Wire Transfer",
			**values,
		}
	)
	payment_entry.insert(ignore_permissions=True)
	payment_entry.submit()
	return str(payment_entry.name)


# --------------------------------------------------------------------- API mock


class MercuryAPI:
	"""Answers the Mercury client's HTTP requests in-process."""

	def __init__(self) -> None:
		self.mock = responses.RequestsMock(assert_all_requests_are_fired=False)

	def start(self) -> None:
		self.mock.start()

	def stop(self) -> None:
		self.mock.stop()
		self.mock.reset()

	def add(self, method: str, path: str, json: Any = None, status: int = 200, **kwargs: Any) -> None:
		"""Register an answer for ``method path``; repeated registrations answer in order."""
		self.mock.add(method, PRODUCTION_BASE_URL + path, json=json, status=status, **kwargs)

	def replace(self, method: str, path: str, json: Any = None, status: int = 200, **kwargs: Any) -> None:
		"""Drop every answer registered for ``method path`` and register this one."""
		self.mock.remove(method, PRODUCTION_BASE_URL + path)
		self.add(method, path, json, status, **kwargs)

	def get(self, path: str, json: Any = None, **kwargs: Any) -> None:
		self.add("GET", path, json, **kwargs)

	def post(self, path: str, json: Any = None, **kwargs: Any) -> None:
		self.add("POST", path, json, **kwargs)

	def calls(self, method: str | None = None, path: str | None = None) -> list[Any]:
		found = []
		for call in self.mock.calls:
			request = call.request
			url = str(request.url).split("?")[0]
			if method and request.method != method:
				continue
			if path and url != PRODUCTION_BASE_URL + path:
				continue
			found.append(call)
		return found

	def body(self, method: str, path: str, index: int = -1) -> dict[str, Any]:
		"""The JSON body of a recorded request."""
		return json.loads(self.calls(method, path)[index].request.body)


# --------------------------------------------------------------------- fixtures


def _gl_account(account_name: str, parent: str, account_type: str | None = None) -> str:
	name = f"{account_name} - {ABBR}"
	if not frappe.db.exists("Account", name):
		frappe.get_doc(
			{
				"doctype": "Account",
				"account_name": account_name,
				"parent_account": parent,
				"company": COMPANY,
				"account_type": account_type,
				"account_currency": "USD",
			}
		).insert(ignore_permissions=True)
	return name


def _bank_account(account_name: str, gl_account: str, mercury_id: str, number: str) -> str:
	from mercury_integration.sync.accounts import ensure_mercury_bank

	name = f"{account_name} - Mercury"
	if not frappe.db.exists("Bank Account", name):
		frappe.get_doc(
			{
				"doctype": "Bank Account",
				"account_name": account_name,
				"bank": ensure_mercury_bank(),
				"account": gl_account,
				"is_company_account": 1,
				"company": COMPANY,
				"bank_account_no": number,
				"mercury_account_id": mercury_id,
			}
		).insert(ignore_permissions=True)
	return name


class Fixtures(NamedTuple):
	"""What :func:`make_fixtures` sets up, by name."""

	checking: str  # Bank Account, mapped to CHECKING_ID
	savings: str  # Bank Account, mapped to SAVINGS_ID
	checking_gl: str
	savings_gl: str
	clearing_gl: str
	fees_gl: str
	expense_group: str


def make_fixtures() -> Fixtures:
	"""The company side: two Mercury bank accounts, an AR clearing and a card fee account."""
	bank_group = f"Bank Accounts - {ABBR}"
	checking_gl = _gl_account("Mercury Checking", bank_group, "Bank")
	savings_gl = _gl_account("Mercury Savings", bank_group, "Bank")
	clearing_gl = _gl_account("Mercury AR Clearing", bank_group, "Bank")
	expense_group = cast(
		"str",
		frappe.db.get_value(
			"Account",
			{"company": COMPANY, "root_type": "Expense", "is_group": 1, "account_name": "Indirect Expenses"},
		),
	)
	fees_gl = _gl_account("Mercury Card Fees", expense_group, None)
	return Fixtures(
		checking=_bank_account("Mercury Checking", checking_gl, CHECKING_ID, CHECKING_NUMBER),
		savings=_bank_account("Mercury Savings", savings_gl, SAVINGS_ID, SAVINGS_NUMBER),
		checking_gl=checking_gl,
		savings_gl=savings_gl,
		clearing_gl=clearing_gl,
		fees_gl=fees_gl,
		expense_group=expense_group,
	)


def configure_settings(api: MercuryAPI, **values: Any):
	"""Enable Mercury Settings against the mocked API (validation lists the accounts)."""
	api.get("accounts", {"accounts": []})
	settings = frappe.get_single("Mercury Settings")
	settings.update(
		{
			"enabled": 1,
			"use_sandbox": 0,
			"api_token": API_TOKEN,
			"company": COMPANY,
			"alert_recipients": "alerts@example.com",
			**values,
		}
	)
	settings.save(ignore_permissions=True)
	frappe.clear_document_cache("Mercury Settings", "Mercury Settings")
	return settings


def set_settings(**values: Any) -> None:
	"""Change Mercury Settings fields without running its validation (no API call)."""
	for field, value in values.items():
		frappe.db.set_single_value("Mercury Settings", field, value)
	frappe.clear_document_cache("Mercury Settings", "Mercury Settings")


class MercuryTestCase(IntegrationTestCase):
	"""Mercury API mocked, the app's commits suppressed, every test rolled back.

	The app commits (or rolls back) per row in its scheduler loops. Here
	``frappe.db.commit`` and a full ``frappe.db.rollback()`` are mocks the tests can
	assert on, savepoint rollbacks still run, and each test is rolled back to the
	savepoint it started from, so nothing a test does outlives it.
	"""

	fixtures: ClassVar[Fixtures]

	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		cls.fixtures = make_fixtures()

	def setUp(self) -> None:
		super().setUp()
		frappe.set_user("Administrator")
		self._clear_caches()
		frappe.db.savepoint(SAVEPOINT)

		real_rollback = frappe.db.rollback

		def rollback(*, save_point: str | None = None, **kwargs: Any) -> None:
			if save_point:
				real_rollback(save_point=save_point)

		self.commit = cast("MagicMock", self._patch("frappe.db.commit"))
		self.rollback = cast("MagicMock", self._patch_object(frappe.db, "rollback", side_effect=rollback))
		self._patch("mercury_integration.client.http.time.sleep")

		self.api = MercuryAPI()
		self.api.start()
		self.addCleanup(self.api.stop)

	def tearDown(self) -> None:
		frappe.set_user("Administrator")
		frappe.db.after_commit.reset()
		frappe.db.rollback(save_point=SAVEPOINT)
		self._clear_caches()
		super().tearDown()

	@staticmethod
	def _clear_caches() -> None:
		from mercury_integration.mercury_integration.doctype.mercury_settings.mercury_settings import (
			WEBHOOK_SECRET_CACHE_KEY,
		)

		frappe.clear_document_cache("Mercury Settings", "Mercury Settings")
		cast("RedisWrapper", frappe.cache).delete_value(WEBHOOK_SECRET_CACHE_KEY)

	def _patch(self, target: str, *args: Any, **kwargs: Any) -> Any:
		patcher = patch(target, *args, **kwargs)
		mocked = patcher.start()
		self.addCleanup(patcher.stop)
		return mocked

	def _patch_object(self, target: Any, attribute: str, *args: Any, **kwargs: Any) -> Any:
		patcher = patch.object(target, attribute, *args, **kwargs)
		mocked = patcher.start()
		self.addCleanup(patcher.stop)
		return mocked

	def configure(self, **values: Any):
		return configure_settings(self.api, **values)
