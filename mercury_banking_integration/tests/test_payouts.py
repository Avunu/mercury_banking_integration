# Copyright (c) 2026, Avunu LLC and contributors
# For license information, please see license.txt

"""Payroll ACH and vendor payouts, Mercury API mocked.

Recipients onboarded → payroll preview → per-slip sends (direct and approval
modes, idempotency replays, Mercury's duplicate guard) → status progression from
the approval poll and transaction events.
"""

from __future__ import annotations

from typing import cast

import frappe
from frappe.utils import add_to_date, now_datetime, today

from mercury_banking_integration.payouts import payroll, recipients, send, vendor
from mercury_banking_integration.tests.utils import (
	CHECKING_ID,
	EMPLOYEE,
	PAYROLL_ENTRY,
	SUPPLIER,
	MercuryTestCase,
	make_payroll,
	make_supplier_payment,
	recipient_payload,
	set_settings,
	transaction_payload,
	uuid,
)

EMPLOYEE_EMAIL = "employee1@example.com"
SUPPLIER_EMAIL = "vendor@example.com"


class PayoutTestCase(MercuryTestCase):
	def setUp(self) -> None:
		super().setUp()
		self.configure(
			enable_payouts=1,
			payout_mode="Direct Send",
			payroll_funding_bank_account=self.fixtures.checking,
			vendor_funding_bank_account=self.fixtures.checking,
		)
		frappe.db.set_value(
			"Employee",
			EMPLOYEE,
			{
				"prefered_email": EMPLOYEE_EMAIL,
				"mercury_recipient_id": None,
				"mercury_recipient_status": None,
				"mercury_invite_id": None,
			},
		)
		frappe.db.set_value(
			"Supplier",
			SUPPLIER,
			{
				"email_id": SUPPLIER_EMAIL,
				"mercury_recipient_id": None,
				"mercury_recipient_status": None,
				"mercury_invite_id": None,
			},
		)
		self.notify = self._patch("mercury_banking_integration.payouts.send.notify_failure")

	def link_recipient(self, party_type: str = "Employee", party: str = EMPLOYEE, recipient: str = "rec-1"):
		frappe.db.set_value(
			party_type,
			party,
			{"mercury_recipient_id": recipient, "mercury_recipient_status": "Active"},
		)


class TestRecipients(PayoutTestCase):
	def test_invite_then_daily_sync_activates_the_recipient(self):
		self.api.post(
			"recipients/invites",
			{"id": "inv-1", "status": "created", "onboardingUrl": "https://mercury.example/invite"},
		)
		self.assertEqual(
			recipients.send_invite("Employee", EMPLOYEE), {"invite_id": "inv-1", "status": "created"}
		)
		body = self.api.body("POST", "recipients/invites")
		self.assertEqual(body["contactEmail"], EMPLOYEE_EMAIL)
		self.assertEqual(frappe.db.get_value("Employee", EMPLOYEE, "mercury_recipient_status"), "Invite Sent")

		self.api.get(
			"recipients/invites/inv-1", {"id": "inv-1", "status": "completed", "recipientId": "rec-1"}
		)
		self.api.get("recipient/rec-1", recipient_payload())
		recipients.sync_recipients()
		self.assertEqual(
			frappe.db.get_value("Employee", EMPLOYEE, ["mercury_recipient_id", "mercury_recipient_status"]),
			("rec-1", "Active"),
		)
		self.assertEqual(recipients.get_recipient_id("Employee", EMPLOYEE), "rec-1")
		self.assertEqual(recipients.get_party_for_recipient("rec-1"), ("Employee", EMPLOYEE))

	def test_invite_for_an_existing_recipient_carries_its_id(self):
		frappe.db.set_value("Supplier", SUPPLIER, "mercury_recipient_id", "rec-9")
		self.api.post("recipients/invites", {"id": "inv-2", "status": "created", "recipientId": "rec-9"})
		recipients.send_invite("Supplier", SUPPLIER)
		self.assertEqual(self.api.body("POST", "recipients/invites")["recipientId"], "rec-9")

	def test_invite_needs_an_email_and_a_known_party_type(self):
		frappe.db.set_value("Supplier", SUPPLIER, "email_id", None)
		with self.assertRaisesRegex(frappe.ValidationError, "no email address"):
			recipients.send_invite("Supplier", SUPPLIER)
		with self.assertRaisesRegex(frappe.ValidationError, "Unsupported"):
			recipients.send_invite("Customer", "x")

	def test_recipient_endpoints_are_role_gated(self):
		frappe.set_user("Guest")
		for call in (
			lambda: recipients.send_invite("Employee", EMPLOYEE),
			lambda: recipients.refresh_recipient("Employee", EMPLOYEE),
			lambda: recipients.create_recipient_directly("Employee", EMPLOYEE, "021000021", "123"),
			lambda: recipients.list_unmatched_recipients(),
			lambda: recipients.match_recipient("Employee", EMPLOYEE, "rec-1"),
			lambda: recipients.link_existing_recipients(),
		):
			with self.assertRaises(frappe.PermissionError):
				call()

	def test_refresh_follows_the_invite(self):
		frappe.db.set_value("Employee", EMPLOYEE, "mercury_invite_id", "inv-1")
		self.api.get("recipients/invites/inv-1", {"id": "inv-1", "status": "created"})
		self.assertEqual(recipients.refresh_recipient("Employee", EMPLOYEE)["status"], "Invite Sent")

		self.api.get("recipients/invites/inv-1", {"id": "inv-1", "status": "expired"})
		self.assertEqual(recipients.refresh_recipient("Employee", EMPLOYEE)["status"], "")

		frappe.db.set_value("Employee", EMPLOYEE, "mercury_invite_id", None)
		self.assertEqual(recipients.refresh_recipient("Employee", EMPLOYEE)["detail"], "No recipient yet")

		self.link_recipient()
		self.api.get("recipient/rec-1", recipient_payload(status="disabled"))
		result = recipients.refresh_recipient("Employee", EMPLOYEE)
		self.assertEqual(result["status"], "Disabled")
		self.assertEqual(result["account_last4"], "6789")

		with self.assertRaisesRegex(frappe.ValidationError, "not found"):
			recipients.refresh_recipient("Employee", "no-such-employee")

	def test_create_directly_sends_the_details_and_stores_none(self):
		self.api.post("recipients", recipient_payload(id="rec-2"))
		result = recipients.create_recipient_directly(
			"Supplier", SUPPLIER, "021000021", "000123456789", "businessChecking"
		)
		self.assertEqual(result, {"status": "Active", "account_last4": "6789"})
		body = self.api.body("POST", "recipients")
		self.assertEqual(body["electronicRoutingInfo"]["accountNumber"], "000123456789")
		self.assertEqual(body["emails"], [SUPPLIER_EMAIL])
		self.assertEqual(frappe.db.get_value("Supplier", SUPPLIER, "mercury_recipient_id"), "rec-2")

	def test_unmatched_contacts_put_the_likely_match_first(self):
		self.link_recipient("Supplier", SUPPLIER, "rec-claimed")
		self.api.get(
			"recipients",
			{
				"recipients": [
					recipient_payload(id="rec-a", name="Somebody Else", emails=["other@example.com"]),
					recipient_payload(id="rec-b", name="Zed", emails=[EMPLOYEE_EMAIL]),
					recipient_payload(id="rec-off", status="deleted"),
					recipient_payload(id="rec-claimed"),
				]
			},
		)
		rows = recipients.list_unmatched_recipients("Employee", EMPLOYEE)
		self.assertEqual([row["recipient_id"] for row in rows], ["rec-b", "rec-a"])
		self.assertTrue(rows[0]["suggested"])
		self.assertEqual(rows[0]["suggested_because"], "same email")

		employee_name = cast("str", frappe.db.get_value("Employee", EMPLOYEE, "employee_name"))
		self.api.get(
			"recipients", {"recipients": [recipient_payload(id="rec-c", name=employee_name, emails=[])]}
		)
		rows = recipients.list_unmatched_recipients("Employee", EMPLOYEE)
		self.assertEqual(rows[0]["suggested_because"], "same name")

	def test_match_links_an_existing_contact_once(self):
		self.api.get("recipient/rec-1", recipient_payload())
		result = recipients.match_recipient("Employee", EMPLOYEE, "rec-1")
		self.assertEqual(result["status"], "Active")
		with self.assertRaisesRegex(frappe.ValidationError, "already linked to Mercury contact"):
			recipients.match_recipient("Employee", EMPLOYEE, "rec-1")
		with self.assertRaisesRegex(frappe.ValidationError, "already linked to Employee"):
			recipients.match_recipient("Supplier", SUPPLIER, "rec-1")
		with self.assertRaisesRegex(frappe.ValidationError, "Select a Mercury contact"):
			recipients.match_recipient("Supplier", SUPPLIER, "")

	def test_link_existing_recipients_never_guesses(self):
		employee_name = cast("str", frappe.db.get_value("Employee", EMPLOYEE, "employee_name"))
		self.api.get(
			"recipients",
			{
				"recipients": [
					recipient_payload(id="rec-email", name="Payroll Name", emails=[EMPLOYEE_EMAIL]),
					recipient_payload(id="rec-name", name="_Test Employee 1", emails=[]),
					recipient_payload(id="rec-dup-1", name="_Test Employee 2", emails=[]),
					recipient_payload(id="rec-dup-2", name="_Test Employee 2", emails=[]),
				]
			},
		)
		dry = recipients.link_existing_recipients("Employee", dry_run=True)
		self.assertIn(f"{employee_name} ({EMPLOYEE})", [row["party"] for row in dry["linked"]])
		self.assertTrue(dry["ambiguous"])
		self.assertIsNone(frappe.db.get_value("Employee", EMPLOYEE, "mercury_recipient_id"))

		applied = recipients.link_existing_recipients("Employee", dry_run=False, parties=[EMPLOYEE])
		self.assertEqual([row["recipient_id"] for row in applied["linked"]], ["rec-email"])
		self.assertEqual(frappe.db.get_value("Employee", EMPLOYEE, "mercury_recipient_id"), "rec-email")

	def test_link_existing_reports_a_recipient_two_parties_claim(self):
		frappe.db.set_value("Supplier", "_Test Supplier 1", "email_id", SUPPLIER_EMAIL)
		self.api.get("recipients", {"recipients": [recipient_payload(id="rec-v", emails=[SUPPLIER_EMAIL])]})
		result = recipients.link_existing_recipients("Supplier", include_inactive_parties=True)
		self.assertIn("also matches other ERP records", str(result["ambiguous"]))

	def test_sync_recipients_logs_and_continues(self):
		frappe.db.set_value(
			"Employee", EMPLOYEE, {"mercury_recipient_status": "Invite Sent", "mercury_invite_id": "inv-x"}
		)
		log_error = self._patch("frappe.log_error")
		recipients.sync_recipients()
		log_error.assert_called_once()

		set_settings(enable_payouts=0)
		recipients.sync_recipients()
		log_error.assert_called_once()


class TestPayrollPayouts(PayoutTestCase):
	def test_preview_lists_what_will_be_sent(self):
		self.link_recipient()
		slips = make_payroll([(EMPLOYEE, 1234.567), ("_T-Employee-00002", 500.0)])
		rows = {row["salary_slip"]: row for row in payroll.get_payroll_payout_preview(PAYROLL_ENTRY)}
		self.assertEqual(rows[slips[0]]["amount"], 1234.57)  # exact net pay, never rounded_total
		self.assertTrue(rows[slips[0]]["sendable"])
		self.assertFalse(rows[slips[1]]["sendable"])  # no recipient
		self.assertIsNone(rows[slips[0]]["warning"])

	def test_preview_warns_about_mercurys_duplicate_guard(self):
		self.link_recipient()
		(earlier,) = make_payroll([(EMPLOYEE, 100.0)], "HR-PRUN-MERCURY-0")
		frappe.db.set_value("Salary Slip", earlier, "mercury_payment_status", "Sent")
		frappe.get_doc(
			{
				"doctype": "Integration Request",
				"integration_request_service": "Mercury",
				"request_description": "Payout",
				"reference_doctype": "Salary Slip",
				"reference_docname": earlier,
				"status": "Completed",
			}
		).insert(ignore_permissions=True)
		make_payroll([(EMPLOYEE, 100.0)])
		(row,) = payroll.get_payroll_payout_preview(PAYROLL_ENTRY)
		self.assertIn("Mercury will block it", row["warning"])

	def test_initiate_checks_the_slips_and_queues_the_run(self):
		self.link_recipient()
		slips = make_payroll([(EMPLOYEE, 100.0)])
		enqueue = self._patch("frappe.enqueue")
		payroll.initiate_payroll_payouts(PAYROLL_ENTRY, frappe.as_json(slips))
		self.assertEqual(enqueue.call_args.kwargs["salary_slips"], slips)
		self.assertEqual(enqueue.call_args.kwargs["job_id"], f"mercury_payroll::{PAYROLL_ENTRY}")

		with self.assertRaisesRegex(frappe.ValidationError, "No salary slips selected"):
			payroll.initiate_payroll_payouts(PAYROLL_ENTRY, [])
		with self.assertRaisesRegex(frappe.ValidationError, "do not belong"):
			payroll.initiate_payroll_payouts(PAYROLL_ENTRY, ["Sal Slip/other"])

		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			payroll.initiate_payroll_payouts(PAYROLL_ENTRY, slips)
		with self.assertRaises(frappe.PermissionError):
			payroll.get_payroll_payout_preview(PAYROLL_ENTRY)

	def test_run_sends_each_slip_with_an_idempotency_key(self):
		self.link_recipient()
		slips = make_payroll([(EMPLOYEE, 1234.567), ("_T-Employee-00002", 500.0)])
		self.api.post(
			f"account/{CHECKING_ID}/transactions", transaction_payload(id=uuid(10), amount=-1234.57)
		)
		payroll_notify = self._patch("mercury_banking_integration.payouts.payroll.notify_failure")
		realtime = self._patch("frappe.publish_realtime")

		payroll.run_payroll_payouts(PAYROLL_ENTRY, slips)

		body = self.api.body("POST", f"account/{CHECKING_ID}/transactions")
		self.assertEqual(body["amount"], 1234.57)
		self.assertEqual(body["idempotencyKey"], f"{slips[0]}:1")
		self.assertEqual(body["recipientId"], "rec-1")
		self.assertEqual(
			frappe.db.get_value(
				"Salary Slip", slips[0], ["mercury_transaction_id", "mercury_payment_status"]
			),
			(uuid(10), "Sent"),
		)
		# the second employee has no recipient: failed, alerted, and in the summary
		self.assertEqual(frappe.db.get_value("Salary Slip", slips[1], "mercury_payment_status"), "Failed")
		payroll_notify.assert_called_once()
		self.assertEqual(realtime.call_args.args[0], "mercury_payroll_done")
		self.assertIn("1 sent, 0 skipped, 1 failed of 2", realtime.call_args.args[1]["summary"])
		self.commit.assert_called()

	def test_a_crashing_slip_is_logged_and_the_run_continues(self):
		slips = make_payroll([(EMPLOYEE, 100.0)])
		self._patch(
			"mercury_banking_integration.payouts.payroll.send_payout", side_effect=RuntimeError("boom")
		)
		self._patch("mercury_banking_integration.payouts.payroll.notify_failure")
		self._patch("frappe.publish_realtime")
		log_error = self._patch("frappe.log_error")
		payroll.run_payroll_payouts(PAYROLL_ENTRY, slips)
		log_error.assert_called_once()

	def test_skipped_slips_are_summarised(self):
		slips = make_payroll([(EMPLOYEE, 100.0)])
		frappe.db.set_value("Salary Slip", slips[0], "mercury_payment_status", "Sent")
		realtime = self._patch("frappe.publish_realtime")
		payroll.run_payroll_payouts(PAYROLL_ENTRY, slips)
		self.assertIn("1 skipped", realtime.call_args.args[1]["summary"])

	def test_slip_with_a_payout_in_flight_cannot_be_cancelled(self):
		with self.assertRaisesRegex(frappe.ValidationError, "payout in flight"):
			payroll.block_cancel_if_active_payout(
				frappe._dict(name="Sal Slip/1", mercury_payment_status="Pending Approval")
			)
		payroll.block_cancel_if_active_payout(
			frappe._dict(name="Sal Slip/1", mercury_payment_status="Failed")
		)


class TestSendPayout(PayoutTestCase):
	def test_approval_mode_then_rejection(self):
		self.link_recipient()
		set_settings(payout_mode="Request Approval")
		(slip,) = make_payroll([(EMPLOYEE, 100.0)])
		self.api.post(
			f"account/{CHECKING_ID}/request-send-money", {"requestId": "req-1", "status": "pendingApproval"}
		)
		self.assertEqual(send.send_payout("Salary Slip", slip), {"pending_approval": "req-1"})
		self.assertEqual(
			frappe.db.get_value("Salary Slip", slip, "mercury_payment_status"), "Pending Approval"
		)

		self.api.get("request-send-money/req-1", {"requestId": "req-1", "status": "pendingApproval"})
		send.poll_approval_requests()
		self.assertEqual(
			frappe.db.get_value("Salary Slip", slip, "mercury_payment_status"), "Pending Approval"
		)

		self.api.get("request-send-money/req-1", {"requestId": "req-1", "status": "rejected"})
		send.poll_approval_requests()
		self.assertEqual(frappe.db.get_value("Salary Slip", slip, "mercury_payment_status"), "Rejected")
		self.notify.assert_called_once()

	def test_approved_request_is_adopted_from_its_transaction(self):
		(slip,) = make_payroll([(EMPLOYEE, 100.0)])
		frappe.db.set_value(
			"Salary Slip",
			slip,
			{"mercury_payment_status": "Pending Approval", "mercury_approval_request_id": "req-1"},
		)
		from mercury_banking_integration.client.models import MercuryTransaction

		pending = MercuryTransaction.model_validate(
			transaction_payload(id=uuid(20), requestId="req-1", status="pending", postedAt=None)
		)
		send.on_transaction_event(pending)
		self.assertEqual(
			frappe.db.get_value("Salary Slip", slip, ["mercury_transaction_id", "mercury_payment_status"]),
			(uuid(20), "Pending Approval"),
		)
		sent = MercuryTransaction.model_validate(transaction_payload(id=uuid(20), postedAt=None))
		send.on_transaction_event(sent)
		self.assertEqual(frappe.db.get_value("Salary Slip", slip, "mercury_payment_status"), "Sent")
		send.on_transaction_event(MercuryTransaction.model_validate(transaction_payload(id=uuid(20))))
		self.assertEqual(frappe.db.get_value("Salary Slip", slip, "mercury_payment_status"), "Posted")

		send.on_transaction_event(
			MercuryTransaction.model_validate(
				transaction_payload(id=uuid(20), status="failed", reasonForFailure="closed account")
			)
		)
		self.assertEqual(frappe.db.get_value("Salary Slip", slip, "mercury_payment_status"), "Failed")
		self.notify.assert_called_once()

		# a transaction no payout knows about is ignored
		send.on_transaction_event(MercuryTransaction.model_validate(transaction_payload(id=uuid(21))))

	def test_poll_failure_is_logged(self):
		(slip,) = make_payroll([(EMPLOYEE, 100.0)])
		frappe.db.set_value(
			"Salary Slip",
			slip,
			{"mercury_payment_status": "Pending Approval", "mercury_approval_request_id": "req-gone"},
		)
		log_error = self._patch("frappe.log_error")
		send.poll_approval_requests()
		log_error.assert_called_once()

	def test_idempotency_replay_adopts_the_original_transaction(self):
		self.link_recipient()
		(slip,) = make_payroll([(EMPLOYEE, 100.0)])
		self.api.post(f"account/{CHECKING_ID}/transactions", transaction_payload(id=uuid(30)), status=409)
		self.assertEqual(send.send_payout("Salary Slip", slip), {"sent": uuid(30), "adopted_replay": True})
		self.assertEqual(frappe.db.get_value("Salary Slip", slip, "mercury_payment_status"), "Sent")

	def test_a_conflict_without_a_transaction_is_raised(self):
		from mercury_banking_integration.client import MercuryConflictError

		self.link_recipient()
		(slip,) = make_payroll([(EMPLOYEE, 100.0)])
		self.api.post(f"account/{CHECKING_ID}/transactions", {"errors": {"message": "busy"}}, status=409)
		with self.assertRaises(MercuryConflictError):
			send.send_payout("Salary Slip", slip)

	def test_duplicate_guard_blocks_then_waits_24_hours(self):
		self.link_recipient()
		(slip,) = make_payroll([(EMPLOYEE, 100.0)])
		self.api.post(
			f"account/{CHECKING_ID}/transactions",
			{"errors": {"message": "Duplicate payment detected"}},
			status=400,
		)
		result = send.send_payout("Salary Slip", slip)
		self.assertEqual(result["status"], "Blocked - Duplicate")
		self.notify.assert_called_once()

		self.assertEqual(
			send.send_payout("Salary Slip", slip),
			{"skipped": "Mercury's 24h duplicate guard is still active"},
		)

		# a day later the next attempt carries a fresh idempotency key
		frappe.db.set_value(
			"Integration Request",
			{"reference_docname": slip},
			"creation",
			add_to_date(now_datetime(), hours=-25),
			update_modified=False,
		)
		self.api.post(f"account/{CHECKING_ID}/transactions", transaction_payload(id=uuid(31)))
		self.assertEqual(send.send_payout("Salary Slip", slip), {"sent": uuid(31)})
		self.assertEqual(
			self.api.body("POST", f"account/{CHECKING_ID}/transactions")["idempotencyKey"], f"{slip}:2"
		)

	def test_preconditions(self):
		(slip,) = make_payroll([(EMPLOYEE, 0.0)])
		self.assertEqual(send.send_payout("Salary Slip", slip), {"skipped": "nothing to pay"})
		frappe.db.set_value("Salary Slip", slip, "docstatus", 0)
		self.assertEqual(send.send_payout("Salary Slip", slip), {"skipped": "not submitted"})

		with self.assertRaisesRegex(frappe.ValidationError, "not found"):
			send.send_payout("Salary Slip", "no-such-slip")
		with self.assertRaisesRegex(frappe.ValidationError, "Unsupported payout reference"):
			send.send_payout("Journal Entry", "x")

		set_settings(enable_payouts=0)
		with self.assertRaisesRegex(frappe.ValidationError, "payouts are disabled"):
			send.send_payout("Salary Slip", slip)

	def test_funding_account_must_be_set_and_mapped(self):
		self.link_recipient()
		(slip,) = make_payroll([(EMPLOYEE, 10.0)])
		frappe.db.set_value("Bank Account", self.fixtures.checking, "mercury_account_id", None)
		with self.assertRaisesRegex(frappe.ValidationError, "no Mercury Account ID"):
			send.send_payout("Salary Slip", slip)
		set_settings(payroll_funding_bank_account=None)
		with self.assertRaisesRegex(frappe.ValidationError, "No funding Bank Account"):
			send.send_payout("Salary Slip", slip)

	def test_slip_payout_amount_is_exact(self):
		self.assertEqual(send.slip_payout_amount(1234.567), 1234.57)
		self.assertEqual(send.slip_payout_amount(None), 0.0)


class TestVendorPayouts(PayoutTestCase):
	def test_supplier_payment_is_sent_and_reference_recorded(self):
		self.link_recipient("Supplier", SUPPLIER, "rec-v")
		payment_entry = make_supplier_payment()
		# no reference yet: the Mercury transaction id becomes it
		frappe.db.set_value("Payment Entry", payment_entry, "reference_no", None)
		self.api.post(f"account/{CHECKING_ID}/transactions", transaction_payload(id=uuid(40), amount=-250))
		self.assertEqual(vendor.pay_payment_entry_via_mercury(payment_entry), {"sent": uuid(40)})
		self.assertEqual(
			frappe.db.get_value("Payment Entry", payment_entry, ["reference_no", "mercury_payment_status"]),
			(uuid(40), "Sent"),
		)

	def test_vendor_preconditions(self):
		payment_entry = make_supplier_payment()
		frappe.db.set_value("Payment Entry", payment_entry, "clearance_date", today())
		with self.assertRaisesRegex(frappe.ValidationError, "already cleared"):
			vendor.pay_payment_entry_via_mercury(payment_entry)
		frappe.db.set_value(
			"Payment Entry", payment_entry, {"clearance_date": None, "party_type": "Employee"}
		)
		with self.assertRaisesRegex(frappe.ValidationError, "Only supplier payments"):
			vendor.pay_payment_entry_via_mercury(payment_entry)
		frappe.db.set_value("Payment Entry", payment_entry, "docstatus", 0)
		with self.assertRaisesRegex(frappe.ValidationError, "must be submitted"):
			vendor.pay_payment_entry_via_mercury(payment_entry)
		with self.assertRaisesRegex(frappe.ValidationError, "Only supplier payments"):
			send._context("Payment Entry", payment_entry)
		with self.assertRaisesRegex(frappe.ValidationError, "not found"):
			send._context("Payment Entry", "no-such-payment")

		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			vendor.pay_payment_entry_via_mercury(payment_entry)

	def test_a_different_bank_account_is_noted(self):
		self.link_recipient("Supplier", SUPPLIER, "rec-v")
		payment_entry = make_supplier_payment()
		frappe.db.set_value("Payment Entry", payment_entry, "bank_account", self.fixtures.savings)
		self.api.post(f"account/{CHECKING_ID}/transactions", transaction_payload(id=uuid(41), amount=-250))
		msgprint = self._patch("mercury_banking_integration.payouts.vendor.frappe.msgprint")
		vendor.pay_payment_entry_via_mercury(payment_entry)
		msgprint.assert_called_once()
