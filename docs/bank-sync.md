---
title: Bank sync
description: Connect your Mercury accounts to ERPNext Bank Accounts, import their transactions, and keep them current with the webhook, the poll and replays.
nav_title: Bank sync
order: 3
updated: 2026-10-26
---

Bank sync brings Mercury into ERPNext's own banking documents. Each Mercury account is a Bank Account, and each Mercury transaction is a Bank Transaction that you reconcile in the Bank Reconciliation Tool as usual, or that the app reconciles for you (see [Automatic journal entries](auto-journal.md)).

## Before you start

- **Mercury Settings** has a **Company** and a token, and **Enabled** is ticked and saved. See [Configuration](configuration.md#set-up-in-order).
- The company's chart of accounts has a Bank group account (an Asset group with the account type Bank). New Mercury accounts get their GL account under it.

## Connect your Mercury accounts

In **Mercury Settings**, choose **Mercury → Sync Accounts**. The app lists your Mercury accounts and, for each active one:

1. Skips it when a Bank Account already carries its **Mercury Account ID**.
2. Otherwise adopts an existing Bank Account that has the same account number (the last four digits or more are enough, for numbers an earlier integration stored masked), or the same name under the `Mercury` bank. Adopting writes the **Mercury Account ID**, corrects the account and routing numbers from Mercury, and re-enables the Bank Account.
3. Otherwise creates a Bank Account named after the account's nickname, under a `Mercury` Bank (created the first time), with a GL account of the same name under the company's Bank group account (an existing Bank ledger account of that name is reused). When two accounts share a nickname, the second gets the last four digits of its number added.

A Mercury account that is archived or deleted is never adopted or created; if a Bank Account is still linked to it, that Bank Account is disabled. A message then says how many Bank Accounts were created, adopted, already linked and disabled, and names the ones adopted.

Run **Sync Accounts** again whenever you open or close an account in Mercury.

> [!TIP]
> If you used Plaid before, Sync Accounts adopts the Plaid Bank Accounts by account number instead of creating duplicates. It writes its own field and leaves Plaid's **Integration ID** alone.

## Import transactions

Tick **Automatic Transaction Sync** to import each Mercury Bank Account's transactions every hour. The first sync of an account reaches back to **Sync Start Date**, or 12 months when that is empty. Each later sync starts 3 days before the newest transaction already imported, so changes Mercury makes in those days are picked up.

To import a stretch of history at any time, choose **Mercury → Backfill Transactions**, enter the number of **Days** (90 by default) and confirm. The import runs in the background, one job per account.

Each posted Mercury transaction becomes a submitted Bank Transaction:

| Bank Transaction field                         | From Mercury                                                                            |
| ---------------------------------------------- | --------------------------------------------------------------------------------------- |
| **Date**                                       | The posting date, or the creation date while it has none.                               |
| **Deposit**, **Withdrawal**                    | The amount. Mercury writes money out as a negative amount.                              |
| **Currency**                                   | USD.                                                                                    |
| **Transaction ID**                             | The Mercury transaction id. A transaction is imported once, however often it is synced. |
| **Reference Number**                           | The check number, without Mercury's leading zeros.                                      |
| **Transaction Type**                           | Mercury's kind, such as `internalTransfer`.                                             |
| **Description**                                | The counterparty, then Mercury's bank description, note or memo.                        |
| **Party Name/Account Holder (Bank Statement)** | The counterparty name.                                                                  |

Pending transactions are skipped until they post, unless **Create Pending Transactions** is ticked. Then each pending transaction is a draft Bank Transaction, updated while it is pending, submitted when it posts and deleted if Mercury cancels it or it fails.

Transactions on a Bank Account that is disabled, or that has no **Mercury Account ID**, are not imported.

## When Mercury changes a transaction

Mercury can change a transaction after it posted. The next sync or event applies the change to the submitted Bank Transaction:

| Change in Mercury                      | Bank Transaction not yet reconciled                | Bank Transaction already reconciled                |
| -------------------------------------- | -------------------------------------------------- | -------------------------------------------------- |
| A new posting date                     | The date is updated.                               | The date is updated.                               |
| A new amount                           | Cancelled, and imported again with the new amount. | Left alone; an alert asks you to amend it by hand. |
| Cancelled, failed, reversed or blocked | Cancelled.                                         | Left alone; an alert asks you to amend it by hand. |

## Live updates: webhook, poll and replay

Three channels feed the same handler, which always fetches the transaction from Mercury again before it writes anything. An event that arrives twice, or out of order, ends in the same state.

- **Webhook.** Choose **Mercury → Register Webhook** to create an endpoint in Mercury that points at your site, store its signing secret and send a test event. Registering again replaces the old endpoint. Mercury's sandbox has no webhooks, and Mercury needs to reach your site over the internet. If the test event fails, an alert says so.
- **Poll.** Every 15 minutes the app reads Mercury's events API from where it last stopped, up to 100 events per run, and queues more runs until it has caught up. This catches anything the webhook missed, and is the only channel in the sandbox.
- **Replay.** Choose **Mercury → Replay Events** and enter the number of **Hours** (24 by default) to run the events of that period through the handler again, including ones already processed.

Once a day the app checks the webhook in Mercury. If Mercury turned it off after failed deliveries, the app turns it back on and sends an alert; **Webhook Status** shows the current state.

## The event log

Every event is recorded once as an **Integration Request** with the **Service** `Mercury`, the event id as **Request ID**, and the channel (`Webhook`, `Poll` or `Replay`) as **Request Description**. Its status is `Queued` until the handler runs, then `Completed` (linked to the Bank Transaction it touched) or `Failed` with the error. Failures also go to the Error Log. Log Settings controls how long Integration Requests are kept: 90 days unless you change it.

Payout attempts are logged the same way, with the description `Payout`: see [Payouts](payouts.md#payout-statuses).

## Related

- [Automatic journal entries](auto-journal.md) books and reconciles the transactions you GL-code in Mercury.
- [Troubleshooting](troubleshooting.md) lists the errors bank sync can raise.
