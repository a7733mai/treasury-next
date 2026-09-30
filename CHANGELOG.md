# Changelog

## Unreleased (1.1)

### Bank Position Alerts (Phase 7e)
- Per-bank daily projected positions from the Bank Position Forecast engine (`treasury.pdc.bank_positions`,
  one engine call per company; banks sharing a GL account are merged), evaluated against new **Balance
  Thresholds** (Treasury Company Settings: warning / minimum balance per company bank account).
- Hourly job: a **daily digest** per recipient, company and day from the digest hour on, and **critical /
  warning alerts** with dedupe and escalation ("Worse:"), as in-system notifications and / or e-mails in each
  recipient's language, limited to the banks the recipient may see. Settings and **Recipients** in Treasury
  Settings (install adds Treasury Manager once).
- **Entry-time low balance guard** (On Low Projected Balance: Ignore / Warn / Block) on cheque delivery
  (single, Cheque Batch submit / Deliver All, security cheque encashment), Bank Transfer submit / Send Now /
  Reschedule, Facility Repayment and Bank Charge submit; a Treasury Manager may override a Block (field
  *Override Low Balance* or the action's confirmation), which is logged. Every guarded entry triggers an
  evaluation after the commit.
- New **Treasury Alert Log** (digests, alerts, overrides; Log Settings clears it after 180 days), report
  **Bank Balance Outlook**, number card *Banks Below Minimum (7 days)*, chart *Lowest Projected Balance per
  Bank*. Patch `set_balance_alert_defaults`. No accounting is posted.

### Bank Charges (Phase 7d)
- New submittable **Bank Charge** (Charge / Refund) in Bank Transfers and the master **Bank Charge
  Category** (expense account per company, statement keywords) in Treasury Setup; six default categories
  are created once on install / migrate.
- Posted as one **Payment Entry** to the Bank's Supplier (Treasury Settings → Bank Suppliers, created on
  demand) with the whole amount as a deduction, so the supplier balance is never touched — or as one **Bank
  Entry** Journal Entry (Treasury Company Settings → Bank Charge Voucher). Clearance on posting or on the
  bank statement line (Bank Charge Clearance). Refunds reduce the charge's refundable amount.
- PDC Bank Clearing matches Posted bank charges and proposes new charges for unmatched withdrawals whose
  text contains a category keyword (created, submitted and reconciled in one click).
- Report **Bank Charges Analysis**: every bank charge booked by Treasury (bank charges, transfer fees,
  cheque clearance and bounce charges, LG / LC commissions and charges, facility fees) per month, bank,
  account, category or source, with a detail view.
- Posting service: `create_bank_charge_payment`. Patch `set_bank_charge_defaults`. Bank Charge is an
  accounting dimension doctype.
- UX: the charge form defaults everything from the category (account, cost center, description; the bank
  account is remembered), explains what Submit will post, heads a submitted charge with its voucher number,
  statement line and refunds, and offers *Record Refund* and *Reconcile with Statement Line*. PDC Bank
  Clearing gets a *Document* column, bulk selection buttons, *Set Charge Category*, a selection counter and
  a per-category confirmation. The category form previews its keywords; the report shows Top Category / Top
  Source cards, source colours and a drill-down from every group.

### Bank Transfers (Phase 7b)
- New module **Bank Transfers**: the submittable **Bank Transfer** (Party Payment / Party Receipt /
  Internal Transfer; Normal, Fawran, SWIFT), posted as one Payment Entry with the bank fee as a deduction
  row; Scheduled transfers are sent by a daily job on their execution date.
- Treasury Company Settings: *Bank Transfers* section (default method, banks per method, charge bearer,
  completion, fee refund on return, limit action, approval threshold), **Transfer Fee Rules** and **Transfer
  Limits**. Patch `set_bank_transfer_defaults` fills the new settings of existing companies.
- Posting service: charges on Pay / Receive Payment Entries, Internal Transfers between two currencies
  (no exchange difference in the entry), reversals keeping the charges, a public clearance date setter.
- PDC Bank Clearing matches statement lines with Sent transfers too.
- Reports: Bank Transfer Register, Transfer Fees Analysis; Bank Position Forecast flows and summary card.
- Bank Transfer is an accounting dimension doctype.
- Controls: the approval threshold compares amount + fee at the higher of the transfer's and the system
  exchange rate; the transfer limits are enforced under a Bank Account row lock with a locking count on
  submit, Send Now, Reschedule and the daily job; the form helpers check the company and return a party's
  bank details only to users who may read the party; return dates cannot be in the future.

## 1.0.1 — 2026-09-29

Fixes found during UAT on a live ERPNext 16 site.

- **Form action buttons**: every action button (PDC Cheque, Cheque Batch, Security Cheque, Bank Facility,
  Facility Drawdown, Letter of Guarantee / Credit, Cheque Discounting) called `frm.call` without the document,
  which Frappe resolves as a module function — the buttons failed in the browser ("Failed to get method").
  All calls now pass `doc: frm.doc`; `tests/test_client_calls.py` scans every form script and checks each
  called action exists and is whitelisted.
- **Postpone** proposes the current due date + 1 month instead of today.
- **Arabic terminology**: ERPNext's generic translations are wrong in a treasury context (e.g. "Amount" as
  quantity, "Issue Date" as a lawsuit date). Field labels are now translated under each DocType's context
  and report / dialog / print / e-mail words under a `Treasury` context, without overriding the core
  translations elsewhere. `tests/test_translations.py` enforces it.
- **Icon**: Treasury desktop / sidebar icon (solid + subtle) and `app_logo_url`.
- **Deployment (frappe_docker)**: `sites/assets` is a per-container volume, so the public folder link and
  the compiled Arabic `.mo` are now created in every container (workers and scheduler included, so alert
  e-mails are translated too).

## 1.0.0 — 2026-09-29

First release, for Frappe 16 / ERPNext 16 (Python 3.14). No Frappe / ERPNext file is modified: the app
integrates through its own DocTypes, custom fields on Payment Entry / Journal Entry, `doc_events`,
`doctype_js` and scheduler events. All accounting goes through `treasury/accounting/posting.py` as
standard Payment Entries and Journal Entries, idempotent per source document, event and cycle.

### Treasury Setup
- Treasury Settings (alert days, future clearance dates) and Treasury Company Settings with separate
  posting events for outgoing and incoming cheques (party posting, under collection stage, bank posting,
  on bounce) and the PDC account mapping per bank account (PDC Payable / Receivable, Under Collection,
  Bank Charges, cheque print format), fully validated.
- Roles Treasury Manager, Treasury User, Cheque Approver; posting in an elevated context after the
  permission check, without granting any permission on core doctypes.
- Voucher guard: Treasury vouchers cannot be cancelled or deleted from their own form.

### PDC Cheques
- PDC Cheque with operational and accounting statuses, distinct dates, status log and postponement log;
  actions Deliver / Receive, Post to Party Now, Deposit, Clear, Post to Bank, Postpone, Bounce (with
  charges), Re-present, Replace, Return to Party, Cancel; lifecycle fields protected against form / API
  writes; date ordering between events; multi-currency with exchange gain / loss.
- Cheque Book with locked number allocation and cancelled leaves.
- Daily party posting for *On Cheque Date* (catch-up of missed days) and due alerts.
- PDC Bank Clearing tool matching Bank Transactions and reconciling them.
- Print formats: cheque receipt / delivery voucher (print date = today, amount in words in Arabic or
  English) and cheque leaf.
- Reports: Bank Position Forecast (with extension hooks), Projected Party Statement, PDC Register, PDC
  Maturity, Bounced and Postponed Cheques, Cheque Book Usage; Treasury workspace, number cards and charts.

### Cheque Batch
- Multi-cheque voucher with installment generator, manual / FIFO / advance allocation, all-or-nothing
  submit creating one PDC Cheque per row, batch status, Deliver / Receive All, batch receipt built from the
  live cheques, conditional cancel.

### Security Cheques
- Guarantee cheques that never create accounting entries: receive / issue, custody and custody transfer
  (append-only log), return, lost, cancel, expiry extension; fixed or Open / Blank amounts (Treasury
  Manager); encashment through a PDC Cheque only.
- Expiry alerts, four reports (Register, Exposure, by Custody, Expired Not Returned), number cards and a
  receipt / handover print format.

### Bank Facilities
- Bank Facility with sub-limits, status actions (activate, suspend, renew, close) and utilisation computed
  from the ledger (with a hook for other documents).
- Facility Drawdown with repayment schedules (equal principal, annuity, bullet, grace period, Murabaha
  profit, day count), Facility Repayment with prepayment, optional monthly interest accrual.
- Letters of Guarantee (margin, commission, extend, reduce, release, claim, repay claim), Letters of
  Credit (margin, commission, amend, documents, acceptance, settlement by Payment Entry, release margin)
  and Cheque Discounting (discount, settlement at maturity, bounce with recourse).
- Facility installments and accepted LCs in the Bank Position Forecast; Facility Utilisation, Facility
  Repayment Schedule, Cost of Financing, LG Register, LC Register and Discounted Cheques reports; number
  cards and daily alerts.

### Quality
- Arabic translation (`locale/ar.po`) of every user-facing string; generic words already translated by
  Frappe / ERPNext are left to the core or translated with a context, so the app never changes the core
  wording.
- Uninstall removes exactly the custom fields the app created.
- About 400 integration tests, including translation catalogue and install / uninstall checks.
