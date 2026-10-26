---
title: Invoicing
description: Bill customers through Mercury from ERPNext Payment Requests, follow the invoice to payment, book the deposit, and send overdue reminders.
nav_title: Invoicing
order: 5
updated: 2026-10-26
---

With the accounts receivable gateway on, Mercury is a payment gateway in ERPNext, next to the ones the payments app provides. You keep raising Payment Requests in ERPNext; the app creates a Mercury invoice for each one, gives the customer Mercury's pay page, and books the money in two steps: a Payment Entry against a clearing account when the invoice is paid, and a Journal Entry from clearing to the bank when the deposit arrives.

Mercury cannot pull money from a customer's account, so every payment is made by the customer on the pay page. Reminders follow up on the ones that are late.

## Set up the gateway

1. Run **Sync Accounts** so that the Bank Account that should receive payments has a **Mercury Account ID**. See [Bank sync](bank-sync.md#connect-your-mercury-accounts).
2. Create a clearing account for money that Mercury has collected but not yet paid out, for example `Mercury AR Clearing` under Current Assets.
3. In **Mercury Settings**, tick **Enable AR Gateway** and set the **Destination Bank Account** and the **AR Clearing Account**. Choose the **Invoice Email Sender**, and which payment methods the pay page offers with **Accept ACH Debit** and **Accept Credit Card**. If you accept cards, set a **Card Fees Account**.
4. Save. The app creates the `Mercury` Payment Gateway and a Payment Gateway Account for it, with the clearing account as its payment account and the **Payment Channel** Email. ERPNext names it after the gateway, the currency and the company abbreviation, for example `Mercury - USD - EX`.

The new Payment Gateway Account is not made the default, so nothing changes until you choose it. Saving the settings again with another clearing account updates the existing Payment Gateway Account.

## Bill a customer

1. Make a Payment Request (Inward) for a Customer, in USD, for example from a Sales Invoice or a Sales Order.
2. Choose the Mercury account in **Payment Gateway Account**, check **To** (the customer's email address) and **Subject**, and submit.

On submit the app:

- Creates the customer in Mercury the first time, with the customer's name and the Payment Request's **To** address (or the Customer's email when **To** is empty), and stores its id in **Mercury Customer ID**.
- Creates the Mercury invoice: dated today, due on the Payment Request's date or today if that has passed, with one line that carries the **Subject** and the grand total. The invoice number is the Sales Invoice's name when the request is for a Sales Invoice (followed by the Payment Request's name if another request for that invoice already has a Mercury invoice), or else the Payment Request's name. The **Message**, without its formatting, goes to the customer as the invoice memo.
- Stores the invoice in **Mercury Invoice ID** and the pay page in ERPNext's **Payment URL**, and adds a comment.

If Mercury refuses the invoice, the Payment Request is still submitted, without a pay link. An alert says so, and an hourly job keeps trying; when it succeeds and the **Invoice Email Sender** is `ERP`, it sends the payment email and comments on the request. A System Manager or Accounts Manager can also try at once with **Mercury → Recreate Mercury Invoice** on the Payment Request, which appears while it has no Mercury invoice.

### Who emails the customer

**Invoice Email Sender** decides who sends the invoice:

- `ERP` (the default): ERPNext's own Payment Request email, with the Mercury pay link.
- `Mercury`: the app asks Mercury to email its invoice.

> [!IMPORTANT]
> ERPNext 16.51 sends its Payment Request email on submit whatever the gateway answers, unless the request was made with its email muted (which code that calls `make_payment_request` can do; the desk form cannot). With `Mercury` selected, a Payment Request submitted from the desk therefore gets both Mercury's email and ERPNext's. If the invoice could not be created, ERPNext's email goes out without a pay link.

## From payment to bank

Every 15 minutes the app reads the status of each open Mercury invoice:

| Mercury invoice status | What happens in ERPNext                                                                                                                                                                                                                                                                                               |
| ---------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Processing             | The Payment Request's status becomes **Initiated**, with a comment.                                                                                                                                                                                                                                                   |
| Paid                   | A Payment Entry is made from the Payment Request's reference document (as ERPNext's own **Create Payment Entry** does), paid into the clearing account, with the Mercury invoice id as **Cheque/Reference No**. It is submitted, ERPNext updates the Payment Request's status from it, and a comment names the entry. |
| Cancelled (in Mercury) | A comment and an alert. The Payment Request is cancelled unless a Payment Entry already exists for the invoice.                                                                                                                                                                                                       |

The Payment Entry's amount is the invoice amount, capped at what is still outstanding on the Payment Request, so a repeated poll cannot collect twice.

When the deposit reaches the **Destination Bank Account**, its Bank Transaction is submitted by the sync and the app looks for the paid invoice it belongs to: a paid Mercury invoice with no funding entry yet, whose Payment Entry is dated from 7 days before to 1 day after the deposit, and whose amount equals the deposit. With exactly one match, it books a Journal Entry (Bank Entry) that debits the bank, credits the clearing account and carries the invoice id as **Reference Number**, and reconciles it with the deposit.

### Card payments

Mercury pays card payments out with the card fee taken off. When no invoice matches the deposit exactly and **Accept Credit Card** is ticked, the app also accepts an invoice whose amount is higher than the deposit by no more than 1.25 times an estimated fee of 2.9% plus $0.30. The difference is debited to the **Card Fees Account**. Without a **Card Fees Account**, nothing is booked and an alert says so.

This match is an estimate. Leave **Accept Credit Card** off until you have checked a real card settlement against it.

### When there is no single match

If two or more paid invoices fit the same deposit, the app books nothing and sends an alert. Reconcile the deposit in the Bank Reconciliation Tool. A deposit that matches nothing is left as an ordinary unreconciled Bank Transaction.

## Cancel a Payment Request

Cancelling a Payment Request that has a Mercury invoice cancels the invoice in Mercury when it is still unpaid. While the payment is processing or paid, the cancel is refused: settle or refund the payment in Mercury first.

## Overdue reminders

With **Send Overdue Reminders** ticked, a daily job re-sends ERPNext's Payment Request email for each Mercury Payment Request that is still **Requested** after its date, when:

- its Mercury invoice is still unpaid,
- it has had fewer than **Maximum Reminders** reminders, and
- the last one was at least **Reminder Interval (Days)** days ago.

The count and date are kept in **Mercury Reminders Sent** and **Mercury Last Reminder On**. When the last reminder goes out, an alert tells you to follow up with the customer yourself. Reminders use ERPNext's email whichever **Invoice Email Sender** you chose.

## Related

- [Configuration](configuration.md#accounts-receivable-gateway) for the settings.
- [Troubleshooting](troubleshooting.md) for the errors invoicing can raise.
