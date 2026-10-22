<!-- Copyright (c) 2026, Avunu LLC and contributors
For license information, please see license.txt-->

<!-- frappe-nix:begin header -->

<div align="center">

<img src="mercury_banking_integration/public/images/mercury_banking_integration-logo.svg" width="80" alt="">

# Mercury Banking for ERPNext

Mercury bank sync, ACH payouts, invoicing and GL coding for ERPNext

[![CI](https://github.com/Avunu/mercury_banking_integration/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/Avunu/mercury_banking_integration/actions/workflows/ci.yml)
[![Latest release](https://img.shields.io/github/v/release/Avunu/mercury_banking_integration)](https://github.com/Avunu/mercury_banking_integration/releases)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](license.txt)
[![Frappe v16](https://img.shields.io/badge/Frappe-v16-blue)](https://github.com/frappe/frappe/tree/version-16)

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/screenshots/settings-dark.webp">
  <img src="docs/screenshots/settings-light.webp" alt="Mercury Settings with sandbox credentials, automatic sync and auto journal entries turned on, and the Mercury menu open">
</picture>

</div>

<!-- frappe-nix:end header -->

## Overview

A business that banks with [Mercury](https://mercury.com) and keeps its books in ERPNext ends up entering the same money twice. This app connects the two, so ERPNext stays the source of truth: Mercury transactions arrive as Bank Transactions and are reconciled, Payment Requests become Mercury invoices with a pay link, and payroll and supplier payments go out by ACH from ERPNext.

It is for finance teams on ERPNext, payments and HRMS whose bank is Mercury. Each part (bank sync, automatic journal entries, invoicing and payouts) has its own switch in **Mercury Settings**, so you can start with bank sync alone.

## Features

- **Bank sync.** _Sync Accounts_ creates the `Mercury` Bank and a Bank Account (with its GL account) for every active Mercury account, or adopts an existing Bank Account with the same account number (or the same name under the `Mercury` bank). Posted transactions are imported hourly as submitted Bank Transactions, and _Backfill Transactions_ imports a chosen number of days. Pending transactions can be kept as drafts. Later changes in Mercury are applied to the Bank Transaction: a new posting date is written to it, and a changed amount or a failed or cancelled transaction cancels it (and re-imports it with the new amount). If it is already reconciled, an amount or status change is left alone and an alert is sent instead.
- **Live updates.** A signed webhook endpoint and a 15-minute poll of Mercury's events API both feed the same handler, and _Replay Events_ runs the last few hours again. Every event is recorded once as an Integration Request.
- **Automatic journal entries.** A transaction that carries a Mercury GL code is booked as a Journal Entry against the ERPNext account of the same name and reconciled with its Bank Transaction. Split transactions get one line per GL code, and a line on a payable or receivable account gets the Employee or Supplier linked to the Mercury counterparty as its party. The transaction's attachments are copied to the Bank Transaction and the Journal Entry. A daily sweep books transactions that were coded after they were synced.
- **Internal transfers.** A transfer between two Mercury accounts is booked as one Bank Entry and reconciled on both accounts.
- **GL code export.** _Export GL Codes_ downloads your expense, income, liability and equity account names as the CSV that Mercury's GL code upload page takes, and opens that page.
- **Invoicing (accounts receivable).** Mercury is registered as a payment gateway. Submitting a Payment Request creates a Mercury invoice and stores its pay link; ERPNext or Mercury e-mails it to the customer. When the invoice is paid, a Payment Entry is made against a clearing account, and when the deposit reaches the bank, a Journal Entry moves it from clearing to the bank account and reconciles it. Overdue reminders, cancellation and an hourly retry of failed invoice creation are included.
- **Payroll and supplier payouts.** _Pay via Mercury_ on a submitted Payroll Entry previews every salary slip and sends one ACH payment per employee; on a submitted supplier Payment Entry it pays that entry. Payments are either sent at once or queued for a second Mercury user to approve. Their status is tracked on the salary slip or Payment Entry, and the resulting Bank Transactions are reconciled against the payroll Journal Entry or the Payment Entry.
- **Payee onboarding.** Employees and suppliers get a Mercury invite to enter their own bank details, so no bank account numbers are stored in ERPNext. Payees you already pay in Mercury can be matched to their Employee or Supplier from the form or in bulk.
- **Alerts.** Sync, booking, invoicing and payout failures are e-mailed to the addresses in _Alert Recipients_.

<!-- frappe-nix:begin compatibility -->

## Compatibility

| Frappe | Branch       |
| ------ | ------------ |
| v16    | `version-16` |

| Requires                                       | Version            |
| ---------------------------------------------- | ------------------ |
| [erpnext](https://github.com/frappe/erpnext)   | `>=16.0.0,<17.0.0` |
| [payments](https://github.com/frappe/payments) | `>=0.0.1,<1.0.0`   |
| [hrms](https://github.com/frappe/hrms)         | `>=16.0.0,<17.0.0` |

<!-- frappe-nix:end compatibility -->

<!-- frappe-nix:begin installation -->

## Installation

On Frappe Cloud, install Mercury Banking for ERPNext from the [Frappe marketplace](https://cloud.frappe.io/marketplace/apps/mercury_banking_integration).

On a bench of your own:

```bash
bench get-app https://github.com/Avunu/mercury_banking_integration --branch version-16
bench --site <site> install-app mercury_banking_integration
```

<!-- frappe-nix:end installation -->

The app requires ERPNext, payments and HRMS on the same site.

## Configuration

Open **Mercury Settings**. A System Manager or Accounts Manager can edit it; **Register Webhook** and **Replay Events** are for a System Manager only.

1. Set the **Company** and an **API Token** (or tick **Use Sandbox** and set a **Sandbox API Token**), then tick **Enabled**. Saving checks the token against Mercury.
2. **Mercury → Sync Accounts** creates or adopts the Bank Accounts. The company needs a Bank group account in its chart of accounts.
3. **Mercury → Register Webhook** (production only; Mercury's sandbox has no webhooks) stores the endpoint id and signing secret and sends a test event. Without it the 15-minute poll still picks up every event.
4. **Mercury → Backfill Transactions** imports past transactions. Tick **Automatic Transaction Sync** for the hourly import. **Sync Start Date** limits the first import (12 months back when empty).
5. Turn on the parts you want:

| Section                     | Settings                                                                                                                                                                                                                                                       |
| --------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Auto Journal                | **Enable Auto Journal Entries**, **Require Attachment** (on by default), **Maximum Amount** (zero means no limit), **Daily Backfill (Days)** (90 by default; zero turns the sweep off), **Default Cost Center**, **Auto Reconcile Internal Transfers**         |
| Accounts Receivable Gateway | **Enable AR Gateway**, the **Destination Bank Account** that receives payments, an **AR Clearing Account**, **Invoice Email Sender** (ERP or Mercury), **Accept ACH Debit**, **Accept Credit Card** with a **Card Fees Account**, **Show Real Account Number** |
| Overdue Reminders           | **Send Overdue Reminders**, **Reminder Interval (Days)**, **Maximum Reminders**                                                                                                                                                                                |
| Payouts                     | **Enable Payouts**, **Payout Mode** (Request Approval or Direct Send), **Auto Reconcile Payouts**, the **Payroll** and **Vendor Funding Bank Account**                                                                                                         |
| Notifications               | **Alert Recipients**, comma-separated                                                                                                                                                                                                                          |

6. For automatic journal entries, use **Mercury → Export GL Codes** and upload the file on Mercury's [GL codes page](https://app.mercury.com/accounting/mapping/gl-codes). GL codes are read-only in Mercury's API, so repeat this after adding or renaming accounts. Account names that two accounts share, or that contain a comma, quote or line break, are left out and listed.
7. For payouts, use **Mercury → Send Mercury Invite** on each Employee or Supplier, or **Match Mercury Contact** for payees that already exist in Mercury. To match many at once:

   ```bash
   # a dry run by default; repeat with 'dry_run': False to write the matches
   bench --site <site> execute mercury_banking_integration.payouts.recipients.link_existing_recipients \
     --kwargs "{'party_type': 'Employee'}"
   ```

   A match is made only when the email (or else the name) pairs one record with one Mercury contact; anything else is reported and skipped.

Which Mercury token you need:

- A read-only token is enough for bank sync, automatic journal entries and invoice polling.
- Creating invoices needs a read-write or custom-scoped token.
- **Direct Send** needs a read-write token and the server's fixed outgoing IP address on Mercury's allow list. **Request Approval** needs neither, but a second Mercury user approves each payment in Mercury.

Moving an existing site from Plaid and GoCardless to this app is described step by step in the [cutover runbook](docs/cutover-runbook.md).

## How It Works

- **Mercury API client.** `mercury_banking_integration/client/` is a typed client for Mercury's API that does not import Frappe. It retries on 408, 429 and 5xx responses, honours `Retry-After`, follows cursor pagination and sends idempotency keys. `sync/client_factory.py` builds it from Mercury Settings.
- **One DocType.** The app adds only **Mercury Settings** (a Single). Everything else is stored in custom fields on standard DocTypes:

| DocType                    | Fields                                                                            | Used for                                                            |
| -------------------------- | --------------------------------------------------------------------------------- | ------------------------------------------------------------------- |
| Bank Account               | `mercury_account_id`                                                              | Which Mercury account it is. `integration_id` is left to Plaid.     |
| Bank Transaction           | the standard `transaction_id`                                                     | The Mercury transaction id, so a transaction is imported once.      |
| Customer                   | `mercury_customer_id`                                                             | The Mercury invoicing customer.                                     |
| Payment Request            | `mercury_invoice_id`, reminder count and date                                     | The Mercury invoice; the pay link is in the standard `payment_url`. |
| Employee, Supplier         | `mercury_recipient_id`, `mercury_recipient_status`, `mercury_invite_id`           | The Mercury payee.                                                  |
| Salary Slip, Payment Entry | `mercury_transaction_id`, `mercury_payment_status`, `mercury_approval_request_id` | The state of each payout.                                           |

- **Events.** The webhook (`/api/method/mercury_banking_integration.webhooks.webhook`) checks the `Mercury-Signature` HMAC, records the event and returns at once; a background job does the work. The poller and _Replay Events_ record events the same way, and an event id that is already recorded is skipped. Handlers always fetch the transaction from Mercury again, so duplicate or out-of-order events end in the same state.
- **Logs.** Each inbound event and each payout attempt is an Integration Request with the service "Mercury". Inbound ones have `is_remote_request` set, the event id in `request_id` and the channel (Webhook, Poll or Replay) in `request_description`. Log Settings controls how long they are kept.
- **Bookkeeping.** Journal Entries carry the Mercury transaction or invoice id in `cheque_no`, which is how the app knows a transaction is already booked. Reconciliation uses ERPNext's own `reconcile_vouchers`, so unreconciling in ERPNext works as usual.
- **Scheduled jobs.** Every 15 minutes: poll events, invoices and approval requests. Hourly: retry failed invoice creation and sync transactions. Daily: the auto-journal sweep, a webhook health check that re-enables a webhook Mercury turned off, overdue reminders, completed invites and payouts still waiting for their Bank Transaction.

## Limitations

- **GL coding sends no event.** Mercury's transaction events do not cover GL codes, and the hourly sync re-reads only the last 3 days. A transaction coded later is booked by the daily sweep, which costs one API call per unreconciled transaction in its window. To sweep on demand: `bench execute mercury_banking_integration.sync.gl_codes.reevaluate_unreconciled --kwargs "{'dry_run': False}"` (a dry run by default, and with no date limit unless you pass `from_date`). The Journal Entry is dated on the transaction's date, so a long backlog can post into a closed period.
- **Attachments are copied only with an automatic journal entry.** A transaction that is not booked by the app keeps its attachments in Mercury.
- **US dollars only.** The invoicing gateway accepts only USD, and automatic journal entries post amounts without exchange rates.
- **No ACH pulls.** Mercury cannot debit a customer's account, so the customer pays from the pay link (with reminders when overdue).
- **Mercury's 24-hour duplicate rule.** Mercury refuses a payment to the same payee, from the same account, for the same amount within 24 hours, whatever the idempotency key. The payroll preview warns about it. A supplier Payment Entry that Mercury blocked this way can be sent again with _Pay via Mercury_ once 24 hours have passed since the last attempt.
- **Card payments are matched by estimate.** Card payouts arrive with fees taken off, and the deposit is matched to its invoice within an estimated fee range. Leave **Accept Credit Card** off until you have checked a real settlement.
- **Export GL Codes** also shows an "Upload the GL codes" message with a link, even when Mercury's upload page did open in a new tab.

<!-- frappe-nix:begin support -->

## Support

- Bug reports and questions: [GitHub issues](https://github.com/Avunu/mercury_banking_integration/issues)
- E-mail: <mail@avu.nu>

<!-- frappe-nix:end support -->

<!-- frappe-nix:begin development -->

## Development

```bash
nix develop        # or: direnv allow
prek install       # the git hooks CI runs
frappe-test        # the tests, the way CI runs them
```

The managed files, checks and release flow are described in the [app standards documentation](https://docs.avunu.net/frappe-nix/docs/app-standards/).

<!-- frappe-nix:end development -->

The test suite, the API client and the Plaid and GoCardless migration helpers are described in [docs/development.md](docs/development.md).

<!-- frappe-nix:begin license -->

## License

MIT, Copyright (c) 2026–present Avunu LLC → [license.txt](license.txt)

<!-- frappe-nix:end license -->
