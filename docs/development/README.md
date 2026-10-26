---
title: Develop Mercury Banking for ERPNext
description: The dev shell, the tests and checks, CI and the release flow of this repository, and the one-off migration helpers.
nav_title: Development
order: 9
updated: 2026-10-26
---

This page is for people changing the app. [How it works](architecture.md) explains how the code is put together. The shared rules every Avunu app follows (managed files, checks, release flow) are in the frappe-nix [app standards documentation](https://docs.avunu.net/frappe-nix/docs/app-standards/), and this page does not repeat them.

## Set up

The app is built with [frappe-nix](https://github.com/Avunu/frappe-nix). From the repository root:

```bash
nix develop --no-pure-eval
prek install
frappe-test
```

`direnv allow` gives the same shell through `.envrc`. The shell provides a bench with Frappe, ERPNext, payments and HRMS checked out under `.frappe-nix/bench`, and a development site. `prek install` installs the git hooks CI runs, and `frappe-test` runs the tests the way CI does.

## Commands

| Command                             | What it does                                                                                                                                                        |
| ----------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `frappe-test`                       | The Python tests with the coverage gate (`fail_under` in `pyproject.toml`, 97%), then the testmap and composition checks; `--ci` runs exactly what `ci / test` runs |
| `frappe-test --reuse-site`          | The same tests against the dev shell's site, without rebuilding it                                                                                                  |
| `prek run --all-files`              | Every pre-commit hook: ruff, ssort, oxlint, oxfmt, Frappe's checks and the rest                                                                                     |
| `yarn check`                        | `oxfmt --check`, `oxlint` and `tsc --build` over the form scripts                                                                                                   |
| `yarn lint:py`, `yarn typecheck:py` | ruff and ty over the Python code, pinned in `tools/uv.lock`                                                                                                         |
| `frappe-demo`                       | Loads the demo data from `mercury_banking_integration/demo.py`                                                                                                      |

The form scripts in `mercury_banking_integration/public/js/` are plain JavaScript with JSDoc types, checked by TypeScript against [frappe-types](https://github.com/Avunu/frappe-types). There is no bundle to build.

## Tests

The tests are in `mercury_banking_integration/tests/`. None of them reaches Mercury: every HTTP call is answered in-process with `responses` by the `MercuryAPI` fixture in `tests/utils.py`, so a request with no registered answer fails instead of reaching the bank.

| File                        | Covers                                                                                           |
| --------------------------- | ------------------------------------------------------------------------------------------------ |
| `test_client.py`            | The API client: retries, pagination, errors, models (no site needed)                             |
| `test_webhook_signature.py` | The `Mercury-Signature` check (no site needed)                                                   |
| `test_gl_codes.py`          | GL code extraction, export eligibility, allocation balancing and party resolution                |
| `test_settings.py`          | Token validation, the settings buttons, the client factory and the scheduler gates               |
| `test_sync.py`              | Accounts, transactions, events and the webhook                                                   |
| `test_auto_journal.py`      | GL-coded transactions booked as Journal Entries, and the GL code export                          |
| `test_reconcile.py`         | Payout Bank Transactions and internal transfers                                                  |
| `test_ar.py`                | The invoicing flow end to end, from Payment Request to funding Journal Entry                     |
| `test_payouts.py`           | Recipients, payroll and supplier payouts, both modes, idempotency replays and the duplicate rule |
| `test_migrate.py`           | The migration helpers below                                                                      |
| `test_demo.py`              | The demo hook                                                                                    |

## Continuous integration and releases

`.github/workflows/ci.yml` is managed by frappe-nix and runs on every pull request, every push to `main` and once a day, on the self-hosted runners. Its gates are `ci / lint` (the pre-commit hooks), `ci / typecheck`, `ci / test` (`frappe-test` with coverage) and `ci / marketplace` (the checks of `marketplace/listing.toml`). `pr-policy.yml` checks a pull request's base, title and commits, `nightly.yml` runs the integration tests, the screenshots, the link check and the duplication check each night, and `deps.yml` relocks and auto-merges dependency pull requests, with a daily sweep and a weekly Python relock.

`release.yml` runs release-please. Merging conventional commits to `main` opens a release pull request that bumps the version in `package.json` and `mercury_banking_integration/__init__.py` and writes `CHANGELOG.md`; merging that publishes the GitHub release.

`docs.yml` checks this documentation with Avunu/site-docs when `docs/` changes. To run the same check locally, clone [Avunu/site-docs](https://github.com/Avunu/site-docs) and, from its folder (Node 24), run:

```bash
npm ci --ignore-scripts && npm run build
CI=true node build/cli.js build --only mercury-banking-integration --source mercury-banking-integration=<PATH_TO_THIS_REPOSITORY>
```

The screenshots in `docs/screenshots/` are generated from `marketplace/screenshots.ts` over the demo data. Do not edit them by hand.

## Migration helpers

`mercury_banking_integration/migrate.py` holds one-off helpers for moving a site that used GoCardless for invoicing and Plaid for bank feeds to this app. They are run with `bench --site <SITE_NAME> execute`. None of them commits on its own (`bench execute` commits when the function returns), and the ones that change data are dry runs unless you pass `dry_run` false. The order that works:

1. **Prepare.** Install the app, create the AR clearing account, and fill in **Mercury Settings**. The Mercury Payment Gateway Account is created with **Is Default** off, so nothing changes for existing Payment Requests.
2. **Payroll first.** Onboard the employees and pay one Payroll Entry with **Pay via Mercury** in `Request Approval` mode, then check that the Bank Transactions reconcile against the payroll Journal Entry. This does not touch GoCardless.
3. **Invoicing pilot.** Move one or two Subscription Plans to the Mercury gateway account and follow a full cycle, from Payment Request to funding Journal Entry:

   ```bash
   bench --site <SITE_NAME> execute mercury_banking_integration.migrate.repoint_subscription_plans --kwargs '{"from_gateway_account": "<OLD_GATEWAY_ACCOUNT>", "to_gateway_account": "<MERCURY_GATEWAY_ACCOUNT>", "dry_run": false}'
   ```

4. **Invoicing cutover.** Tell your customers, make the Mercury gateway account the default with `migrate.set_default_gateway_account` (the default is global: anything that creates Payment Requests without naming a gateway follows it), and move the remaining Subscription Plans. Keep the old gateway's webhooks live long enough for payments and chargebacks in flight.
5. **Bank feed cutover**, at a chosen moment:
   1. Run a last Plaid sync, then `migrate.snapshot_plaid_integration_ids`, which records each Bank Account's Plaid **Integration ID** in a comment on Plaid Settings for rollback.
   2. Run **Sync Accounts**. It adopts the Plaid Bank Accounts by account number.
   3. Turn off Plaid's automatic sync for those accounts.
   4. Backfill from the cutover date, and list the overlap with `migrate.find_duplicate_bank_transactions --kwargs '{"around": "<CUTOVER_DATE>"}'`.
   5. Merge the duplicates with `migrate.merge_plaid_mercury_duplicates`. A dry run first writes a CSV report as a private File. Where the Plaid copy is reconciled it survives and takes on the Mercury identity; where neither is, the Mercury copy survives; where both are, nothing is merged and the pair is reported.

`sync.transfers.reconcile_existing_transfers` books internal transfers that were synced before **Auto Reconcile Internal Transfers** was on: see [Automatic journal entries](../auto-journal.md#internal-transfers).
