# Changelog

## 1.2.0 — 2026-10-01

Treasury 1.2 speaks of cheques, not PDCs: the screens say "Cheque", "Cheque Register" and "Cheque Bank
Clearing" in English and "شيك / الشيكات" in Arabic, since the app handles current-dated cheques as well as
post-dated ones. Every cheque now shows whether it is current-dated or post-dated, and the Treasury workspace
and sidebar list every document, report and page of the app. Nothing is renamed internally and no posting
changes.

### Upgrading from 1.1.0
1. `bench migrate` runs the new patch `set_cheque_timing` (`[post_model_sync]`, after `set_facility_defaults`):
   every existing PDC Cheque with both dates gets its Cheque Timing recomputed from its issue and original due
   dates (a missing or wrong value is corrected). It is safe to run twice and does not change `modified`.
2. `bench compile-po-to-mo --app treasury` builds the new English catalogue (`en.mo`) next to the Arabic one;
   without it English screens keep showing "PDC".
3. `bench --site <site> clear-cache`, so the desk loads the new wording, sidebar and workspace.

### Cheques instead of PDC (Phase 8)
- Labels only: the module, DocTypes, reports, number cards, chart and print formats are shown as "Cheques",
  "Cheque", "Cheque Register", "Cheque Maturity", "Cheque Bank Clearing", "Cheques by Status" …; messages and
  field labels follow.
- Internal names are unchanged: DocTypes, reports, fieldnames, routes (`/app/pdc-cheque`), the REST API, the
  naming series `PDC-OUT-` / `PDC-IN-` and fixtures keep "PDC", so scripts, integrations and imports keep
  working.
- New English catalogue `locale/en.po` (Frappe translates English too): it maps every visible "PDC" string to
  the new wording. The Arabic catalogue uses "شيك / الشيكات" instead of "شيك آجل"; "آجل" is kept only where it
  really means post-dated or usance.
- Treasury Dashboard: the cheque cards are "Cheques Issued" and "Cheques in Hand" (they count every open cheque,
  not only post-dated ones).
- The Arabic name of PDC Bank Clearing is "تسوية الشيكات مع البنك" in every message.

### Cheque timing (Phase 8)
- New read-only field **Cheque Timing** on the cheque (in the list view and as a standard filter):
  *Current-dated* when the original due date is the issue date, *Post-dated* when it is later. It is set from
  the dates on every save, for both directions; a postponement does not change it (the original due date is
  used), and both dates are locked once the cheque leaves Created.
- Stored as "Current-dated" / "Post-dated" (English screens show "Current"; Arabic "حال" / "آجل").
- Informational only: no GL effect, no new voucher. A current-dated cheque follows the normal flow and can be
  handed over, posted and cleared on the same day.
- New **Cheque Timing** filter and column in PDC Register (Cheque Register), PDC Maturity and Cheque Leaf
  Register; a leaf matches only when its cheque is readable and has that timing.
- Treasury Dashboard: a **Cheque Timing** filter narrows the four open-cheque cards (Due Out / Due In This
  Week, Cheques Issued, Cheques in Hand) and their Cheque Register drill-downs; cash positions and Bounced This
  Month are not filtered (their reports have no timing filter). The filter is remembered with the others and
  cleared by Reset Filters.

### Complete workspace (Phase 8)
- The sidebar and the Treasury workspace have the same six sections / cards: **Cheques**, **Transfers &
  Charges**, **Security Cheques**, **Bank Facilities**, **Reports** (the Treasury Dashboard and all 23 reports)
  and **Setup**.
- Newly linked: Cheque Batch, Cheque Discounting, Custody Location, Treasury Alert Log, the reports Cost of
  Financing, Discounted Cheques, LG Register, LC Register, Security Cheques by Custody and Expired Security
  Cheques Not Returned, and the core Invoice Discounting, Bank Transaction, Bank Account, Mode of Payment,
  Document Naming Settings and Document Naming Rule.
- Bank Reconciliation Tool and Bank Clearance are deliberately not linked: they would clear Treasury vouchers
  outside the cheque lifecycle.
- A static test keeps the sidebar and workspace complete when a DocType or report is added.

## 1.1.0 — 2026-09-30

Treasury 1.1 turns the cheque and facility app of 1.0 into a treasury: every Treasury voucher has a Payment
Entry number to file its paper under, bank transfers, bank charges and bank position alerts are new modules,
cheques can be paid / received from any ERPNext screen, cheque leaves are printed on layouts, facilities cover
the Qatar banks and finance companies (Islamic products, auto debit, cheques to the lender), and a Treasury
Dashboard shows it all. As in 1.0, no Frappe / ERPNext file is changed: everything goes through the app's own
DocTypes, custom fields, hooks and standard Payment Entries / Journal Entries.

### Upgrading from 1.0.1
`bench migrate` runs the patches below once, in this order (`[post_model_sync]`); each only fills empty values:
1. `set_bank_settlement_voucher` — *Bank Settlement Voucher* = Payment Entry, *Mode Name Template*
   `{method} - {bank}`.
2. `set_bank_transfer_defaults` — the Bank Transfers settings (Normal, SHA, On Bank Confirmation, Warn).
3. `set_document_payment_defaults` — *Pay / Receive from Documents* (12 Monthly installments, the bank's active
   cheque book).
4. `set_bank_charge_defaults` — *Bank Charge Voucher* Payment Entry, *Bank Charge Clearance* On Posting.
5. `set_balance_alert_defaults` — *On Low Projected Balance* = Warn.
6. `set_cheque_leaf_defaults` — cancelled leaves kind and source, cheque print counts, reprint by a Treasury
   Manager only.
7. `set_facility_defaults` — lender type / name, repayment mode Manual, the schedule's payment voucher,
   *Facility Voucher* Payment Entry, *Auto Debit Posting* On Due Date.

**Bank Settlement Voucher = Payment Entry** for every existing company: the under-collection stage and the bank
settlement of cheques become Internal Transfer Payment Entries. In that mode a foreign-currency cheque is
settled at its carrying rate: the realised exchange gain / loss is no longer booked at clearance (it is left to
ERPNext's Exchange Rate Revaluation). Companies with foreign-currency cheques that want it booked at clearance
set *Bank Settlement Voucher* = Journal Entry after the migrate, before clearing any cheque.

### Payment Entry as the Single Reference (Phase 7a)
- New company setting **Bank Settlement Voucher** (Payment Entry, the default | Journal Entry): in Payment
  Entry mode the under-collection stage and the bank settlement of cheques are **Internal Transfer Payment
  Entries**, with the bank charges inside the same Payment Entry; an Internal Transfer Payment Entry is reversed
  by a reverse Payment Entry.
- Child table **Modes of Payment** (*Treasury Payment Mode Map*: bank account, method, Mode of Payment, Payment
  Entry series) with the actions *Generate Modes of Payment* and *Add Series to Payment Entry* (System Manager,
  with confirmation); every Payment Entry built by the posting service, the LC settlement included, takes the
  mode and series of its bank and method.
- **Accounting Details** and the company's accounting dimensions on PDC Cheque and Cheque Batch, copied to every
  voucher; cheques created by a batch inherit them.
- **Open party** (any Party Type) on PDC Cheque, Cheque Batch and security cheque encashment, and the *New Payee*
  action.
- **Payment Entry visibility**: a voucher summary on each Treasury document, a *Payment Entry* column in the
  reports, the Payment Entry number on the PDC Cheque Receipt and the Cheque Batch Receipt.
- Patch `set_bank_settlement_voucher`; install / uninstall updates.

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

### Pay / Receive from Documents (Phase 7c)
- A **Treasury** button group (plain `public/js/treasury_payment_buttons.js`, loaded by `doctype_js`) on Sales /
  Purchase Invoice, Sales / Purchase Order, Journal Entry, Expense Claim and Employee Advance (hrms), Customer,
  Supplier, Employee, Quotation and Supplier Quotation.
- Read-only server mappers returning an unsaved document: `make_pdc_cheque`, `make_cheque_batch`,
  `make_security_cheque`, and `make_bank_transfer` extended to Journal Entry and the hrms sources
  (`treasury/utils/source_documents.py`, `treasury/utils/hrms.py`).
- Cheque Batch *Created From* (`source_doctype` / `source_name`): *Generate Cheques* allocates the generated
  rows to that source.
- Treasury Company Settings section **Pay / Receive from Documents** (default cheque banks, cheque book
  auto-selection, installment defaults, tender / performance bond %).
- Treasury connections on the ERPNext / hrms doctypes (`override_doctype_dashboards`) and on the Treasury
  doctypes. Patch `set_document_payment_defaults`.

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
- New **Treasury Alert Log** (digests, alerts, overrides; Log Settings clears digests and alerts after 180
  days, override logs are kept permanently), report
  **Bank Balance Outlook**, number card *Banks Below Minimum (7 days)*, chart *Lowest Projected Balance per
  Bank*. Patch `set_balance_alert_defaults`. No accounting is posted.
- Alerts UX (7e-2): Arabic / RTL digest and alert e-mails with level colours and words, weekday dates, the
  lowest balance per bank, "was …" on worse days and the party Payment Entry of cheque payments; *Preview
  Digest*, *Send Test to Me* and *Run Alerts Now* for Treasury Managers; *Add All Bank Accounts* and hints
  in the settings; one red *Low Projected Balance* dialog with *Override and Continue / Submit* on every
  guarded form; report colours, a minimum line on its chart, a *Largest Shortfall* card and a workspace
  shortcut.

### Cheque Leaves, Cheque Printing and Security Reports (Phase 7f)
- **Cheque Leaf Register** (every leaf of the outgoing books with its status, usage, party, dates, Payment
  Entries; view Leaves / Books) on a read-only leaf engine (`treasury.pdc.cheque_leaves`); print format
  **Cheque Book Leaves**; Cheque Book Usage gets a Void Leaves column.
- **Void Leaf** on Cheque Book (one leaf or a range, reason type and reason, also on a Closed book); cancelled /
  void leaves rows keep kind, reason type, user, time and the cancelled cheque, and can only be written by the
  leaf actions.
- **Cheque Layout** per bank account (positions in mm, calibration, fonts, date formats, Arabic-Indic digits,
  "only" / currency / fraction wording, A4 carrier) with a live preview and a test print; print formats
  **Treasury Cheque**, **Cheque Batch Leaves**, **Cheque Layout Test Print**. *Print Cheque* registers each
  print (counter, last printed on / by, status log Printed / Reprinted, reprint reason; Treasury Manager only by
  default: `cheque_reprint_requires_manager`); *Payee Name on Cheque*.
- `treasury_amount_in_words`: optional "only", currency name and fraction name overrides, fraction in figures.
- Reports **Security Cheques Summary** (ageing buckets, chart, cards) and **Security Cheque Movements**.
- Patch `set_cheque_leaf_defaults`. No accounting in this phase.
- UX (7f-2): leaf bar and "Active · n left" on the Cheque Book, Void Leaves dialog with a live range check;
  leaf status pills, book summary rows, usage chart and one-book mode (Print Book / Open Cheque Book) in the
  Cheque Leaf Register; Cheque Layout form with a scaled preview (language, zoom, pinned while editing, click a
  box to edit it, out-of-leaf boxes in red, optional scan behind), headline of the layout that prints and a
  Test Print dialog; Print Cheque dialog with the words preview (`get_cheque_text`), reprint block and the
  manager rule; Print All Cheques with "Unprinted only / All" and the leaf loading order; print windows open
  in the click (never blocked); Security Cheques Summary grouped tree and Security Cheque Movements pills.

### Qatar Facilities and Finance Companies (Phase 7g)
- **Bank Facility**: *Lender Type* (Bank / Finance Company), *Lender* (Supplier; a bank's is created on first
  use), *Lender Name*, *Islamic Facility* (Profit wording; set by Murabaha / Tawarruq / Ijarah / Istisna'a
  sub-limits), late payment penalties paid to the lender or payable to charity (own accounts, charity Journal
  Entry). New sub-limits **Ijarah**, **Istisna'a**, **Trust Receipt** (*Default Tenor*) and **Invoice
  Discounting**.
- **Facility vouchers as Payment Entries** (Treasury Company Settings *Facility Voucher*, default Payment
  Entry): drawdowns, repayments, stage disbursements and trust receipts in the company currency are Pay /
  Receive Payment Entries to / from the lender with the whole amount in deductions
  (`posting.create_deductions_payment`: no party row, no Payment Ledger Entry); foreign currency keeps the
  Journal Entries. Utilisation, accrued interest and Cost of Financing read both voucher types.
- **Repayment Mode** per drawdown: Manual, **Auto Debit** (daily job on the due date, or posted from the bank
  statement in PDC Bank Clearing: *Auto Debit Posting*) and **PDC to Lender** (Generate Installment Cheques:
  one cheque per installment whose party posting is the repayment, one Payment Entry from PDC Payable; bounce /
  replace reopen the installment; no double posting).
- **Istisna'a** stages (*Disburse Stage*, commitment utilisation), **Ijarah** disbursement to another account,
  **Trust Receipt** created by the LC settlement (*Finance by Trust Receipt*, atomic), ERPNext **Invoice
  Discounting** counted against the facility (custom field `treasury_facility`, doc events).
- A Trust Receipt drawdown can be cancelled on its own (the settled credit logs it); an Invoice Discounting
  under a facility can be cancelled (ERPNext's own ledger rows no longer block it) and its loan Journal
  Entries refresh the facility.
- *Create → Security Cheque* on a facility (collateral cheque to the lender, linked to the facility).
- Reports: Cost of Financing categories (Interest, Profit, Ijarah Rentals, Commission, Fees, Penalties, Late
  Payment (Charity)) with the installment cheques; lender columns and the *Lender Type* filter; schedule report
  repayment mode, installment cheque, cheque status and payment voucher. Forecast: no double count of an
  installment and its cheque; installment cheques left out of the Projected Party Statement.
- Patch `set_facility_defaults` (the schedule's `payment_journal` becomes a Dynamic Link on
  `payment_voucher_type`).
- **Facilities UX**: one word per facility on every form (*Interest* / *Profit*, *Rental* for Ijarah) with an
  *Islamic* tag; lender indicators (Bank / Finance Company), a *Late Payment* section, sub-limit types filtered
  for a finance company, row hints; the schedule grid shows the state of each installment (*Cheque Issued*,
  *Cheque Due*, *Awaiting Statement*, *Posting Today*), its cheque with a status dot and its payment voucher;
  drawdown headline / intro per repayment mode, the *Generate Installment Cheques* preview dialog, the *Disburse
  Stage* dialog and the Istisna'a progress bar; repayment *Settled By* indicator and headline; installment
  cheque and batch intros; *Installment* rows and *Installments Only* in PDC Bank Clearing; the LC settle dialog
  with a live Trust Receipt preview. Cost of Financing drops all-zero categories and gets a stacked chart;
  Facility Utilisation is grouped by lender (Istisna'a commitment tooltip); the schedule report shows the same
  installment states and a *Covered by Cheques* card. Number card **Auto Debits Awaiting Statement**; *Invoice
  Discounting* in the workspace and the facility's Connections (only Active facilities of the company offered).

### Treasury Dashboard and Setup (Phase 7h)
- **Treasury Dashboard** page (`/app/treasury-dashboard`, plain page script, no bundle): company, bank
  accounts and 7 / 14 / 30-day horizon filters; bank position KPIs, per-bank levels, balance curve and cash
  flows (one 30-day Bank Position Forecast engine call), cheques due this week / issued / in hand / bounced this
  month, transfers awaiting confirmation, bank charges this month, and the company-wide security cheques,
  facility utilisation, next installments, expiring Letters of Guarantee / Credit and the 30-day maturities
  chart. Every card opens its report with the same filters and equals it; sections the user may not read are
  left out. One read-only endpoint (`get_dashboard_data`, service `treasury.pdc.overview`).
- **Workspace refresh**: a *Treasury Dashboard* shortcut first, the card *Treasury Masters* becomes **Treasury
  Setup** (the setup masters in setup order, Document Naming Settings / Rule included), the sidebar gets the
  dashboard and *Security Cheques* / *Bank Facilities* sections.
- **Setup Checklist** section in Treasury Company Settings: eight Required / Recommended items (PDC Accounts,
  Modes of Payment, default banks, transfer fee rules, balance thresholds, Cheque Layouts, Bank Charge
  Categories, Bank Suppliers), each with what is missing and a link to fix it; a headline while a Required
  item is missing; the dashboard shows the count.
- **Naming Series** section: every Treasury series field, its options and default, how the series is chosen,
  the Document Naming Rules per DocType, and the routes to Frappe's *Document Naming Settings* / *Document
  Naming Rule* (*Add Rule per Bank*). No new naming mechanism.
- LC Register filter **Expiring Within (Days)**; Bounced and Postponed Cheques filter **Bank Account**.
- Uninstall also deletes the Property Setters, Document Naming Rules and Custom Fields of the app's own
  doctypes (orphans otherwise); the Payment Entry series, Modes of Payment, payees and roles are still kept.
- Refactors shared by the dashboard and the reports: `bank_positions.collect_positions`,
  `pdc.dashboard.open_cheque_totals`, `security_exposure.get_totals`. No patch, no accounting.

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
