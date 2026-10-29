# Copyright (c) 2026, Avunu LLC and contributors
# For license information, please see license.txt

"""Bank sync, Mercury API mocked: accounts, transactions, events and the webhook."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import TYPE_CHECKING, Any, cast

import frappe
from werkzeug.test import EnvironBuilder

from mercury_banking_integration import webhooks
from mercury_banking_integration.client import MercuryServerError
from mercury_banking_integration.client.models import MercuryTransaction
from mercury_banking_integration.sync import accounts, events, transactions
from mercury_banking_integration.tests.utils import (
	ABBR,
	CHECKING_ID,
	SAVINGS_ID,
	MercuryTestCase,
	account_payload,
	event_payload,
	set_settings,
	transaction_payload,
	uuid,
)

if TYPE_CHECKING:
	from erpnext.accounts.doctype.bank_account.bank_account import BankAccount  # frappe-vulture:ignore
	from frappe.integrations.doctype.integration_request.integration_request import (  # frappe-vulture:ignore
		IntegrationRequest,
	)


def txn(**overrides: Any) -> MercuryTransaction:
	return MercuryTransaction.model_validate(transaction_payload(**overrides))


class TestAccountSync(MercuryTestCase):
	def setUp(self) -> None:
		super().setUp()
		self.configure()

	def test_sync_creates_adopts_and_disables(self):
		frappe.db.set_value("Bank Account", self.fixtures.savings, "mercury_account_id", "old-savings-id")
		self.api.get(
			"accounts",
			{
				"accounts": [
					account_payload(),  # already linked
					account_payload(
						id=SAVINGS_ID, accountNumber="22225202", nickname="Savings"
					),  # adopt by number
					account_payload(id="new-acc", accountNumber="000033336303", nickname="Mercury Treasury"),
					account_payload(id="old-savings-id", status="archived", nickname="Old"),
					account_payload(id="ext", type="external"),
				]
			},
		)
		result = accounts.sync_mercury_accounts()
		self.assertEqual(result["updated"], [self.fixtures.checking])
		self.assertEqual(result["adopted"], [self.fixtures.savings])
		self.assertEqual(result["created"], ["Mercury Treasury - Mercury"])
		self.assertEqual(
			frappe.db.get_value(
				"Bank Account", self.fixtures.savings, ["mercury_account_id", "bank_account_no"]
			),
			(SAVINGS_ID, "22225202"),
		)
		created = cast("BankAccount", frappe.get_doc("Bank Account", "Mercury Treasury - Mercury"))
		self.assertEqual(created.account, f"Mercury Treasury - {ABBR}")
		self.assertEqual(frappe.db.get_value("Account", created.account, "account_type"), "Bank")

	def test_archived_account_still_mapped_is_disabled(self):
		self.api.get("accounts", {"accounts": [account_payload(status="archived")]})
		self.assertEqual(accounts.sync_mercury_accounts()["disabled"], [self.fixtures.checking])

	def test_matching_cascade(self):
		candidates = [
			frappe._dict(name="exact", bank_account_no="1234-5678", bank="Other", account_name="x"),
			frappe._dict(name="tail", bank_account_no="9999", bank="Other", account_name="y"),
			frappe._dict(name="by-name", bank_account_no=None, bank="Mercury", account_name="Ops"),
		]
		match = accounts._match_existing_bank_account
		self.assertEqual(match(cast("Any", frappe._dict(account_number="12345678")), candidates), "exact")
		self.assertEqual(match(cast("Any", frappe._dict(account_number="00009999")), candidates), "tail")
		self.assertEqual(
			match(cast("Any", frappe._dict(account_number=None, nickname=" ops ", name=None)), candidates),
			"by-name",
		)
		self.assertIsNone(
			match(cast("Any", frappe._dict(account_number="555", nickname="none", name=None)), candidates)
		)

	def test_duplicate_nickname_gets_its_last_four(self):
		from mercury_banking_integration.client.models import MercuryAccount

		account = MercuryAccount.model_validate(
			account_payload(id="dup", accountNumber="000044447404", nickname="Mercury Checking")
		)
		name = accounts._create_bank_account(account, "_Test Company 1")
		self.assertEqual(name, "Mercury Checking ••7404 - Mercury")

	def test_a_bank_group_is_required(self):
		self._patch("frappe.db.get_value", return_value=None)
		with self.assertRaisesRegex(frappe.ValidationError, "Bank account group"):
			accounts._get_company_bank_gl_account("_Test Company 1")

	def test_role_gate(self):
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			accounts.sync_mercury_accounts()


class TestTransactionSync(MercuryTestCase):
	def setUp(self) -> None:
		super().setUp()
		self.configure(create_pending_transactions=1)
		self.alert = self._patch("mercury_banking_integration.sync.transactions.notify_failure")

	def bank_transaction(self, transaction_id: str) -> frappe._dict:
		return cast(
			"frappe._dict",
			frappe.db.get_value(
				"Bank Transaction",
				{"transaction_id": transaction_id, "docstatus": ("<", 2)},
				["name", "docstatus", "date", "deposit", "withdrawal", "reference_number", "description"],
				as_dict=True,
			),
		)

	def test_posted_transaction_becomes_a_submitted_bank_transaction(self):
		name = transactions.upsert_transaction(txn(checkNumber="006520", bankDescription="ACH OUT"))
		row = self.bank_transaction(uuid(1))
		self.assertEqual(row.name, name)
		self.assertEqual(row.docstatus, 1)
		self.assertEqual(row.withdrawal, 125.5)
		self.assertEqual(str(row.date), "2026-07-02")
		self.assertEqual(row.reference_number, "6520")
		self.assertEqual(row.description, "Acme Corp — ACH OUT")

		# idempotent
		self.assertEqual(transactions.upsert_transaction(txn(checkNumber="006520")), name)
		self.assertEqual(frappe.db.count("Bank Transaction", {"transaction_id": uuid(1)}), 1)

	def test_pending_draft_is_updated_then_submitted_when_it_posts(self):
		pending = txn(status="pending", postedAt=None, amount=50)
		name = transactions.upsert_transaction(pending)
		self.assertEqual(self.bank_transaction(uuid(1)).docstatus, 0)
		self.assertEqual(self.bank_transaction(uuid(1)).deposit, 50)

		transactions.upsert_transaction(txn(status="pending", postedAt=None, amount=55))
		self.assertEqual(self.bank_transaction(uuid(1)).deposit, 55)

		self.assertEqual(transactions.upsert_transaction(txn(amount=55)), name)
		self.assertEqual(self.bank_transaction(uuid(1)).docstatus, 1)

	def test_pending_is_skipped_unless_enabled_and_a_dead_draft_is_deleted(self):
		set_settings(create_pending_transactions=0)
		self.assertIsNone(transactions.upsert_transaction(txn(status="pending", postedAt=None)))
		set_settings(create_pending_transactions=1)
		transactions.upsert_transaction(txn(status="pending", postedAt=None))
		transactions.upsert_transaction(txn(status="cancelled"))
		self.assertFalse(frappe.db.exists("Bank Transaction", {"transaction_id": uuid(1)}))
		# dead and never seen: nothing is created
		self.assertIsNone(transactions.upsert_transaction(txn(id=uuid(2), status="failed")))

	def test_submitted_mutations(self):
		name = cast("str", transactions.upsert_transaction(txn()))
		# date correction
		transactions.upsert_transaction(txn(postedAt="2026-07-03T09:00:00Z"))
		self.assertEqual(str(self.bank_transaction(uuid(1)).date), "2026-07-03")
		# amount change: cancelled and re-created
		replacement = transactions.upsert_transaction(txn(amount=-130, postedAt="2026-07-03T09:00:00Z"))
		self.assertNotEqual(replacement, name)
		self.assertEqual(frappe.db.get_value("Bank Transaction", name, "docstatus"), 2)
		# reversed: cancelled
		transactions.upsert_transaction(txn(amount=-130, status="reversed"))
		self.assertEqual(frappe.db.get_value("Bank Transaction", replacement, "docstatus"), 2)

	def test_reconciled_transactions_are_never_rewritten(self):
		name = cast("str", transactions.upsert_transaction(txn()))
		self._patch("mercury_banking_integration.sync.transactions._is_reconciled", return_value=True)
		transactions.upsert_transaction(txn(amount=-99))
		transactions.upsert_transaction(txn(status="reversed"))
		self.assertEqual(self.alert.call_count, 2)
		self.assertEqual(frappe.db.get_value("Bank Transaction", name, "docstatus"), 1)

	def test_the_locked_section_is_its_own_transaction(self):
		"""A job waiting on the lock must see the holder's Bank Transaction, booking included.

		Mercury's create and update events for one transaction are handled in parallel;
		without a commit on both sides of the lock each inserted its own copy.
		"""
		steps: list[str] = []
		self.commit.side_effect = lambda: steps.append("commit")
		self._patch(
			"mercury_banking_integration.sync.auto_journal.evaluate",
			side_effect=lambda *_: steps.append("book"),
		)
		transactions.upsert_transaction(txn())
		self.assertEqual(steps, ["commit", "book", "commit"])

	def test_unmapped_account_is_ignored(self):
		self.assertIsNone(transactions.upsert_transaction(txn(accountId="unknown")))

	def test_windowed_sync_and_scheduling(self):
		self.api.get(
			"transactions",
			{
				"transactions": [
					transaction_payload(id=uuid(3)),
					transaction_payload(id=uuid(4), postedAt=None),
				]
			},
		)
		self.assertEqual(transactions.sync_account_transactions(self.fixtures.checking), 2)
		self.assertEqual(
			str(frappe.db.get_value("Bank Account", self.fixtures.checking, "last_integration_date")),
			"2026-07-02",
		)
		request = self.api.calls("GET", "transactions")[-1].request
		self.assertIn(f"accountId={CHECKING_ID}", request.url)

		# next run starts SYNC_OVERLAP_DAYS before the last one
		self.api.get("transactions", {"transactions": []})
		transactions.sync_account_transactions(self.fixtures.checking)
		self.assertIn("start=2026-06-29", self.api.calls("GET", "transactions")[-1].request.url)

		self.api.get("transactions", {"transactions": []})
		transactions.sync_account_transactions(self.fixtures.savings, from_date="2026-01-01")
		self.assertIn("start=2026-01-01", self.api.calls("GET", "transactions")[-1].request.url)

		set_settings(sync_start_date="2026-03-01")
		frappe.db.set_value("Bank Account", self.fixtures.savings, "last_integration_date", None)
		self.api.get("transactions", {"transactions": []})
		transactions.sync_account_transactions(self.fixtures.savings)
		self.assertIn("start=2026-03-01", self.api.calls("GET", "transactions")[-1].request.url)

		self.assertEqual(transactions.sync_account_transactions("no-such-account"), 0)

		enqueue = self._patch("frappe.enqueue")
		transactions.sync_all_accounts()
		transactions.enqueue_backfill(days=10)
		self.assertEqual(enqueue.call_count, 4)
		self.assertTrue(enqueue.call_args.kwargs["from_date"])


class TestEvents(MercuryTestCase):
	def setUp(self) -> None:
		super().setUp()
		self.configure()
		self.enqueue = self._patch("frappe.enqueue")

	def test_ingest_records_each_event_once(self):
		name = events.ingest_event(
			event_payload(), source="Webhook", url="https://erp/x", request_headers={"a": "b"}
		)
		self.assertTrue(name)
		self.assertIsNone(events.ingest_event(event_payload(), source="Poll"))
		self.assertIsNone(events.ingest_event({"resourceType": "transaction"}, source="Poll"))
		request = cast("IntegrationRequest", frappe.get_doc("Integration Request", cast("str", name)))
		self.assertEqual(request.is_remote_request, 1)
		self.assertEqual(request.request_description, "Webhook")
		self.assertTrue(self.enqueue.call_args.kwargs["enqueue_after_commit"])

	def test_processing_a_transaction_event_syncs_the_transaction(self):
		name = cast("str", events.ingest_event(event_payload(), source="Poll"))
		self.api.get(f"transaction/{uuid(1)}", transaction_payload())
		events.process_event(name)
		request = cast("IntegrationRequest", frappe.get_doc("Integration Request", name))
		self.assertEqual(request.status, "Completed")
		self.assertEqual(request.reference_doctype, "Bank Transaction")
		self.assertTrue(frappe.db.exists("Bank Transaction", {"transaction_id": uuid(1)}))
		# done: a second run is a no-op unless forced
		events.process_event(name)
		self.assertEqual(len(self.api.calls("GET", f"transaction/{uuid(1)}")), 1)

	def test_unknown_upstream_transaction_and_other_resources_complete(self):
		name = cast("str", events.ingest_event(event_payload(), source="Poll"))
		self.api.get(f"transaction/{uuid(1)}", {"errors": {}}, status=404)
		events.process_event(name)
		self.assertIn("not found upstream", frappe.db.get_value("Integration Request", name, "output"))

		other = cast(
			"str",
			events.ingest_event(event_payload(id="evt-2", resourceType="checkingAccount"), source="Poll"),
		)
		events.process_event(other)
		self.assertEqual(frappe.db.get_value("Integration Request", other, "status"), "Completed")

	def test_failure_is_recorded_and_retry_reruns_it(self):
		name = cast("str", events.ingest_event(event_payload(), source="Poll"))
		self.api.get(f"transaction/{uuid(1)}", {"errors": {}}, status=500)
		self._patch("frappe.log_error")
		with self.assertRaises(MercuryServerError):
			events.process_event(name)
		self.assertEqual(frappe.db.get_value("Integration Request", name, "status"), "Failed")

		self.api.get(f"transaction/{uuid(1)}", transaction_payload())
		events.retry_event(name)
		self.assertEqual(frappe.db.get_value("Integration Request", name, "status"), "Completed")

		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			events.retry_event(name)

	def test_events_from_the_guest_webhook_run_as_administrator(self):
		name = cast(
			"str", events.ingest_event(event_payload(resourceType="savingsAccount"), source="Webhook")
		)
		frappe.set_user("Guest")
		events.process_event(name)
		self.assertEqual(frappe.session.user, "Administrator")

	def test_poll_advances_the_cursor(self):
		self.api.get("events", {"events": [event_payload(id="evt-a"), event_payload(id="evt-b")]})
		events.poll_events()
		self.assertEqual(frappe.db.get_single_value("Mercury Settings", "events_cursor"), "evt-b")
		self.assertTrue(frappe.db.get_single_value("Mercury Settings", "events_last_polled_at"))
		self.assertEqual(
			frappe.db.count("Integration Request", {"integration_request_service": "Mercury"}), 2
		)

		self.api.get("events", {"events": []})
		events.poll_events()
		self.assertIn("start_after=evt-b", self.api.calls("GET", "events")[-1].request.url)

	def test_a_backlog_keeps_queueing_polls_while_each_one_is_running(self):
		"""Run frappe's real enqueue (dedupe check included) with only redis faked."""
		from unittest.mock import MagicMock

		from frappe.utils import background_jobs
		from rq.job import JobStatus

		def enqueue(method: str, **kwargs: Any) -> Any:
			if method.endswith("events.poll_events"):
				return background_jobs.enqueue(method, **kwargs)
			return None  # process_event jobs are covered elsewhere

		self.enqueue.side_effect = enqueue
		queue = MagicMock(count=0)
		self._patch_object(background_jobs, "get_queue", return_value=queue)
		running: dict[str, Any] = {}
		self._patch_object(
			background_jobs,
			"get_job",
			side_effect=lambda job_id: running.get(background_jobs.create_job_id(job_id)),
		)
		after_commit: list[Any] = []

		def defer(callback: Any) -> None:
			if callback.__qualname__ == "enqueue.<locals>.enqueue_call":
				after_commit.append(callback)  # other after-commit hooks (caches) are not ours

		self._patch_object(frappe.db, "after_commit", MagicMock(add=defer))
		self._patch_object(events, "EVENTS_PER_POLL", 2)

		def run_poll(ids: range) -> str:
			"""Poll once, commit, and mark the queued continuation as the running job."""
			after_commit.clear()
			self.api.get("events", {"events": [event_payload(id=f"evt-{n}") for n in ids]})
			events.poll_events()
			self.assertEqual(len(after_commit), 1)  # queued only once the batch commits
			after_commit[0]()
			job_id = queue.enqueue_call.call_args.kwargs["job_id"]
			running.clear()
			running[job_id] = MagicMock(id=job_id, get_status=MagicMock(return_value=JobStatus.STARTED))
			return job_id

		first = run_poll(range(3))  # the cron run: a full batch
		self.assertEqual(frappe.db.get_single_value("Mercury Settings", "events_cursor"), "evt-1")
		second = run_poll(range(2, 5))  # the continuation, still STARTED while it queues the next
		self.assertEqual(frappe.db.get_single_value("Mercury Settings", "events_cursor"), "evt-3")
		self.assertNotEqual(first, second)
		self.assertEqual(queue.enqueue_call.call_count, 2)

		# the same cursor queued twice is still deduplicated
		running[second].get_status.return_value = JobStatus.QUEUED
		frappe.db.set_single_value("Mercury Settings", "events_cursor", "evt-1")
		after_commit.clear()
		self.api.get("events", {"events": [event_payload(id=f"evt-{n}") for n in range(2, 5)]})
		events.poll_events()
		self.assertEqual(after_commit, [])

		# a short batch ends the chain
		self.api.get("events", {"events": [event_payload(id="evt-9")]})
		events.poll_events()
		self.assertEqual(after_commit, [])

	def test_poll_is_off_while_disabled_and_skips_while_running(self):
		from frappe.utils.file_lock import LockTimeoutError

		set_settings(enabled=0)
		events.poll_events()
		set_settings(enabled=1)
		self._patch("mercury_banking_integration.sync.events.filelock", side_effect=LockTimeoutError("busy"))
		events.poll_events()
		self.assertEqual(self.api.calls("GET", "events"), [])

	def test_register_webhook_replaces_the_endpoint_and_stores_the_secret(self):
		set_settings(webhook_endpoint_id="wh-old")
		self.api.add("DELETE", "webhooks/wh-old", {})
		self.api.post("webhooks", {"id": "wh-1", "status": "active", "secret": "whsec_1"})
		self.api.post("webhooks/wh-1/verify", {}, status=500)
		notify = self._patch("mercury_banking_integration.sync.events.notify_failure")
		self.assertEqual(events.register_webhook_endpoint(), "wh-1")
		self.assertEqual(frappe.db.get_single_value("Mercury Settings", "webhook_endpoint_id"), "wh-1")
		self.assertEqual(webhooks.get_webhook_secret(), "whsec_1")
		self.assertIn(
			"/api/method/mercury_banking_integration.webhooks.webhook",
			self.api.body("POST", "webhooks")["url"],
		)
		notify.assert_called_once()  # verification failed

		self.api.post("webhooks", {"id": "wh-2", "status": "active"})
		with self.assertRaisesRegex(frappe.ValidationError, "signing secret"):
			events.register_webhook_endpoint()

	def test_webhook_health_revives_a_disabled_endpoint(self):
		notify = self._patch("mercury_banking_integration.sync.events.notify_failure")
		events.check_webhook_health()  # no endpoint: nothing to check
		set_settings(webhook_endpoint_id="wh-1", webhook_status="active")
		self.api.get("webhooks/wh-1", {"id": "wh-1", "status": "disabled"})
		self.api.post("webhooks/wh-1", {"id": "wh-1", "status": "active"})
		events.check_webhook_health()
		self.assertEqual(self.api.body("POST", "webhooks/wh-1"), {"status": "active"})
		notify.assert_called_once()

		self.api.get("webhooks/wh-1", {"id": "wh-1", "status": "disabled"})
		self.api.post("webhooks/wh-1", {}, status=400)
		events.check_webhook_health()
		self.assertEqual(frappe.db.get_single_value("Mercury Settings", "webhook_status"), "disabled")

		log_error = self._patch("frappe.log_error")
		self.api.get("webhooks/wh-1", {}, status=404)
		events.check_webhook_health()
		log_error.assert_called_once()

	def test_replay_reprocesses_recent_events(self):
		self._patch("frappe.publish_realtime")
		seen = cast(
			"str",
			events.ingest_event(event_payload(id="evt-seen", resourceType="checkingAccount"), source="Poll"),
		)
		frappe.db.set_value("Integration Request", seen, "status", "Completed")
		self.api.get(
			"events",
			{
				"events": [
					event_payload(
						id="evt-new", resourceType="checkingAccount", occurredAt="2999-01-01T00:00:00Z"
					),
					event_payload(
						id="evt-seen", resourceType="checkingAccount", occurredAt="2999-01-01T00:00:00Z"
					),
					event_payload(id="evt-old", occurredAt="2000-01-01T00:00:00Z"),
				]
			},
		)
		events.run_replay(hours=24)
		self.assertTrue(frappe.db.exists("Integration Request", {"request_id": "evt-new"}))
		self.assertFalse(frappe.db.exists("Integration Request", {"request_id": "evt-old"}))

		events.enqueue_replay(hours=2)
		self.assertEqual(self.enqueue.call_args.kwargs["hours"], 2)


def _signed_request(body: bytes, secret: str | None = "whsec_test", header: str | None = None):
	timestamp = int(time.time())
	if header is None and secret:
		digest = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
		header = f"t={timestamp},v1={digest}"
	headers = {"Content-Type": "application/json"}
	if header:
		headers[webhooks.SIGNATURE_HEADER] = header
	return EnvironBuilder(
		method="POST",
		path="/api/method/mercury_banking_integration.webhooks.webhook",
		data=body,
		headers=list(headers.items()),
	).get_request()


class TestWebhookEndpoint(MercuryTestCase):
	def setUp(self) -> None:
		super().setUp()
		self.configure()
		from frappe.utils.password import set_encrypted_password

		set_encrypted_password("Mercury Settings", "Mercury Settings", "whsec_test", "webhook_secret")
		self.ingest = self._patch("mercury_banking_integration.sync.events.ingest_event")
		self.addCleanup(self.drop_request)

	@staticmethod
	def drop_request() -> None:
		if hasattr(frappe.local, "request"):
			del frappe.local.request

	def deliver(self, body: bytes, **kwargs: Any) -> str:
		frappe.local.request = _signed_request(body, **kwargs)
		return webhooks.webhook()

	def test_a_signed_delivery_is_ingested(self):
		body = json.dumps(event_payload()).encode()
		self.assertEqual(self.deliver(body), "ok")
		self.assertEqual(self.ingest.call_args.args[0]["id"], "evt-1")
		self.assertEqual(self.ingest.call_args.kwargs["source"], "Webhook")

	def test_a_bad_signature_is_refused(self):
		with self.assertRaises(frappe.AuthenticationError):
			self.deliver(b'{"id": "evt-1"}', secret="whsec_wrong")
		with self.assertRaises(frappe.AuthenticationError):
			self.deliver(b'{"id": "evt-1"}', secret=None)
		self.ingest.assert_not_called()

	def test_invalid_json_and_missing_ids(self):
		with self.assertRaisesRegex(frappe.ValidationError, "Invalid JSON"):
			self.deliver(b"not json")
		log_error = self._patch("frappe.log_error")
		self.assertEqual(self.deliver(b'{"resourceType": "transaction"}'), "ok")
		log_error.assert_called_once()
		self.ingest.assert_not_called()
