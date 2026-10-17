# Copyright (c) 2026, Avunu LLC and contributors
# For license information, please see license.txt

"""The receivables flow end to end, Mercury API mocked.

Payment Request submitted → Mercury AR customer and invoice created → the poll
sees it Paid → settlement Payment Entry against the AR clearing account → the
deposit Bank Transaction lands → funding Journal Entry, reconciled.
"""

from __future__ import annotations

from typing import Any, cast

import frappe
from frappe.utils import add_days, today

from mercury_integration.ar import funding, invoices
from mercury_integration.ar.customers import ensure_ar_customer
from mercury_integration.tests.utils import (
	ABBR,
	CHECKING_ID,
	COMPANY,
	CUSTOMER,
	MercuryTestCase,
	invoice_payload,
	set_settings,
)

PAYER_EMAIL = "payer@example.com"


def make_sales_invoice(rate: float = 100.0) -> str:
	from erpnext.accounts.doctype.sales_invoice.test_sales_invoice import create_sales_invoice

	invoice = create_sales_invoice(
		company=COMPANY,
		customer=CUSTOMER,
		debit_to=f"Debtors - {ABBR}",
		income_account=f"Sales - {ABBR}",
		expense_account=f"Cost of Goods Sold - {ABBR}",
		cost_center=f"Main - {ABBR}",
		warehouse=f"Stores - {ABBR}",
		currency="USD",
		rate=rate,
	)
	return str(invoice.name)


class ARTestCase(MercuryTestCase):
	def setUp(self) -> None:
		super().setUp()
		self.configure(
			enable_ar_gateway=1,
			ar_clearing_account=self.fixtures.clearing_gl,
			ar_destination_bank_account=self.fixtures.checking,
			card_fees_account=self.fixtures.fees_gl,
			ar_email_sender="ERP",
		)
		frappe.db.set_value("Customer", CUSTOMER, {"email_id": PAYER_EMAIL, "mercury_customer_id": None})
		self.notify = self._patch("mercury_integration.ar.invoices.notify_failure")
		self.gateway_account = cast(
			"str", frappe.db.get_value("Payment Gateway Account", {"payment_gateway": "Mercury"})
		)

	def mock_invoice_creation(self, invoice_id: str = "inv-1", **invoice: Any) -> None:
		self.api.post("ar/customers", {"id": "cust-1", "name": CUSTOMER, "email": PAYER_EMAIL})
		self.api.post("ar/invoices", invoice_payload(id=invoice_id, slug=f"pay-{invoice_id}", **invoice))

	def make_payment_request(self, rate: float = 100.0):
		from erpnext.accounts.doctype.payment_request.payment_request import make_payment_request

		return make_payment_request(
			dt="Sales Invoice",
			dn=make_sales_invoice(rate),
			payment_gateway_account=self.gateway_account,
			recipient_id=PAYER_EMAIL,
			submit_doc=True,
			mute_email=True,
			return_doc=True,
		)


class TestInvoiceCreation(ARTestCase):
	def test_submitting_a_payment_request_creates_the_mercury_invoice(self):
		self.mock_invoice_creation()
		pr = self.make_payment_request()

		self.assertEqual(pr.mercury_invoice_id, "inv-1")
		self.assertEqual(pr.payment_url, "https://app.mercury.com/pay/pay-inv-1")
		self.assertEqual(frappe.db.get_value("Customer", CUSTOMER, "mercury_customer_id"), "cust-1")

		body = self.api.body("POST", "ar/invoices")
		self.assertEqual(body["customerId"], "cust-1")
		self.assertEqual(body["destinationAccountId"], CHECKING_ID)
		self.assertEqual(body["sendEmailOption"], "DontSend")
		self.assertEqual(body["lineItems"][0]["unitPrice"], 100.0)
		self.assertEqual(self.api.body("POST", "ar/customers")["email"], PAYER_EMAIL)

	def test_the_gateway_caches_its_answer_and_reports_the_email_sender(self):
		from mercury_integration.sync.client_factory import get_settings

		self.mock_invoice_creation()
		set_settings(ar_email_sender="Mercury")
		pr = self.make_payment_request()
		# Mercury emails the payer, so the ERP must not
		self.assertFalse(get_settings().on_payment_request_submission(pr))
		self.assertEqual(len(self.api.calls("POST", "ar/invoices")), 1)
		self.assertEqual(self.api.body("POST", "ar/invoices")["sendEmailOption"], "SendNow")

	def test_a_failed_creation_alerts_and_the_hourly_retry_recovers(self):
		self.api.post("ar/customers", {"id": "cust-1"})
		self.api.post("ar/invoices", {"errors": {"message": "nope"}}, status=400)
		gateway_notify = self._patch("mercury_integration.utils.alerts.notify_failure")
		pr = self.make_payment_request()
		self.assertFalse(frappe.db.get_value("Payment Request", pr.name, "mercury_invoice_id"))
		gateway_notify.assert_called_once()

		self.api.post("ar/invoices", invoice_payload(id="inv-2", slug="pay-inv-2"))
		send_email = self._patch(
			"erpnext.accounts.doctype.payment_request.payment_request.PaymentRequest.send_email"
		)
		invoices.retry_failed_invoice_creation()
		self.assertEqual(frappe.db.get_value("Payment Request", pr.name, "mercury_invoice_id"), "inv-2")
		send_email.assert_called_once()

	def test_retry_failure_is_logged_and_rolled_back(self):
		self.api.post("ar/customers", {"id": "cust-1"})
		self.api.post("ar/invoices", {"errors": {"message": "nope"}}, status=400)
		self._patch("mercury_integration.utils.alerts.notify_failure")
		self.make_payment_request()
		log_error = self._patch("frappe.log_error")
		invoices.retry_failed_invoice_creation()
		log_error.assert_called_once()

	def test_recreate_button_needs_an_accounts_role_and_is_idempotent(self):
		self.mock_invoice_creation()
		pr = self.make_payment_request()
		self.assertEqual(invoices.recreate_mercury_invoice(pr.name), "inv-1")
		self.assertEqual(len(self.api.calls("POST", "ar/invoices")), 1)

		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			invoices.recreate_mercury_invoice(pr.name)

	def test_a_sibling_request_gets_a_distinct_invoice_number(self):
		self.mock_invoice_creation()
		pr = self.make_payment_request()
		self.assertEqual(self.api.body("POST", "ar/invoices")["invoiceNumber"], pr.reference_name)

		# a second request against the same invoice must not reuse the payer-facing number
		other = frappe._dict(
			name="PR-OTHER", reference_doctype="Sales Invoice", reference_name=pr.reference_name
		)
		self.assertEqual(invoices._invoice_number(other), f"{pr.reference_name} / PR-OTHER")
		# requests for anything but a Sales Invoice use their own name
		self.assertEqual(
			invoices._invoice_number(frappe._dict(name="PR-SO", reference_doctype="Sales Order")), "PR-SO"
		)

	def test_creation_preconditions(self):
		pr = frappe._dict(mercury_invoice_id=None, party_type="Supplier", party="x", name="PR-X")
		with self.assertRaisesRegex(frappe.ValidationError, "requires a Customer"):
			invoices.ensure_invoice_for_payment_request(pr)

		set_settings(enable_ar_gateway=0)
		with self.assertRaisesRegex(frappe.ValidationError, "AR gateway is disabled"):
			invoices.ensure_invoice_for_payment_request(pr)

		self.assertEqual(
			invoices.ensure_invoice_for_payment_request(frappe._dict(mercury_invoice_id="inv-9")), "inv-9"
		)

	def test_destination_account_must_be_mapped(self):
		set_settings(ar_destination_bank_account=None)
		from mercury_integration.sync.client_factory import get_settings

		with self.assertRaisesRegex(frappe.ValidationError, "no Mercury Account ID"):
			invoices._destination_account_id(get_settings())

	def test_customer_needs_an_email(self):
		frappe.db.set_value("Customer", CUSTOMER, "email_id", None)
		with self.assertRaisesRegex(frappe.ValidationError, "needs an email"):
			ensure_ar_customer(CUSTOMER)
		frappe.db.set_value("Customer", CUSTOMER, "mercury_customer_id", "cust-known")
		self.assertEqual(ensure_ar_customer(CUSTOMER), "cust-known")

	def test_unsupported_currency_is_refused(self):
		from mercury_integration.sync.client_factory import get_settings

		get_settings().validate_transaction_currency("USD")
		with self.assertRaisesRegex(frappe.ValidationError, "does not support"):
			get_settings().validate_transaction_currency("EUR")

	def test_pay_page_url(self):
		from mercury_integration.sync.client_factory import get_settings

		settings = get_settings()
		self.assertIsNone(settings.get_payment_url())
		self.mock_invoice_creation()
		pr = self.make_payment_request()
		self.assertEqual(
			settings.get_payment_url(reference_docname=pr.name), "https://app.mercury.com/pay/pay-inv-1"
		)

		# no stored URL: ask Mercury for the invoice
		frappe.db.set_value("Payment Request", pr.name, "payment_url", None)
		self.api.get("ar/invoices/inv-1", invoice_payload(slug="fresh"))
		self.assertEqual(invoices.get_pay_page_url(pr.name), "https://app.mercury.com/pay/fresh")
		frappe.db.set_value("Payment Request", pr.name, "mercury_invoice_id", None)
		self.assertIsNone(invoices.get_pay_page_url(pr.name))


class TestSettlementAndFunding(ARTestCase):
	def test_paid_invoice_settles_and_the_deposit_funds_it(self):
		self.mock_invoice_creation()
		pr = self.make_payment_request()

		self.api.get("ar/invoices/inv-1", invoice_payload(status="Paid"))
		invoices.poll_open_invoices()

		payment_entry = frappe.db.get_value(
			"Payment Entry", {"reference_no": "inv-1", "docstatus": 1}, ["name", "paid_to", "paid_amount"]
		)
		self.assertIsNotNone(payment_entry)
		self.assertEqual(payment_entry[1], self.fixtures.clearing_gl)
		self.assertEqual(payment_entry[2], 100.0)
		self.assertEqual(frappe.db.get_value("Payment Request", pr.name, "status"), "Paid")

		# a replayed poll does not book a second Payment Entry
		invoices.settle_paid_invoice(pr.name, invoices.get_client().get_ar_invoice("inv-1"))
		self.assertEqual(frappe.db.count("Payment Entry", {"reference_no": "inv-1", "docstatus": 1}), 1)

		bank_transaction = self.deposit(100.0)
		journal_entry = frappe.db.get_value("Journal Entry", {"cheque_no": "inv-1", "docstatus": 1})
		self.assertTrue(journal_entry)
		self.assertEqual(frappe.db.get_value("Bank Transaction", bank_transaction, "status"), "Reconciled")

	def test_card_fee_deposit_books_the_fee(self):
		self.mock_invoice_creation()
		set_settings(credit_card_enabled=1)
		self.make_payment_request()
		self.api.get("ar/invoices/inv-1", invoice_payload(status="Paid"))
		invoices.poll_open_invoices()

		self.deposit(96.80)  # 100 less 2.9% + 0.30
		journal_entry = cast(
			"str", frappe.db.get_value("Journal Entry", {"cheque_no": "inv-1", "docstatus": 1})
		)
		fee = frappe.db.get_value(
			"Journal Entry Account",
			{"parent": journal_entry, "account": self.fixtures.fees_gl},
			"debit_in_account_currency",
		)
		self.assertEqual(fee, 3.2)

	def test_fee_without_a_fee_account_alerts(self):
		self.mock_invoice_creation()
		set_settings(credit_card_enabled=1, card_fees_account=None)
		self.make_payment_request()
		self.api.get("ar/invoices/inv-1", invoice_payload(status="Paid"))
		invoices.poll_open_invoices()
		notify = self._patch("mercury_integration.ar.funding.notify_failure")
		self.deposit(96.80)
		notify.assert_called_once()
		self.assertFalse(frappe.db.exists("Journal Entry", {"cheque_no": "inv-1", "docstatus": 1}))

	def test_ambiguous_deposit_alerts_instead_of_guessing(self):
		notify = self._patch("mercury_integration.ar.funding.notify_failure")
		candidates = [
			frappe._dict(name=f"PR-{n}", mercury_invoice_id=f"inv-{n}", grand_total=50, paid_on=today())
			for n in (1, 2)
		]
		self._patch("mercury_integration.ar.funding._paid_unfunded_candidates", return_value=candidates)
		self.deposit(50.0)
		notify.assert_called_once()
		self.assertIn("Ambiguous", notify.call_args.args[0])

	def test_funding_ignores_other_accounts_and_withdrawals(self):
		candidates = self._patch("mercury_integration.ar.funding._paid_unfunded_candidates")
		funding.process_ar_funding_transaction(frappe._dict(deposit=0, bank_account=self.fixtures.checking))
		funding.process_ar_funding_transaction(frappe._dict(deposit=10, bank_account=self.fixtures.savings))
		set_settings(enable_ar_gateway=0)
		funding.process_ar_funding_transaction(frappe._dict(deposit=10, bank_account=self.fixtures.checking))
		candidates.assert_not_called()

	def test_failed_funding_journal_is_logged(self):
		log_error = self._patch("frappe.log_error")
		bank_transaction = frappe._dict(name="BT-X", bank_account=self.fixtures.checking, date=today())
		candidate = frappe._dict(name="PR-X", mercury_invoice_id="inv-x", grand_total=10)
		self._patch("frappe.get_doc", side_effect=RuntimeError("boom"))
		from mercury_integration.sync.client_factory import get_settings

		funding._create_funding_journal(bank_transaction, candidate, 10.0, get_settings())
		log_error.assert_called_once()

	def test_processing_marks_the_request_initiated(self):
		self.mock_invoice_creation()
		pr = self.make_payment_request()
		self.api.get("ar/invoices/inv-1", invoice_payload(status="Processing"))
		invoices.poll_open_invoices()
		self.assertEqual(frappe.db.get_value("Payment Request", pr.name, "status"), "Initiated")

	def test_upstream_cancel_cancels_the_request(self):
		self.mock_invoice_creation()
		pr = self.make_payment_request()
		self.api.get("ar/invoices/inv-1", invoice_payload(status="Cancelled"))
		invoices.poll_open_invoices()
		self.assertEqual(frappe.db.get_value("Payment Request", pr.name, "status"), "Cancelled")
		self.notify.assert_called_once()

	def test_poll_errors_are_logged_per_request(self):
		self.mock_invoice_creation()
		self.make_payment_request()
		log_error = self._patch("frappe.log_error")
		self.api.get("ar/invoices/inv-1", {"errors": {}}, status=404)
		invoices.poll_open_invoices()
		self.assertIn("poll failed", log_error.call_args.kwargs["title"])

		self.api.get("ar/invoices/inv-1", invoice_payload(status="Paid"))
		self._patch("mercury_integration.ar.invoices._apply_invoice_status", side_effect=RuntimeError("x"))
		invoices.poll_open_invoices()
		self.assertIn("transition failed", log_error.call_args.kwargs["title"])

	def test_settlement_failure_alerts(self):
		self.mock_invoice_creation()
		pr = self.make_payment_request()
		self._patch(
			"erpnext.accounts.doctype.payment_entry.payment_entry.get_payment_entry",
			side_effect=RuntimeError("boom"),
		)
		self._patch("frappe.log_error")
		invoices.settle_paid_invoice(pr.name, cast("Any", frappe._dict(amount=100)))
		self.notify.assert_called_once()

	def test_jobs_are_off_while_the_gateway_is(self):
		set_settings(enable_ar_gateway=0)
		before = len(self.api.calls())
		invoices.poll_open_invoices()
		invoices.retry_failed_invoice_creation()
		invoices.send_overdue_reminders()
		self.assertEqual(len(self.api.calls()), before)

	def deposit(self, amount: float) -> str:
		bank_transaction = frappe.get_doc(
			{
				"doctype": "Bank Transaction",
				"date": today(),
				"bank_account": self.fixtures.checking,
				"deposit": amount,
				"currency": "USD",
				"description": "Mercury invoice payment",
			}
		)
		bank_transaction.insert(ignore_permissions=True)
		bank_transaction.submit()
		return str(bank_transaction.name)


class TestCancelAndReminders(ARTestCase):
	def test_cancelling_the_request_cancels_an_unpaid_invoice(self):
		self.mock_invoice_creation()
		pr = self.make_payment_request()
		self.api.get("ar/invoices/inv-1", invoice_payload(status="Unpaid"))
		self.api.post("ar/invoices/inv-1/cancel", invoice_payload(status="Cancelled"))
		pr.reload()
		pr.cancel()
		self.assertEqual(len(self.api.calls("POST", "ar/invoices/inv-1/cancel")), 1)

	def test_money_in_flight_blocks_the_cancel(self):
		self.mock_invoice_creation()
		pr = self.make_payment_request()
		self.api.get("ar/invoices/inv-1", invoice_payload(status="Processing"))
		pr.reload()
		with self.assertRaisesRegex(frappe.ValidationError, "money is in flight"):
			pr.cancel()

	def test_cancel_lookup_failure_is_logged(self):
		log_error = self._patch("frappe.log_error")
		invoices.cancel_mercury_invoice(frappe._dict(name="PR-X", mercury_invoice_id="inv-gone"))
		log_error.assert_called_once()
		invoices.cancel_mercury_invoice(frappe._dict(name="PR-Y"))
		log_error.assert_called_once()

	def test_overdue_reminders(self):
		self.mock_invoice_creation()
		set_settings(send_overdue_reminders=1, max_reminders=2, reminder_interval_days=7)
		pr = self.make_payment_request()
		frappe.db.set_value("Payment Request", pr.name, "transaction_date", add_days(today(), -10))
		send_email = self._patch(
			"erpnext.accounts.doctype.payment_request.payment_request.PaymentRequest.send_email"
		)
		self.api.get("ar/invoices/inv-1", invoice_payload(status="Unpaid"))

		invoices.send_overdue_reminders()
		self.assertEqual(send_email.call_count, 1)
		self.assertEqual(frappe.db.get_value("Payment Request", pr.name, "mercury_reminder_count"), 1)

		# within the interval: nothing
		invoices.send_overdue_reminders()
		self.assertEqual(send_email.call_count, 1)

		# the final reminder alerts
		frappe.db.set_value("Payment Request", pr.name, "mercury_last_reminder_on", add_days(today(), -8))
		invoices.send_overdue_reminders()
		self.assertEqual(send_email.call_count, 2)
		self.notify.assert_called_once()

		# past the maximum: nothing
		frappe.db.set_value("Payment Request", pr.name, "mercury_last_reminder_on", add_days(today(), -30))
		invoices.send_overdue_reminders()
		self.assertEqual(send_email.call_count, 2)

	def test_reminders_skip_settled_invoices_and_log_failures(self):
		self.mock_invoice_creation()
		set_settings(send_overdue_reminders=1)
		pr = self.make_payment_request()
		frappe.db.set_value("Payment Request", pr.name, "transaction_date", add_days(today(), -10))
		self.api.get("ar/invoices/inv-1", invoice_payload(status="Paid"))
		send_email = self._patch(
			"erpnext.accounts.doctype.payment_request.payment_request.PaymentRequest.send_email"
		)
		invoices.send_overdue_reminders()
		send_email.assert_not_called()

		log_error = self._patch("frappe.log_error")
		self.api.get("ar/invoices/inv-1", {"errors": {}}, status=404)
		invoices.send_overdue_reminders()
		log_error.assert_called_once()
