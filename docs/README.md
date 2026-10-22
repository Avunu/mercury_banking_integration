# Mercury Banking for ERPNext

Mercury bank sync, ACH payouts, invoicing and GL coding for ERPNext

This app connects ERPNext to [Mercury](https://mercury.com). Mercury transactions arrive as Bank Transactions and are reconciled, GL-coded transactions are booked as Journal Entries, Payment Requests become Mercury invoices with a pay link, and payroll and supplier payments go out by ACH from ERPNext. Each part has its own switch in **Mercury Settings**.

## What you get

- **Bank sync:** Mercury accounts become Bank Accounts, with an hourly import of transactions, a backfill button, and a webhook plus a 15-minute poll for changes.
- **Automatic journal entries:** a transaction with a Mercury GL code is booked against the account of the same name and reconciled, splits included, with its attachments. Transfers between two Mercury accounts become one Bank Entry.
- **Invoicing:** Mercury is a payment gateway for Payment Requests. Paid invoices become Payment Entries, the deposit is booked from a clearing account, and overdue customers get reminders.
- **Payouts:** _Pay via Mercury_ on a Payroll Entry or a supplier Payment Entry sends ACH payments, at once or for approval in Mercury, and reconciles them when they land.
- **Payee onboarding:** Mercury invites, so bank details never enter ERPNext, or a match to payees you already have in Mercury.
- **Audit trail:** every inbound event and payout attempt is an Integration Request.

## Installation

```bash
bench get-app https://github.com/Avunu/mercury_banking_integration --branch version-16
bench --site <site> install-app mercury_banking_integration
```

ERPNext, payments and HRMS must be installed on the site. Setup, the token you need and the known limits are in the [README](https://github.com/Avunu/mercury_banking_integration#readme).

## More

- [Cutover runbook](cutover-runbook.md): moving a site from Plaid and GoCardless to Mercury, in stages.
- [Development](development.md): the dev shell, the source layout, the API client and the design notes.

## License

[MIT](https://github.com/Avunu/mercury_banking_integration/blob/main/license.txt), Copyright (c) 2026–present Avunu LLC.
