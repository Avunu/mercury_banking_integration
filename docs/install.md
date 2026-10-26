---
title: Install and upgrade
description: Install Mercury Banking for ERPNext on a Frappe v16 bench, see what the install adds, and upgrade or remove it.
nav_title: Install
order: 1
updated: 2026-10-26
---

Mercury Banking for ERPNext installs like any Frappe app. It has no install hook of its own: installing it adds one settings page and a set of custom fields, and nothing talks to Mercury until you fill in **Mercury Settings** and tick **Enabled**.

## Before you start

- A bench with Frappe v16 and a site that already has ERPNext, payments and HRMS installed. The app lists all three as required apps.
- A System Manager login on the site.
- A Mercury API token. You can start with a sandbox token: see [Configuration](configuration.md#mercury-api-tokens).
- For the webhook, a site that Mercury can reach over HTTPS. Without one, the 15-minute poll still picks up every event.

## Install

Run these from the bench directory. `<SITE_NAME>` is your site, for example `erp.example.com`.

```bash
bench get-app https://github.com/Avunu/mercury_banking_integration --branch version-16
bench --site <SITE_NAME> install-app mercury_banking_integration
```

The app has no front-end build of its own: its form scripts are plain JavaScript files that Frappe serves as they are.

## What installing sets up

| What                   | Detail                                                                                                                                                                                                                      |
| ---------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Mercury Settings**   | A single settings DocType in the **Mercury Integration** module, with every switch off. System Manager has full access and Accounts Manager can read and write it.                                                          |
| Custom fields          | Read-only fields that hold Mercury ids and payout state on Bank Account, Customer, Employee, Supplier, Payment Request, Payment Entry and Salary Slip. The full list is in [Configuration](configuration.md#custom-fields). |
| Form scripts and hooks | The **Mercury** buttons on Payroll Entry, Payment Entry, Payment Request, Employee and Supplier, and the document events and scheduled jobs listed in [Configuration](configuration.md#what-the-app-adds-to-your-site).     |
| Webhook endpoint       | `/api/method/mercury_banking_integration.webhooks.webhook` answers POST requests that carry a valid Mercury signature. It does nothing until you register the webhook.                                                      |

Installing creates no Bank, Bank Account, Account, Payment Gateway or other record. Those are created later, by the actions that need them:

- **Sync Accounts** creates the `Mercury` Bank, a Bank Account for each Mercury account and, when needed, its GL account under the company's Bank group account. See [Bank sync](bank-sync.md#connect-your-mercury-accounts).
- Saving **Mercury Settings** with **Enable AR Gateway** ticked creates the `Mercury` Payment Gateway and its Payment Gateway Account. See [Invoicing](invoicing.md#set-up-the-gateway).

The scheduled jobs are installed with the app but return at once while **Enabled** is unticked.

## Upgrade

Update the app the way you update any Frappe app: `bench update`, or pull the new code in `apps/mercury_banking_integration` and run:

```bash
bench --site <SITE_NAME> migrate
```

`bench migrate` re-syncs the custom fields and runs any patch the site has not run yet. The app ships two:

| Patch                                                                     | What it changes                                                                                                                                                                                                                    |
| ------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `mercury_banking_integration.patches.v1_1.remove_category_sync`           | Deletes the `mercury_category_id` custom field from Account, left over from the Mercury category sync that GL codes replaced. Categories already created in Mercury stay there; delete them in Mercury if you no longer want them. |
| `mercury_banking_integration.patches.v1_2.set_auto_journal_backfill_days` | Sets **Daily Backfill (Days)** to 90 on a site that installed the app before the setting existed, so the daily sweep is on. A value you have saved is left alone.                                                                  |

The app was once called `mercury_integration`. Its whitelisted methods still answer under the old dotted paths (for example `mercury_integration.webhooks.webhook`), so a webhook or a cached page that uses an old path keeps working after the rename.

## Uninstall

```bash
bench --site <SITE_NAME> uninstall-app mercury_banking_integration
```

Frappe takes a backup first, then deletes what belongs to the app's **Mercury Integration** module: the **Mercury Settings** DocType with its values (including the stored API tokens and the webhook secret) and the custom fields listed in [Configuration](configuration.md#custom-fields). The scheduled jobs, document events and form buttons stop with it.

These stay on the site, because they are ordinary ERPNext records:

- The `Mercury` Bank, the Bank Accounts and their GL accounts.
- Every Bank Transaction, Journal Entry and Payment Entry the app created, and their reconciliations.
- The Integration Requests that log Mercury events and payout attempts.
- The `Mercury` Payment Gateway and its Payment Gateway Account. Its settings DocType is gone, so take the gateway out of use (and off any Subscription Plan) before you uninstall.

Uninstalling changes nothing in Mercury. Delete the webhook endpoint in Mercury's dashboard if you registered one; Mercury disables an endpoint by itself after repeated delivery failures.

## Next

- [Configuration](configuration.md) describes every setting.
- [Bank sync](bank-sync.md) walks through the first sync.
