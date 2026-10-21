# Development

Notes for working on Mercury Banking for ERPNext. Setting up and using the app is covered in the [README](../README.md).

## Dev shell and checks

The app is built with [frappe-nix](https://github.com/Avunu/frappe-nix):

```bash
nix develop        # or: direnv allow
prek install       # the git hooks CI runs (ruff, ssort, oxlint, oxfmt and the rest)
frappe-test        # the tests, the way CI runs them, with the coverage gate
```

`frappe-test --reuse-site` runs against the dev shell's site without rebuilding it.

## Source layout

| Path                      | What                                                                                                                                |
| ------------------------- | ----------------------------------------------------------------------------------------------------------------------------------- |
| `client/`                 | The Mercury API client. It does not import Frappe; `sync/client_factory.py` is the only place that builds it from Mercury Settings. |
| `sync/`                   | Accounts, transactions, events (webhook, poll, replay), automatic journal entries, internal transfers and GL codes.                 |
| `ar/`                     | The payment gateway, Mercury invoices, invoicing customers and the funding Journal Entry.                                           |
| `payouts/`                | Recipients and invites, payroll and supplier payouts, the send path shared by both, and reconciliation.                             |
| `webhooks.py`, `tasks.py` | The webhook endpoint and the scheduler entry points listed in `hooks.py`.                                                           |
| `migrate.py`              | One-off cutover helpers, run with `bench execute` (see below).                                                                      |
| `mercury_integration/`    | The module: the Mercury Settings DocType and the custom fields on standard DocTypes.                                                |
| `public/js/`              | Form scripts for Payroll Entry, Payment Entry, Payment Request, Employee and Supplier.                                              |
| `demo.py`                 | The demo data that `frappe-demo` and the screenshots use.                                                                           |

## The API client

`client/` mirrors Mercury's official Go SDK (mercury-go): Pydantic v2 models, retries on 408, 429 and 5xx with `Retry-After` honoured, cursor pagination, enum values read as plain strings so a new value never breaks parsing, and idempotency keys sent in the request body. Mercury signs amounts with negative meaning money out, the opposite of Plaid.

The tests that need no site (`tests/test_client.py`, `tests/test_webhook_signature.py`) use `UnitTestCase`; the rest are integration tests. Every test answers Mercury's HTTP calls in-process with `responses` (`tests/utils.py`), so a request with no registered answer fails instead of reaching the bank.

## Design notes

- **Logs.** Inbound events and outbound payout attempts are Integration Requests (service "Mercury"). Frappe's Webhook Request Log is not used: core writes it only for its own outbound Webhook DocType, and its fields describe an outgoing delivery.
- **Payout state.** Each payout's idempotency key is `<document name>:<attempt>`, where the attempt number counts earlier failed Integration Requests for that document. A 409 from Mercury is an idempotency replay, and the original transaction is adopted from the error body. A 400 that mentions a duplicate sets the status to `Blocked - Duplicate`.
- **Payroll.** Payouts are never sent on submit; someone presses _Pay via Mercury_. Each employee's ACH payment lands as its own Bank Transaction and is reconciled against the single bank Journal Entry from `Payroll Entry.make_bank_entry`, which clears once every payment has landed. The amount sent is `net_pay` to the cent, never `rounded_total`.
- **Invoices.** Mercury has no invoice webhooks, so invoice status is polled every 15 minutes. The settlement Payment Entry debits the AR clearing account (the Payment Gateway Account's payment account), and the funding Journal Entry moves the money from clearing to the bank when the deposit's Bank Transaction is submitted. A deposit that matches more than one paid invoice is never guessed; an alert asks for manual reconciliation.
- **Internal transfers.** Mercury does not link the two sides of a transfer. Each side's counterparty name ends in the other account's last four digits, which picks the counter account; the counter side is then the one unreconciled transfer there with the opposite direction, the same amount and a date within three days.

## Cutover helpers

`migrate.py` holds the one-off steps for moving a site from GoCardless and Plaid to Mercury. None of them commits on its own (`bench execute` commits when the function returns), and the destructive ones are dry runs by default:

- `repoint_subscription_plans` and `set_default_gateway_account` move invoicing from the GoCardless gateway account to Mercury's.
- `snapshot_plaid_integration_ids`, `find_duplicate_bank_transactions` and `merge_plaid_mercury_duplicates` handle the overlap between Plaid's and Mercury's Bank Transactions.
- `sync.transfers.reconcile_existing_transfers` books internal transfers that were synced before that feature existed.

The order to run them in is in the [cutover runbook](cutover-runbook.md).
