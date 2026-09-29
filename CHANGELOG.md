# Changelog

## Unreleased (1.1)

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
