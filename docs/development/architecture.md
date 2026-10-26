---
title: How it works
description: "The design of Mercury Banking for ERPNext: the API client, the modules, the event pipeline, how bookings stay idempotent, and where state is kept."
nav_title: How it works
order: 10
updated: 2026-10-26
---

The app adds one DocType and keeps everything else on ERPNext's own documents. Mercury ids live in read-only custom fields, inbound events and payout attempts are Integration Requests, and every booking is an ordinary Journal Entry or Payment Entry reconciled through ERPNext's bank reconciliation code.

## The pieces

| Path (under `mercury_banking_integration/`) | What                                                                                                                |
| ------------------------------------------- | ------------------------------------------------------------------------------------------------------------------- |
| `client/`                                   | The Mercury API client. It does not import Frappe.                                                                  |
| `sync/client_factory.py`                    | The only place that builds the client from **Mercury Settings**.                                                    |
| `sync/`                                     | Accounts, transactions, events (webhook, poll, replay), automatic journal entries, GL codes and internal transfers. |
| `ar/`                                       | The payment gateway controller, Mercury customers and invoices, and the funding Journal Entry.                      |
| `payouts/`                                  | Recipients and invites, payroll and supplier payouts, the send path both share, and reconciliation.                 |
| `webhooks.py`, `tasks.py`                   | The webhook endpoint and the scheduler entry points that `hooks.py` lists.                                          |
| `utils/alerts.py`                           | The alert emails to **Alert Recipients**. It never raises.                                                          |
| `migrate.py`                                | The one-off migration helpers (see [Development](README.md#migration-helpers)).                                     |
| `mercury_integration/`                      | The module: the **Mercury Settings** DocType and the custom fields in `custom/*.json`, synced on every migrate.     |
| `public/js/`                                | Form scripts for Payroll Entry, Payment Entry, Payment Request, Employee and Supplier.                              |
| `patches/`                                  | The two data patches listed in [Install and upgrade](../install.md#upgrade).                                        |
| `demo.py`                                   | The demo data for `frappe-demo` and the screenshots, run against an offline stand-in for the API.                   |

## The API client

`client/` follows Mercury's official Go SDK: Pydantic v2 models, retries on 408, 429 and 5xx responses with `Retry-After` honoured, cursor pagination, and idempotency keys sent in the request body. Enum values are read as plain strings, so a new value from Mercury never breaks parsing. Mercury signs amounts with negative meaning money out, the opposite of Plaid.

## Events

The webhook (`webhooks.webhook`, guest, POST only) checks the `Mercury-Signature` header, an HMAC-SHA256 over the timestamp and the raw body with the stored secret, and refuses the request when it does not match. A valid delivery is recorded and answered at once; a background job on the `short` queue does the work.

`sync.events.ingest_event` records each event as an Integration Request (service `Mercury`, `is_remote_request` set, the event id in `request_id`, the channel in `request_description`) under a file lock, and skips an event id that is already recorded. The poller and **Replay Events** go through the same function. Handlers always fetch the transaction from Mercury again, so duplicate or out-of-order events converge on the same state. Frappe's Webhook Request Log is not used: core writes it only for its own outbound Webhook DocType.

The poller keeps its place in the hidden `events_cursor` field and reads 100 events per job, queuing a continuation while more remain.

## Transactions

`sync.transactions.upsert_transaction` is the one path from a Mercury transaction to a Bank Transaction, keyed on `transaction_id` under a file lock. After it writes a submitted Bank Transaction it calls `sync.transfers.reconcile_internal_transfer` and `sync.auto_journal.evaluate`. `Bank Account.last_integration_date` records the newest transaction seen, and each windowed sync starts 3 days before it.

## Bookings and idempotency

Every Journal Entry the app books carries a Mercury id in `cheque_no`, and the app looks for a submitted entry with that `cheque_no` before booking:

| Booking                 | `cheque_no`                         | Built by                                                                                                                                                                                                               |
| ----------------------- | ----------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Automatic journal entry | The transaction id                  | Assembled in `sync/auto_journal.py`, because ERPNext's `create_journal_entry_bts` carries no party and refuses a Payable or Receivable second account. Reconciled with `reconcile_vouchers(..., is_new_voucher=True)`. |
| Internal transfer       | The money-out side's transaction id | ERPNext's `create_journal_entry_bts` for the money-out side, then `reconcile_vouchers` for the money-in side, as ERPNext's own internal transfer does.                                                                 |
| Invoice funding         | The Mercury invoice id              | `ar/funding.py`, on Bank Transaction submit.                                                                                                                                                                           |

The settlement Payment Entry of a paid invoice carries the invoice id in `reference_no` and is made with ERPNext's `get_payment_entry`. Bookings that run from a webhook job switch to Administrator for the booking only, because ERPNext's reconciliation saves with the session user's permissions. The automatic journal entry is booked inside a savepoint, so a failure rolls back the draft and leaves the Bank Transaction work in place.

## Invoicing

The `Mercury` Payment Gateway has no controller, so the payments app resolves **Mercury Settings** as its controller. `ar.gateway.MercuryARGatewayMixin`, mixed into the settings class, provides `validate_transaction_currency`, `get_payment_url` and `on_payment_request_submission`. The last one creates the invoice and caches its result on the document's flags, and never raises. Mercury has no invoice webhooks, so `ar.invoices.poll_open_invoices` reads invoice status every 15 minutes.

## Payouts

Payout state is on the Salary Slip or Payment Entry (`mercury_transaction_id`, `mercury_payment_status`, `mercury_approval_request_id`). Each attempt is an Integration Request with `request_description` `Payout`, and its `request_id`, `<document name>:<attempt>`, is the idempotency key; the attempt number counts earlier failed attempts. A 409 from Mercury is an idempotency replay, and the original transaction is adopted from the error body. A 400 that mentions a duplicate sets `Blocked - Duplicate`.

Payroll payments are never sent on submit. Each employee's payment is its own Bank Transaction, reconciled against the single bank Journal Entry from `Payroll Entry.make_bank_entry`; ERPNext's partial allocation clears that entry once every payment has landed. The amount is `net_pay` to the cent, never `rounded_total`.

## Bank accounts

The app identifies a Bank Account by its own `mercury_account_id` field, never `integration_id`, which Plaid's scheduler selects on. **Sync Accounts** runs in two passes, active accounts first, so that an active account claims an existing Bank Account before an archived one with the same nickname can.
