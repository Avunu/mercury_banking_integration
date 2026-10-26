---
title: Automatic journal entries
description: Export your accounts as Mercury GL codes, let coded transactions book themselves as reconciled Journal Entries, and reconcile internal transfers.
nav_title: Auto journal
order: 4
updated: 2026-10-26
---

In Mercury you give each transaction a GL code before you mark it ready. When those GL codes are the names of your ERPNext accounts, the app can book the transaction for you: it creates the Journal Entry, attaches the receipts, and reconciles it with the Bank Transaction. Transfers between two of your Mercury accounts are booked the same way, without a GL code.

## Before you start

- Bank sync works: see [Bank sync](bank-sync.md).
- **Enable Auto Journal Entries** is ticked in **Mercury Settings**, and the other fields of the **Auto Journal** section are set the way you want: see [Configuration](configuration.md#auto-journal).
- Your accounts are uploaded to Mercury as GL codes, as below.

## Export your accounts as GL codes

Mercury's API cannot create GL codes, so you upload them in Mercury.

1. In **Mercury Settings**, choose **Mercury → Export GL Codes**.
2. The browser downloads a CSV with one account name per line and no header, the format Mercury's upload takes. Every enabled, non-group Liability, Income, Expense and Equity account of the **Company** is in it. Asset accounts are left out, because the bank side of each entry is the Bank Account itself.
3. A new tab opens Mercury's [GL codes page](https://app.mercury.com/accounting/mapping/gl-codes). Upload the file there. If the browser blocked the tab, a message gives the link instead.

An account name that two accounts share, or that contains a comma, a quotation mark or a line break, cannot match a GL code exactly. Those names are left out of the file and listed under **Some accounts cannot be exported**; rename the accounts to include them.

Export and upload again after you add or rename accounts.

## What gets booked

A synced transaction is booked when all of these hold:

- It has posted, and it is not an internal transfer.
- It has an attachment in Mercury, unless **Require Attachment** is unticked.
- Its Bank Transaction is submitted and not yet reconciled, and no Journal Entry has been booked for it before.
- Every GL code on it (one per split) matches the **Account Name** of exactly one enabled, non-group Liability, Income, Expense or Equity account of the **Company**.
- The split amounts add up to the transaction amount, so every cent is coded.
- Its amount is not above **Maximum Amount** (zero means no limit).

The Journal Entry has one line per GL code and one line on the bank's GL account, is dated on the transaction's posting date, and carries the Mercury transaction id in **Reference Number** (`cheque_no`). Income and expense lines get the **Default Cost Center**, or the company's default cost center when that is not a group. The entry is submitted and reconciled with the Bank Transaction through ERPNext's own reconciliation, so unreconciling it in ERPNext cancels it as usual.

A GL code on a Payable or Receivable account (Payroll Payable, for example) needs a party. The app takes it from the Mercury counterparty: the Employee or Supplier whose **Mercury Recipient ID** is that counterparty. See [Payouts](payouts.md#onboard-payees).

The transaction's attachments, up to 32 MB each, are copied to the Bank Transaction and to the Journal Entry as private files.

When a transaction is coded but cannot be booked (a GL code with no matching account or with several, a Payable code with no linked party, splits that do not add up, an Asset account), the app leaves it and emails an alert with the reason. Fix the cause and the next sweep books it.

## Transactions coded later

Mercury sends no event when you add a GL code, and the hourly sync only re-reads the last 3 days. So once a day the app re-checks every unreconciled Mercury transaction from the last **Daily Backfill (Days)** days (90 by default) and books the ones that have become ready. Transactions that are coded but blocked are listed in one alert per day. Each transaction checked costs one call to Mercury.

To sweep on demand, or further back, run this from the bench directory. It is a dry run that lists what it would book; pass `"dry_run": false` to book, and `"from_date"` to limit how far back it looks (no limit by default):

```bash
bench --site <SITE_NAME> execute mercury_banking_integration.sync.gl_codes.reevaluate_unreconciled --kwargs '{"dry_run": false, "from_date": "2026-01-01"}'
```

> [!WARNING]
> The Journal Entry is dated on the transaction's own date. A long backlog can therefore post into a period you have already closed.

## Internal transfers

A transfer between two Mercury accounts arrives as two Bank Transactions, a withdrawal on one account and a deposit on the other. With **Auto Reconcile Internal Transfers** ticked (the default), the app books one Journal Entry with the **Entry Type** Bank Entry, crediting the source bank's GL account and debiting the destination's, and reconciles it with both Bank Transactions. The entry is dated on the withdrawal's date and carries the withdrawal's Mercury id as its reference.

Mercury does not link the two sides. The app reads the last four digits at the end of each side's counterparty name, which Mercury sets to the other account, to find the counter account, then looks there for the one unreconciled transfer in the opposite direction, of the same amount, within 3 days. The first side to sync waits; the second books the pair. If more than one candidate fits, nothing is booked and an alert asks you to reconcile it by hand.

This works whether or not **Enable Auto Journal Entries** is ticked.

To book transfers that were synced before this setting was on, run the backfill. It is a dry run unless you pass `"dry_run": false`:

```bash
bench --site <SITE_NAME> execute mercury_banking_integration.sync.transfers.reconcile_existing_transfers --kwargs '{"dry_run": false}'
```

## Related

- [Configuration](configuration.md#auto-journal) for the settings.
- [Troubleshooting](troubleshooting.md) for the alerts and their fixes.
