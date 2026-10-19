# Copyright (c) 2026, Avunu LLC and contributors
# For license information, please see license.txt

"""Mercury Settings: token validation, the desk buttons, the client factory and the scheduler gates."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import frappe

from mercury_banking_integration.sync.client_factory import get_client, get_settings
from mercury_banking_integration.tests.utils import API_TOKEN, MercuryTestCase, set_settings

if TYPE_CHECKING:
	from mercury_banking_integration.mercury_integration.doctype.mercury_settings.mercury_settings import (
		MercurySettings,
	)


class TestMercurySettings(MercuryTestCase):
	def test_enabling_validates_the_token_against_the_api(self):
		self.configure()
		calls = self.api.calls("GET", "accounts")
		self.assertEqual(len(calls), 1)
		self.assertEqual(calls[0].request.headers["Authorization"], f"Bearer {API_TOKEN}")

	def test_a_rejected_token_blocks_the_save(self):
		self.api.get("accounts", {"errors": {"message": "bad token"}}, status=401)
		settings = frappe.get_single("Mercury Settings")
		settings.update({"enabled": 1, "api_token": "secret-token:wrong", "company": "_Test Company 1"})
		with self.assertRaisesRegex(frappe.ValidationError, "token validation failed"):
			settings.save(ignore_permissions=True)

	def test_sandbox_uses_the_sandbox_token_and_host(self):
		self.configure()
		set_settings(use_sandbox=1)
		settings = get_settings()
		settings.sandbox_api_token = "secret-token:sandbox"
		client = get_client(settings=settings)
		self.assertIn("api-sandbox.mercury.com", client.transport.base_url)
		self.assertEqual(settings.get_api_token(), "secret-token:sandbox")

	def test_a_missing_token_is_reported(self):
		set_settings(use_sandbox=1)
		with self.assertRaisesRegex(frappe.ValidationError, "Sandbox API Token"):
			get_settings().get_api_token()

	def test_saved_token_is_read_back_from_the_password_store(self):
		self.configure()
		settings = cast("MercurySettings", frappe.get_doc("Mercury Settings"))
		self.assertIn("*", settings.api_token or "")
		self.assertEqual(settings.get_api_token(), API_TOKEN)

	def test_require_enabled_guards_background_paths(self):
		with self.assertRaisesRegex(frappe.ValidationError, "disabled"):
			get_client(require_enabled=True)

	def test_buttons_need_write_permission(self):
		self.configure()
		frappe.set_user("Guest")
		settings = cast("MercurySettings", frappe.get_doc("Mercury Settings"))
		with self.assertRaises(frappe.PermissionError):
			settings.sync_accounts_now()

	def test_sync_accounts_now_runs_the_account_sync(self):
		self.configure()
		self.api.get("accounts", {"accounts": []})
		settings = cast("MercurySettings", frappe.get_doc("Mercury Settings"))
		self.assertEqual(
			settings.sync_accounts_now(), {"created": [], "adopted": [], "updated": [], "disabled": []}
		)

	def test_backfill_and_replay_buttons_enqueue_jobs(self):
		self.configure()
		enqueue = self._patch("frappe.enqueue")
		settings = cast("MercurySettings", frappe.get_doc("Mercury Settings"))
		settings.backfill_transactions(days="30")
		settings.replay_events(hours="6")
		methods = [call.args[0] for call in enqueue.call_args_list]
		self.assertIn("mercury_banking_integration.sync.transactions.sync_account_transactions", methods)
		self.assertIn("mercury_banking_integration.sync.events.run_replay", methods)
		replay = next(c for c in enqueue.call_args_list if c.args[0].endswith("run_replay"))
		self.assertEqual(replay.kwargs["hours"], 6)

	def test_register_webhook_button(self):
		self.configure()
		self.api.post("webhooks", {"id": "wh-1", "status": "active", "secret": "whsec_1"})
		self.api.post("webhooks/wh-1/verify", {})
		settings = cast("MercurySettings", frappe.get_doc("Mercury Settings"))
		self.assertEqual(settings.register_webhook(), "wh-1")

	def test_enabling_the_ar_gateway_registers_the_payment_gateway(self):
		self.configure(enable_ar_gateway=1, ar_clearing_account=self.fixtures.clearing_gl)
		self.assertTrue(frappe.db.exists("Payment Gateway", "Mercury"))
		account = frappe.db.get_value(
			"Payment Gateway Account", {"payment_gateway": "Mercury"}, "payment_account"
		)
		self.assertEqual(account, self.fixtures.clearing_gl)
		# the company had no default gateway account; registering must not make Mercury it
		company = frappe.db.get_value("Account", self.fixtures.clearing_gl, "company")
		self.assertFalse(frappe.db.exists("Payment Gateway Account", {"company": company, "is_default": 1}))

		# a new clearing account moves the existing gateway account instead of adding one
		set_settings(ar_clearing_account=self.fixtures.savings_gl)
		from mercury_banking_integration.ar.gateway import register_mercury_gateway

		register_mercury_gateway(get_settings())
		self.assertEqual(
			frappe.get_all(
				"Payment Gateway Account", {"payment_gateway": "Mercury"}, pluck="payment_account"
			),
			[self.fixtures.savings_gl],
		)


class TestSchedulerGates(MercuryTestCase):
	"""tasks.py: each job does nothing until its feature is switched on."""

	def test_sync_all_accounts(self):
		from mercury_banking_integration import tasks

		run = self._patch("mercury_banking_integration.sync.transactions.sync_all_accounts")
		tasks.sync_all_accounts()
		run.assert_not_called()

		self.configure(automatic_sync=1)
		tasks.sync_all_accounts()
		run.assert_called_once()

	def test_backfill_auto_journals(self):
		from mercury_banking_integration import tasks

		run = self._patch("mercury_banking_integration.sync.gl_codes.enqueue_reevaluate")
		self.configure(enable_auto_journal=1, auto_journal_backfill_days=0)
		tasks.backfill_auto_journals()
		run.assert_not_called()

		set_settings(auto_journal_backfill_days=7)
		tasks.backfill_auto_journals()
		run.assert_called_once_with(7)

	def test_poll_events_and_webhook_health_delegate(self):
		from mercury_banking_integration import tasks

		poll = self._patch("mercury_banking_integration.sync.events.poll_events")
		health = self._patch("mercury_banking_integration.sync.events.check_webhook_health")
		tasks.poll_events()
		tasks.check_webhook_health()
		poll.assert_called_once()
		health.assert_called_once()


class TestAlerts(MercuryTestCase):
	def test_alerts_go_to_every_configured_address(self):
		from mercury_banking_integration.utils import alerts

		set_settings(alert_recipients="ops@example.com,\nfinance@example.com , ")
		self.assertEqual(alerts.get_alert_recipients(), ["ops@example.com", "finance@example.com"])
		sendmail = self._patch("frappe.sendmail")
		alerts.notify_failure("Payout failed", "details", reference_doctype="Salary Slip", reference_name="X")
		self.assertEqual(sendmail.call_args.kwargs["subject"], "[Mercury] Payout failed")
		self.assertEqual(sendmail.call_args.kwargs["recipients"], ["ops@example.com", "finance@example.com"])

	def test_alerts_never_raise(self):
		from mercury_banking_integration.utils import alerts

		set_settings(alert_recipients=None)
		sendmail = self._patch("frappe.sendmail", side_effect=RuntimeError("smtp down"))
		alerts.notify_failure("x", "y")
		sendmail.assert_not_called()

		set_settings(alert_recipients="ops@example.com")
		log_error = self._patch("frappe.log_error")
		alerts.notify_failure("x", "y")
		log_error.assert_called_once()
