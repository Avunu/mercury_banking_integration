# Copyright (c) 2026, Avunu LLC and contributors
# For license information, please see license.txt

"""Unit tests for the frappe-free Mercury client (HTTP answered in-process by ``responses``)."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any, cast
from unittest.mock import patch

import requests
import responses
from frappe.tests import UnitTestCase
from responses import matchers

from mercury_banking_integration.client import (
	MercuryAPIError,
	MercuryAuthError,
	MercuryClient,
	MercuryConflictError,
	MercuryConnectionError,
	MercuryNotFoundError,
	MercuryRateLimitError,
	MercuryServerError,
)
from mercury_banking_integration.client.errors import error_for_response
from mercury_banking_integration.client.http import PRODUCTION_BASE_URL, SANDBOX_BASE_URL, _retry_delay
from mercury_banking_integration.client.models import ArInvoice, MercuryTransaction, Recipient
from mercury_banking_integration.client.pagination import paginate

BASE = PRODUCTION_BASE_URL


def _transaction_payload(**overrides: Any) -> dict[str, Any]:
	payload = {
		"id": "11111111-1111-1111-1111-111111111111",
		"accountId": "22222222-2222-2222-2222-222222222222",
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


class ClientTestCase(UnitTestCase):
	def setUp(self) -> None:
		super().setUp()
		sleep = patch("mercury_banking_integration.client.http.time.sleep")
		sleep.start()
		self.addCleanup(sleep.stop)
		self.client = MercuryClient("secret-token:mercury_test_token")
		self.rsps = responses.RequestsMock()
		self.rsps.start()
		self.addCleanup(self.rsps.stop)
		self.addCleanup(self.rsps.reset)


class TestAuthAndEnvironment(ClientTestCase):
	def test_bearer_header_and_prod_base(self):
		self.rsps.get(
			BASE + "accounts",
			json={"accounts": []},
			match=[matchers.header_matcher({"Authorization": "Bearer secret-token:mercury_test_token"})],
		)
		self.assertEqual(list(self.client.list_accounts()), [])

	def test_sandbox_base_url(self):
		sandbox = MercuryClient("secret-token:sandbox", sandbox=True)
		self.rsps.get(SANDBOX_BASE_URL + "accounts", json={"accounts": []})
		self.assertEqual(list(sandbox.list_accounts()), [])


class TestRetryMatrix(ClientTestCase):
	def test_retries_then_succeeds(self):
		for status in (408, 429, 500, 502, 503):
			with self.subTest(status=status):
				self.rsps.reset()
				self.rsps.get(BASE + "transaction/x", status=status)
				self.rsps.get(BASE + "transaction/x", json=_transaction_payload())
				self.assertEqual(self.client.get_transaction("x").status, "sent")
				self.assertEqual(len(self.rsps.calls), 2)

	def test_rate_limit_exhausts_to_error(self):
		for _ in range(4):  # max_retries=3 → 4 attempts
			self.rsps.get(BASE + "transaction/x", status=429, headers={"Retry-After": "0"})
		with self.assertRaises(MercuryRateLimitError) as caught:
			self.client.get_transaction("x")
		self.assertEqual(caught.exception.retry_after, 0.0)

	def test_5xx_exhausts_to_server_error(self):
		for _ in range(4):
			self.rsps.get(BASE + "transaction/x", status=500)
		with self.assertRaises(MercuryServerError):
			self.client.get_transaction("x")

	def test_404_never_retried(self):
		self.rsps.get(BASE + "transaction/x", status=404)
		with self.assertRaises(MercuryNotFoundError):
			self.client.get_transaction("x")
		self.assertEqual(len(self.rsps.calls), 1)

	def test_x_should_retry_overrides(self):
		self.rsps.get(BASE + "transaction/x", status=500, headers={"x-should-retry": "false"})
		with self.assertRaises(MercuryServerError):
			self.client.get_transaction("x")
		self.assertEqual(len(self.rsps.calls), 1)

		self.rsps.reset()
		self.rsps.get(BASE + "transaction/x", status=400, headers={"x-should-retry": "true"})
		self.rsps.get(BASE + "transaction/x", json=_transaction_payload())
		self.assertTrue(self.client.get_transaction("x").id)

	def test_409_retried_on_reads(self):
		self.rsps.get(BASE + "transaction/x", status=409)
		self.rsps.get(BASE + "transaction/x", json=_transaction_payload())
		self.assertTrue(self.client.get_transaction("x").id)
		self.assertEqual(len(self.rsps.calls), 2)

	def test_409_not_retried_on_money_movement(self):
		self.rsps.post(BASE + "account/acc/transactions", status=409, json=_transaction_payload())
		with self.assertRaises(MercuryConflictError) as caught:
			self.client.create_payment(
				"acc", recipient_id="r", amount=Decimal("100.00"), idempotency_key="SLIP-0001:1"
			)
		self.assertEqual(len(self.rsps.calls), 1)
		# the replayed transaction is recoverable from the error body
		self.assertEqual(cast("dict", caught.exception.parsed)["id"], _transaction_payload()["id"])

	def test_connection_error_exhausts(self):
		for _ in range(4):
			self.rsps.get(BASE + "accounts", body=requests.ConnectionError("boom"))
		with self.assertRaises(MercuryConnectionError):
			list(self.client.list_accounts())

	def test_retry_delay_honours_the_headers(self):
		response = requests.Response()
		response.headers["Retry-After-Ms"] = "1500"
		self.assertEqual(_retry_delay(response, 0), 1.5)
		response.headers["Retry-After-Ms"] = "soon"
		response.headers["Retry-After"] = "2"
		self.assertEqual(_retry_delay(response, 0), 2.0)
		response.headers["Retry-After"] = "later"
		self.assertLess(_retry_delay(response, 0), 1.0)  # falls back to jittered backoff
		self.assertLessEqual(_retry_delay(None, 10), 30.0 * 1.25)


class TestErrors(UnitTestCase):
	def test_status_mapping(self):
		cases = {
			401: MercuryAuthError,
			403: MercuryAuthError,
			404: MercuryNotFoundError,
			400: MercuryAPIError,
		}
		for status, expected in cases.items():
			with self.subTest(status=status):
				error = error_for_response(status, "{}", method="GET", path="x")
				self.assertIs(type(error), expected)

	def test_unparseable_bodies(self):
		self.assertIsNone(error_for_response(400, "not json", method="GET", path="x").parsed)
		self.assertIsNone(error_for_response(400, "[1, 2]", method="GET", path="x").parsed)
		self.assertIn("HTTP 400", str(error_for_response(400, "bad", method="GET", path="x")))


class TestPagination(ClientTestCase):
	def test_cursor_follow_and_stop(self):
		page1 = [_transaction_payload(id=f"00000000-0000-0000-0000-00000000000{i}") for i in range(3)]
		self.rsps.get(
			BASE + "transactions",
			json={"transactions": page1},
			match=[matchers.query_param_matcher({"limit": "3", "order": "asc"}, strict_match=False)],
		)
		self.rsps.get(
			BASE + "transactions",
			json={"transactions": [_transaction_payload(id="99999999-9999-9999-9999-999999999999")]},
			match=[matchers.query_param_matcher({"start_after": page1[-1]["id"]}, strict_match=False)],
		)
		items = list(
			paginate(
				self.client.transport,
				"transactions",
				envelope_key="transactions",
				item_model=MercuryTransaction,
				params={"order": "asc"},
				limit=3,
			)
		)
		self.assertEqual(
			[t.id for t in items], [p["id"] for p in page1] + ["99999999-9999-9999-9999-999999999999"]
		)

	def test_short_page_stops_without_second_request(self):
		self.rsps.get(BASE + "accounts", json={"accounts": [{"id": "a1"}]})
		self.assertEqual(len(list(self.client.list_accounts())), 1)
		self.assertEqual(len(self.rsps.calls), 1)

	def test_start_after_and_empty_body(self):
		self.rsps.get(
			BASE + "events",
			body="",
			match=[matchers.query_param_matcher({"start_after": "evt-9"}, strict_match=False)],
		)
		self.assertEqual(list(self.client.list_events(start_after="evt-9")), [])


class TestModels(ClientTestCase):
	def test_camel_case_aliasing_and_decimals(self):
		self.rsps.get(BASE + "transaction/x", json=_transaction_payload())
		txn = self.client.get_transaction("x")
		self.assertEqual(txn.account_id, "22222222-2222-2222-2222-222222222222")
		self.assertEqual(txn.amount, Decimal("-125.5"))
		self.assertEqual(cast("Any", txn.posted_at).year, 2026)
		self.assertTrue(txn.is_posted)
		self.assertFalse(txn.is_pending or txn.is_dead)

	def test_unknown_enum_and_extra_fields_tolerated(self):
		payload = _transaction_payload(
			status="quantumSettled",  # future enum value
			kind="teleportationFee",
			brandNewField={"nested": True},
		)
		self.rsps.get(BASE + "transaction/x", json=payload)
		txn = self.client.get_transaction("x")
		self.assertEqual(txn.status, "quantumSettled")
		self.assertFalse(txn.is_posted or txn.is_dead)  # unknown → conservative fall-through

	def test_invoice_pay_page_url(self):
		invoice = ArInvoice.model_validate({"id": "i1", "slug": "abc123", "status": "Unpaid"})
		self.assertEqual(invoice.pay_page_url, "https://app.mercury.com/pay/abc123")
		self.assertIsNone(ArInvoice.model_validate({"id": "i2"}).pay_page_url)

	def test_recipient_ach_helpers(self):
		recipient = Recipient.model_validate(
			{
				"id": "r1",
				"electronicRoutingInfo": {
					"accountNumber": "000123456789",
					"routingNumber": "021000021",
					"electronicAccountType": "personalChecking",
				},
			}
		)
		self.assertEqual(recipient.ach_account_last4, "6789")
		self.assertEqual(recipient.ach_account_type, "personalChecking")
		self.assertIsNone(Recipient.model_validate({"id": "r2"}).ach_account_last4)


class TestUpdateTransactionSentinels(ClientTestCase):
	def test_omit_keeps_and_none_clears(self):
		self.rsps.patch(
			BASE + "transaction/x",
			json=_transaction_payload(),
			match=[matchers.json_params_matcher({"categoryId": None})],
		)
		self.client.update_transaction("x", category_id=None)

		self.rsps.reset()
		self.rsps.patch(
			BASE + "transaction/x",
			json=_transaction_payload(),
			match=[matchers.json_params_matcher({"note": "hi", "categoryId": "c1"})],
		)
		self.client.update_transaction("x", note="hi", category_id="c1")


class TestMoneyMovementBodies(ClientTestCase):
	def test_create_payment_body(self):
		self.rsps.post(
			BASE + "account/acc/transactions",
			json=_transaction_payload(),
			match=[
				matchers.json_params_matcher(
					{
						"recipientId": "r",
						"amount": 1234.56,
						"paymentMethod": "ach",
						"idempotencyKey": "SLIP-0001:1",
						"note": "Payroll",
						"externalMemo": "July",
					}
				)
			],
		)
		self.client.create_payment(
			"acc",
			recipient_id="r",
			amount=Decimal("1234.56"),
			idempotency_key="SLIP-0001:1",
			note="Payroll",
			external_memo="July",
		)

	def test_request_send_money_parses_approval(self):
		self.rsps.post(
			BASE + "account/acc/request-send-money",
			json={"requestId": "req-1", "status": "pendingApproval", "amount": 10, "reviews": []},
			match=[matchers.json_params_matcher({"note": "n", "externalMemo": "m"}, strict_match=False)],
		)
		approval = self.client.request_send_money(
			"acc", recipient_id="r", amount=10, idempotency_key="k1", note="n", external_memo="m"
		)
		self.assertEqual(approval.approval_id, "req-1")
		self.assertEqual(approval.status, "pendingApproval")

	def test_send_money_requests(self):
		self.rsps.get(BASE + "request-send-money/req-1", json={"id": "req-1", "status": "approved"})
		self.rsps.get(BASE + "request-send-money", json={"requests": [{"id": "req-1"}]})
		self.assertEqual(self.client.get_send_money_request("req-1").approval_id, "req-1")
		self.assertEqual(len(list(self.client.list_send_money_requests(status="approved"))), 1)


class TestEndpoints(ClientTestCase):
	"""One request per endpoint wrapper: the path, the verb and the parsed model."""

	def test_accounts_transactions_and_attachments(self):
		self.rsps.get(BASE + "account/a1", json={"id": "a1"})
		self.rsps.get(BASE + "transactions", json={"transactions": [_transaction_payload()]})
		self.rsps.post(BASE + "transaction/x/attachments", json={"ok": True})
		self.assertEqual(self.client.get_account("a1").id, "a1")
		self.assertEqual(
			len(list(self.client.list_transactions(account_id="a1", start="2026-01-01", status=["sent"]))), 1
		)
		self.assertEqual(
			self.client.upload_transaction_attachment("x", file_name="r.txt", content=b"r"), {"ok": True}
		)

	def test_categories(self):
		self.rsps.get(BASE + "categories", json={"categories": [{"id": "c1", "name": "Meals"}]})
		self.rsps.post(BASE + "categories", json={"id": "c2", "name": "Travel"})
		self.rsps.post(
			BASE + "categories/c2",
			json={"id": "c2", "name": "Trips"},
			match=[matchers.json_params_matcher({"name": "Trips", "visibleForOther": False})],
		)
		self.rsps.delete(BASE + "categories/c2")
		self.assertEqual([c.name for c in self.client.list_categories()], ["Meals"])
		self.assertEqual(self.client.create_category(name="Travel").id, "c2")
		self.assertEqual(
			self.client.update_category("c2", name="Trips", visible_for_other=False, unknown=1).name, "Trips"
		)
		self.client.delete_category("c2")

	def test_events_and_webhooks(self):
		self.rsps.get(BASE + "events/e1", json={"id": "e1", "resourceType": "transaction"})
		self.rsps.get(BASE + "webhooks", json={"webhooks": [{"id": "w1"}]})
		self.rsps.post(
			BASE + "webhooks",
			json={"id": "w2", "secret": "s"},
			match=[
				matchers.json_params_matcher({"url": "https://erp/x", "eventTypes": ["transaction.created"]})
			],
		)
		self.rsps.get(BASE + "webhooks/w2", json={"id": "w2", "status": "active"})
		self.rsps.post(BASE + "webhooks/w2", json={"id": "w2", "status": "disabled"})
		self.rsps.post(
			BASE + "webhooks/w2/verify", json={}, match=[matchers.json_params_matcher({"eventType": "test"})]
		)
		self.rsps.delete(BASE + "webhooks/w2")
		self.assertEqual(self.client.get_event("e1").resource_type, "transaction")
		self.assertEqual([w.id for w in self.client.list_webhooks()], ["w1"])
		self.assertEqual(
			self.client.create_webhook(url="https://erp/x", event_types=["transaction.created"]).secret, "s"
		)
		self.assertEqual(self.client.get_webhook("w2").status, "active")
		self.assertEqual(self.client.update_webhook("w2", status="disabled").status, "disabled")
		self.client.verify_webhook("w2", event_type="test")
		self.client.delete_webhook("w2")

	def test_recipients_and_invites(self):
		self.rsps.get(BASE + "recipients", json={"recipients": [{"id": "r1"}]})
		self.rsps.post(BASE + "recipient/r1", json={"id": "r1", "nickname": "R"})
		self.rsps.post(
			BASE + "recipients/invites",
			json={"id": "i1", "status": "created"},
			match=[
				matchers.json_params_matcher(
					{
						"contactEmail": "a@example.com",
						"name": "A",
						"recipientId": "r1",
						"paymentMethods": ["ach"],
						"requireTaxDocument": False,
						"sendEmail": True,
						"notes": "n",
					}
				)
			],
		)
		self.rsps.get(BASE + "recipients/invites", json={"invites": [{"id": "i1"}]})
		self.rsps.delete(BASE + "recipients/invites/i1")
		self.assertEqual([r.id for r in self.client.list_recipients()], ["r1"])
		self.assertEqual(self.client.update_recipient("r1", nickname="R").nickname, "R")
		self.client.create_recipient_invite(
			contact_email="a@example.com", name="A", recipient_id="r1", payment_methods=["ach"], notes="n"
		)
		self.assertEqual(len(list(self.client.list_recipient_invites(status="created"))), 1)
		self.client.delete_recipient_invite("i1")

	def test_accounts_receivable(self):
		self.rsps.get(BASE + "ar/customers", json={"customers": [{"id": "c1"}]})
		self.rsps.post(
			BASE + "ar/customers",
			json={"id": "c2"},
			match=[
				matchers.json_params_matcher(
					{"name": "C", "email": "c@example.com", "address": {"city": "X"}}
				)
			],
		)
		self.rsps.get(BASE + "ar/customers/c2", json={"id": "c2"})
		self.rsps.post(BASE + "ar/customers/c2", json={"id": "c2", "name": "D"})
		self.rsps.get(BASE + "ar/invoices", json={"invoices": [{"id": "i1"}]})
		self.rsps.post(
			BASE + "ar/invoices",
			json={"id": "i2"},
			match=[
				matchers.json_params_matcher(
					{"invoiceNumber": "N", "payerMemo": "p", "internalNote": "i", "poNumber": "po"},
					strict_match=False,
				)
			],
		)
		self.rsps.post(BASE + "ar/invoices/i2", json={"id": "i2", "poNumber": "po2"})
		self.rsps.post(BASE + "ar/invoices/i2/cancel", body="")
		self.rsps.get(BASE + "ar/attachments/at1", json={"id": "at1", "url": "https://x"})
		self.assertEqual([c.id for c in self.client.list_ar_customers()], ["c1"])
		self.assertEqual(
			self.client.create_ar_customer(name="C", email="c@example.com", address={"city": "X"}).id, "c2"
		)
		self.assertEqual(self.client.get_ar_customer("c2").id, "c2")
		self.assertEqual(self.client.update_ar_customer("c2", name="D").name, "D")
		self.assertEqual([i.id for i in self.client.list_ar_invoices()], ["i1"])
		self.client.create_ar_invoice(
			customer_id="c2",
			destination_account_id="a1",
			invoice_date="2026-07-01",
			due_date="2026-07-15",
			line_items=[],
			invoice_number="N",
			payer_memo="p",
			internal_note="i",
			po_number="po",
		)
		self.assertEqual(self.client.update_ar_invoice("i2", poNumber="po2").po_number, "po2")
		self.assertIsNone(self.client.cancel_ar_invoice("i2"))
		self.assertEqual(self.client.get_ar_attachment("at1")["url"], "https://x")


class TestRawResponses(ClientTestCase):
	def test_invoice_pdf_bytes(self):
		self.rsps.get(BASE + "ar/invoices/i1/pdf", body=b"%PDF-1.7", content_type="application/pdf")
		self.assertTrue(self.client.get_ar_invoice_pdf("i1").startswith(b"%PDF"))

	def test_error_body_preserved(self):
		self.rsps.get(BASE + "transaction/x", status=404, json={"errors": {"message": "nope"}})
		with self.assertRaises(MercuryNotFoundError) as caught:
			self.client.get_transaction("x")
		self.assertEqual(caught.exception.parsed, {"errors": {"message": "nope"}})
		self.assertEqual(json.loads(caught.exception.body)["errors"]["message"], "nope")
