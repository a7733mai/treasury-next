## Treasury

Post-dated cheques, security cheques, multi-cheque vouchers, bank transfers, bank charges, bank position
alerts, bank facilities and a treasury dashboard for ERPNext v16.

**English.** Treasury manages the cheques a company writes and receives (post-dated cheques with their full
lifecycle and configurable posting events), multi-cheque vouchers (e.g. 12 rent cheques in one document),
guarantee cheques that never touch the ledger, bank transfers (Normal, Fawran, SWIFT) with their fees, bank
charges, daily bank position alerts and bank credit facilities (limits, loans, Murabaha and the other Islamic
products, Letters of Guarantee / Credit, cheque discounting), with a bank position forecast, a **Treasury
Dashboard** and Arabic / English print formats. Every accounting effect is a standard ERPNext Payment Entry or
Journal Entry; no ERPNext or Frappe file is changed.

**العربية.** تطبيق الخزينة لـ ERPNext بيدير الشيكات الآجلة الصادرة والواردة بدورة حالات كاملة وأحداث ترحيل
قابلة للضبط لكل شركة، وسند الشيكات المتعدد (زي 12 شيك إيجار في مستند واحد)، وشيكات الضمان من غير أي قيد
محاسبي، والتحويلات البنكية (عادي وفوري وسويفت) برسومها، ومصاريف البنوك، وتنبيهات مركز البنك اليومية،
والتسهيلات البنكية (الحدود، والقروض، والمرابحة وباقي المنتجات الإسلامية، وخطابات الضمان، والاعتمادات
المستندية، وخصم الأوراق التجارية)، ومعاهم تقرير مركز البنك المتوقع و**لوحة الخزينة** وطباعة عربي وإنجليزي.
كل القيود بتطلع كـ Payment Entry أو Journal Entry قياسية، ومن غير أي تعديل على كود ERPNext أو Frappe.

Version 1.1.0 — Frappe 16 / ERPNext 16, Python 3.14. License: MIT.

---

### Contents

1. [What's new in 1.1](#whats-new-in-11)
2. [Installation](#installation)
3. [Upgrading from 1.0](#upgrading-from-10)
4. [Setup guide](#setup-guide)
5. [Configuration](#configuration)
6. [Modules](#modules): [PDC Cheques](#pdc-cheques), [Cheque Batch](#cheque-batch-multi-cheque-voucher),
   [Security Cheques](#security-cheques), [Bank Facilities](#bank-facilities), [Bank Transfers](#bank-transfers),
   [Bank Charges](#bank-charges), [Bank Position Alerts](#bank-position-alerts),
   [Pay or receive from any screen](#pay-or-receive-from-any-screen), [Treasury Dashboard](#treasury-dashboard)
7. [Reports and workspace](#reports-and-workspace)
8. [Roles and permissions](#roles-and-permissions)
9. [Scheduled jobs](#scheduled-jobs)
10. [Known limitations and follow-ups](#known-limitations-and-follow-ups)
11. [Uninstall](#uninstall-what-is-kept)
12. [Testing](#testing)

---

### What's new in 1.1

- **7a — Payment Entry as the single reference**: *Bank Settlement Voucher* (Payment Entry by default), the
  Modes of Payment map with Payment Entry series per bank and method, Accounting Details and dimensions, open
  party / *New Payee*, the Payment Entry numbers on every document and report
  ([Payment Entry settlement](#payment-entry-settlement), [Modes of Payment](#modes-of-payment)).
- **7b — Bank Transfers**: Normal / Fawran / SWIFT transfers as one Payment Entry with the fee inside, fee
  rules, limits, approval ([Bank Transfers](#bank-transfers)).
- **7c — Pay / receive from any screen**: the *Treasury* buttons on invoices, orders, Journal Entries,
  parties, quotations and HRMS documents, and the Treasury connections
  ([Pay or receive from any screen](#pay-or-receive-from-any-screen)).
- **7d — Bank Charges**: Bank Charge and categories, statement keyword proposals, Bank Charges Analysis
  ([Bank Charges](#bank-charges)).
- **7e — Bank Position Alerts**: balance thresholds, the daily digest and critical alerts, the entry-time low
  balance guard, Bank Balance Outlook ([Bank Position Alerts](#bank-position-alerts)).
- **7f — Cheque leaves, cheque printing, security reports**: Cheque Leaf Register, Void Leaf, Cheque Layout
  printing, Security Cheques Summary / Movements ([PDC Cheques](#pdc-cheques)).
- **7g — Qatar facilities and finance companies**: lenders, Islamic products, Payment Entry facility vouchers,
  Auto Debit and PDC to Lender repayments ([Bank Facilities](#bank-facilities)).
- **7h — Treasury Dashboard and setup**: the dashboard page, the *Treasury Setup* workspace card, the *Setup
  Checklist* and *Naming Series* sections of Treasury Company Settings ([Treasury Dashboard](#treasury-dashboard),
  [Setup guide](#setup-guide)).

---

### Installation

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app $URL_OF_THIS_REPO --branch develop
bench --site <site> install-app treasury
bench compile-po-to-mo --app treasury      # Arabic translations (locale/ar.po -> sites/assets/locale)
```

- Requires ERPNext (the app links to Company, Account, Bank Account, Payment Entry, Journal Entry ...).
- **No Node.js needed.** The app ships no JS bundle (`*.bundle.js`, `app_include_js`): client code lives in
  the DocType / report `.js` files and in `public/js/voucher_treasury_link.js`, loaded by `doctype_js`, so
  `bench build` is not required for it. This is what makes it installable in **frappe_docker** runtime
  containers (which have no Node): add the app to the image (or copy it into every Python container —
  backend, queue workers, scheduler — and `pip install -e` it), then run `install-app` / `migrate` and
  `compile-po-to-mo` from the backend container (the MO files land in the shared `sites` volume). Restart
  the workers and the scheduler afterwards.
- `after_install` / `after_migrate` (idempotent, safe on a fresh site without any Treasury Company
  Settings): creates the module definitions and the roles *Treasury Manager*, *Treasury User*, *Cheque
  Approver*; creates / updates the custom fields on Payment Entry and Journal Entry; stores the missing
  Treasury Settings defaults; removes, once per site, the Custom DocPerm grants of development versions.
- **Uninstall** (`before_uninstall`) removes exactly the custom fields the app created on Payment Entry /
  Journal Entry (`treasury_section`, `treasury_source_doctype`, `treasury_source_name`, `treasury_event`,
  `treasury_cycle`) and Invoice Discounting (`treasury_facility`) and nothing else from core, plus the
  customisations of the app's own doctypes (see [Uninstall](#uninstall-what-is-kept)). The three roles and
  their assignment to users are kept on purpose (without the app they grant nothing); delete them in *Role*
  if they are not wanted. The vouchers created by Treasury stay ordinary Payment / Journal Entries.

### Upgrading from 1.0

> [!IMPORTANT]
> **Upgrading from 1.0.** The migrate patch `set_bank_settlement_voucher` switches **every existing company**
> to *Bank Settlement Voucher* = **Payment Entry** (Treasury Company Settings, section *Vouchers*): from then
> on the under-collection stage and the bank settlement of cheques are Internal Transfer Payment Entries (see
> [Payment Entry settlement](#payment-entry-settlement)). In Payment Entry mode a **foreign-currency cheque is
> settled at its carrying rate**: the realised exchange gain / loss is **no longer booked at clearance** (it
> is left to ERPNext's *Exchange Rate Revaluation*). Companies with foreign-currency (e.g. USD) cheques that
> want the realised exchange difference booked at clearance must set *Bank Settlement Voucher* = **Journal
> Entry** after the migrate, before clearing any cheque (vouchers already posted are not changed either way).
>
> *Generate Modes of Payment* may **add an account row to an existing Mode of Payment** that has the same
> name as a generated one (see [Modes of Payment](#modes-of-payment)).

`bench migrate` runs these patches once, in this order (`patches.txt`, `[post_model_sync]`); each only fills
values that are still empty, an explicit choice is kept:

1. `treasury.patches.v1_1.set_bank_settlement_voucher` — Treasury Company Settings: *Bank Settlement Voucher*
   = Payment Entry and *Mode Name Template* = `{method} - {bank}` (see the box above).
2. `treasury.patches.v1_1.set_bank_transfer_defaults` — the Bank Transfers settings (method Normal, charge
   bearer SHA, completion On Bank Confirmation, limit action Warn); fee rules and limits start empty.
3. `treasury.patches.v1_1.set_document_payment_defaults` — *Pay / Receive from Documents*: 12 Monthly
   installments, *Use the Bank's Active Cheque Book* on.
4. `treasury.patches.v1_1.set_bank_charge_defaults` — *Bank Charge Voucher* Payment Entry, *Bank Charge
   Clearance* On Posting.
5. `treasury.patches.v1_1.set_balance_alert_defaults` — *On Low Projected Balance* = Warn.
6. `treasury.patches.v1_1.set_cheque_leaf_defaults` — cancelled leaves get the kind *Cancelled* (and their
   cancelled cheque as source), cheques a print count of 0, *Only a Treasury Manager may reprint a cheque* on.
7. `treasury.patches.v1_1.set_facility_defaults` — lender type Bank and lender name on the facilities,
   repayment mode Manual on the drawdowns, repayments settled by the bank, the schedule's payment voucher a
   Journal Entry, *Facility Voucher* Payment Entry and *Auto Debit Posting* On Due Date.

No other data is changed. 1.1.0 (phase 7h) adds no patch: the dashboard page, the workspace and the new
settings sections are synced by the migrate. After the migrate, open *Treasury Company Settings* and follow the
[Setup guide](#setup-guide) (its *Setup Checklist* lists what is missing).

### Setup guide

The order below is the order of the **Setup Checklist** (Treasury Company Settings, first section) and of the
*Treasury Setup* card of the workspace. Every checklist item says what is missing and links to the place to
fix it; *Required* items must be done before the first cheque, *Recommended* ones before the related feature.

1. **Roles and users.** Give the treasury staff *Treasury Manager*, *Treasury User* or *Cheque Approver*,
   plus a standard ERPNext role that can read the masters (see [Roles and permissions](#roles-and-permissions)).
2. **Company Bank Accounts** (*Bank Account*, *Is Company Account*) whose GL account has Account Type *Bank*
   (checklist: no bank at all is a *Required* item).
3. **PDC Accounts** (*Required*): one PDC Account Mapping row per company bank account (PDC Payable /
   Receivable, Under Collection, Bank Charges) — see [Configuration](#configuration).
4. **Modes of Payment and Payment Entry series**: *Generate Modes of Payment*, then *Add Series to Payment
   Entry* (System Manager) — a row for *Cheque Out* and *Cheque In* of every PDC bank and for the method of
   every default transfer bank ([Modes of Payment](#modes-of-payment)). A series not registered on Payment
   Entry yet shows as a *Warning*.
5. **Default banks**: *Default Bank (Outgoing Cheques)* and *Default Bank (Normal)* (with a single PDC bank,
   the checklist offers *Use …* to fill both).
6. **Transfer fee rules and limits**: a fee rule for every default transfer bank and method
   ([Bank Transfers](#bank-transfers)).
7. **Balance thresholds, alert settings and recipients**: one threshold row per company bank (*Add All Bank
   Accounts*), then Treasury Settings → Bank Position Alerts ([Bank Position Alerts](#bank-position-alerts)).
8. **Cheque books and Cheque Layouts**: a default Cheque Layout (or a Cheque Print Format on the PDC Accounts
   row) for every PDC bank ([PDC Cheques](#pdc-cheques)).
9. **Bank Charge Categories and Bank Suppliers**: an expense account of the company on every enabled category;
   map each Bank to its existing Supplier in Treasury Settings → Bank Suppliers, or let the first charge create
   one ([Bank Charges](#bank-charges)).
10. **Naming series** (section *Naming Series* of Treasury Company Settings, documentation only — see the table
    below): change the options of a series in *Document Naming Settings*; name per bank (or per any condition)
    with a *Document Naming Rule* (*Add Rule per Bank* prepares one).
11. **Facility settings**: *Facility Voucher*, *Accrue Facility Interest Monthly*, *Auto Debit Posting*
    ([Bank Facilities](#bank-facilities)).
12. **Open the Treasury Dashboard** (`/app/treasury-dashboard`, first shortcut of the workspace): its banner
    shows how many checklist items are still missing.

**Naming series of the Treasury DocTypes**

| DocType | Series field | Shipped options (default first) | How the series is chosen | Per-bank rule condition field |
|---|---|---|---|---|
| PDC Cheque | `naming_series` (hidden) | `PDC-OUT-.YYYY.-.#####`, `PDC-IN-.YYYY.-.#####` | fixed by direction | `bank_account` (+ `direction`) |
| Cheque Batch | `naming_series` (hidden) | `CHQB-OUT-.YYYY.-.####`, `CHQB-IN-.YYYY.-.####` | fixed by direction | `bank_account` (+ `direction`) |
| Security Cheque | `naming_series` (hidden) | `SEC-IN-.YYYY.-.#####`, `SEC-OUT-.YYYY.-.#####` | fixed by direction | `bank_account` (+ `direction`) |
| Cheque Book | `naming_series` (hidden) | `CHB-.YYYY.-.####` | field default (Document Naming Settings) | `bank_account` |
| Bank Transfer | `naming_series` (visible) | `BT-.YYYY.-.#####` | user / field default | `bank_account` |
| Bank Charge | `naming_series` (visible) | `BCH-.YYYY.-.#####` | user / field default | `bank_account` |
| Bank Facility | `naming_series` (hidden) | `BF-.YYYY.-.#####` | first option | `default_bank_account` |
| Facility Drawdown | `naming_series` (hidden) | `FDD-.YYYY.-.#####` | first option | `bank_account` |
| Facility Repayment | `naming_series` (hidden) | `FRP-.YYYY.-.#####` | first option | `bank_account` |
| Letter of Guarantee | `naming_series` (hidden) | `LG-.YYYY.-.#####` | first option | `bank_account` |
| Letter of Credit | `naming_series` (hidden) | `LC-.YYYY.-.#####` | first option | `bank_account` |
| Cheque Discounting | `naming_series` (hidden) | `CHD-.YYYY.-.#####` | first option | `bank_account` |
| Payment Entry (Treasury vouchers) | `pe_naming_series` of the *Modes of Payment* rows | — | per bank and method | — |

A Document Naming Rule is evaluated before the series (no Treasury controller defines `autoname`), the highest
priority first; its counter is one number that does not restart each year. PDC Cheque, Cheque Batch and
Security Cheque pick their series by direction, so re-ordering their options in Document Naming Settings
changes nothing: use a rule with a `direction` condition. Cheque Layout (`CHL-#####`), Bank Charge Category,
Custody Location, Treasury Company Settings (by name) and Treasury Alert Log (hash) are named by the app.
The default of a series is the field's default (the one Document Naming Settings writes; Bank Transfer, Bank
Charge and Cheque Book ship one), else the first option. The section lists the existing rules only to users
who may read Document Naming Rule (System Manager).

### Configuration

**Treasury Settings** (single, global): *Default Alert Days* for PDC due alerts (default 3), *Security
Cheque Alert Days* (default 15), *Facility Alert Days* (default 7), *Allow Future Clearance Date* (default
off). `bench migrate` stores the default of any of these fields a site has no value for yet.

**Treasury Company Settings** (one per company) — the posting events, each set separately for outgoing and
incoming cheques:

| Setting | Options (default first) |
|---|---|
| Outgoing Party Posting | **On Handover**, On Cheque Date, On Creation (warns: the cheque may never be handed over), Manual |
| Outgoing Bank Posting | **On Actual Clearance**, On Bank Transaction Date, Manual |
| Incoming Party Posting | **On Receipt**, On Cheque Date, Manual |
| Incoming Under Collection Stage | **On Deposit**, Disabled |
| Incoming Bank Posting | **On Actual Clearance**, On Bank Transaction Date, Manual |
| On Bounce | **Reverse by Journal Entry**, Cancel Payment Entry |
| Bounce Charges Account | Expense account (company currency) credited with the charges re-billed to the customer |
| Accrue Facility Interest Monthly | off by default (see Bank Facilities) |

**PDC Accounts** (child table *PDC Account Mapping*, one row per company bank account):

| Account | Account Type | Root Type | Currency |
|---|---|---|---|
| PDC Payable Account (أوراق دفع) | Bank | Liability | the bank account's |
| PDC Receivable Account (أوراق قبض) | Bank | Asset | the bank account's |
| Under Collection Account (شيكات تحت التحصيل) | Bank | Asset | the bank account's; required when Under Collection is *On Deposit* |
| Bank Charges Account | — | Expense | company currency |
| Cheque Print Format | optional default print format for the cheque leaf | | |

The PDC accounts have Account Type *Bank* because ERPNext's Payment Entry only pays from / to Bank or Cash
accounts; they sit under current liabilities / assets, not with the real banks. The bank account's own GL
account must have Account Type *Bank*.

**Posting events matrix** (all vouchers are created and submitted by `treasury/accounting/posting.py`,
linked back through the read-only `treasury_source_doctype` / `treasury_source_name` / `treasury_event` /
`treasury_cycle` fields, and never created twice for the same event and cycle):

| Event (when) | Voucher | Entry |
|---|---|---|
| Outgoing party posting (handover date / due date by the daily job / issue date / *Post to Party Now*) | Payment Entry (Pay) | Dr Supplier (or Customer) / Cr PDC Payable, allocated to the cheque's invoices / orders |
| Incoming party posting (receipt date / due date by the daily job / *Post to Party Now*) | Payment Entry (Receive) | Dr PDC Receivable / Cr Customer (or Supplier), allocated |
| Incoming deposit (Under Collection *On Deposit*) | Payment Entry (Internal Transfer); Journal Entry in *Journal Entry* mode | Dr Under Collection / Cr PDC Receivable |
| Bank posting (clearance date / bank transaction date via *PDC Bank Clearing* / *Post to Bank*) | Payment Entry (Internal Transfer, clearance date set, bank charges inside it); Journal Entry in *Journal Entry* mode | outgoing: Dr PDC Payable / Cr Bank; incoming: Dr Bank / Cr Under Collection (or PDC Receivable) — in *Journal Entry* mode plus exchange gain / loss for foreign currency |
| Bank charges at clearance | inside the bank Payment Entry; a separate Journal Entry in *Journal Entry* mode | Dr Bank Charges / Cr Bank |
| Bounce after the party posting | Cancel of the vouchers, or reversal (per *On Bounce*): an Internal Transfer Payment Entry by a reverse Payment Entry, the party Payment Entry and Journal Entries by a Journal Entry | the party owes / is owed again; later postings (under collection) are reversed too |
| Bounce charges (incoming) | Journal Entry | Dr Customer / Cr Bounce Charges Account |
| Clearing before the party posting | party posting first, then the bank posting | |
| Security cheque (any action) | **none** | only an encashment creates a PDC Cheque, which posts as above |

A Payment Entry or Journal Entry managed by Treasury cannot be cancelled or deleted from its own form (the
form links to the cheque / facility document, which undoes it).

#### Payment Entry settlement

*Bank Settlement Voucher* (Treasury Company Settings, section *Vouchers*) chooses the voucher of the
under-collection stage and of the bank settlement of cheques:

- **Payment Entry** (default): an Internal Transfer Payment Entry per bank event — PDC Receivable → Under
  Collection at the deposit, Under Collection (or PDC Receivable) → Bank / Bank → PDC Payable at the
  clearance — so every cheque event has a Payment Entry number to file the paper under. The bank charges
  are inside the same entry: outgoing, the bank pays amount + charges and PDC Payable receives the amount;
  incoming, the bank receives the amount − charges (the charges must be less than the cheque amount). They
  are booked on the deduction row ERPNext keeps for the exchange difference of an Internal Transfer, with
  its account changed to the Bank Charges Account: ERPNext 16 rejects any other deduction row on an
  Internal Transfer. A reverse Payment Entry undoes an Internal Transfer on bounce / return / replace; the
  party Payment Entry is still reversed by a Journal Entry (ERPNext cannot reverse an invoice-allocated
  Payment Entry by a Payment Entry).
- **Journal Entry**: the behaviour of version 1.0 (Bank Entry Journal Entries, the charges in a separate
  Journal Entry).

Switching is safe at any time: vouchers already posted stay as they are, a voucher is always reversed by the
kind that fits it (a Journal Entry of version 1.0 by a Journal Entry), and the bank Payment Entry takes the
cheque from whichever account the earlier voucher left it in.

*Foreign currency in Payment Entry mode.* PDC, Under Collection and Bank accounts share the bank's currency,
and ERPNext books an Internal Transfer between two accounts of one currency at one rate. The settlement is
therefore at the cheque's carrying rate (its party posting rate); a clearance rate entered on *Clear* is
ignored with a message, and the realised exchange difference is left to ERPNext's *Exchange Rate
Revaluation*. Companies that want the difference booked at the clearance keep *Journal Entry* mode.

*Bank reconciliation of outgoing cheques with charges.* The bank Payment Entry credits the bank with the
amount plus the charges in one line, while a bank statement usually shows the cheque and the charges as two
lines, so ERPNext's Bank Reconciliation Tool will not match it exactly (match it manually against both
lines). The *PDC Bank Clearing* tool reconciles the Bank Transaction against the bank Payment Entry (or the
bank Journal Entry of a Journal Entry mode / 1.0 cheque).

#### Modes of Payment

Treasury Company Settings, section *Modes of Payment*: one row per (company bank account, method — Cheque Out,
Cheque In, Bank Transfer, Fawran, SWIFT, Internal Transfer, Auto Debit, Bank Charge) with a Mode of Payment
and an optional Payment Entry naming series (e.g. `PE-RYN-CHQ-.YYYY.-`). Every Payment Entry of a cheque gets
the mode and series of its bank and direction (a cheque may override the mode); the LC settlement gets the
*Bank Transfer* row.

- *Generate Modes of Payment* (Treasury Manager / System Manager) creates the missing modes (type *Bank*,
  the bank's GL account as the company's default account) named by the *Mode Name Template* (`{method}`,
  `{bank}`, `{account}`; two accounts at the same bank get the account name appended) and fills the
  missing rows. Existing map rows are never changed, and a mode that already uses another account for the
  company is skipped with the reason. **Note:** when a Mode of Payment with the generated name already exists
  (created by hand, by another app or for another company) and has no account for this company, Generate
  Modes of Payment **adds an account row** for this company (the bank's GL account) to that existing mode and
  links it — Mode of Payment records are shared by all companies, and this happens even if the user has no
  write permission on Mode of Payment. Choose a *Mode Name Template* that does not collide with modes you do
  not want Treasury to touch. The action works on the saved settings (save your edits first); given lists of
  methods / bank accounts must not be empty.
- A series must be registered on Payment Entry before it is used: *Add Series to Payment Entry* (System
  Manager, with confirmation) adds the map's series through Document Naming Settings, keeping the current
  default series first. Posting on a bank whose series is not registered fails with a clear message.

**Accounting details.** PDC Cheque and Cheque Batch have *Accounting Details* (cost center, project, mode of
payment, party bank account, contact person) and the company's *Accounting Dimensions*; they are defaulted
on save (company cost center and dimension defaults, the mapped mode, the party's default bank account and
contact), copied from a batch to its cheques and to a replacement cheque, carried to every voucher and
locked once the cheque is posted.

#### Open party / New Payee

A PDC Cheque or Cheque Batch may be of any ERPNext *Party Type* — Customer, Supplier, Employee, Shareholder
... The party account comes from ERPNext (the party's account, else the company default for the Party
Type's account type). Invoices and orders can be allocated only by an outgoing cheque to a Supplier / an
incoming cheque from a Customer; any cheque may settle a Journal Entry. *New Payee* creates a Supplier from a
name alone in the *Payee Supplier Group* of Treasury Settings (default *Treasury Payees*, created on
install), or selects the existing enabled Supplier of that name — one the user can read (User Permissions
apply) or one of the Payee Supplier Group. A Supplier of that name the user cannot read is neither selected
nor duplicated: the user is asked to have someone with access to Suppliers select it. A security cheque of
any party can be encashed (not one of an *Other* party).

### Modules

#### PDC Cheques

- **PDC Cheque** (PDC-OUT- / PDC-IN-): direction, party (any Party Type), bank account, cheque book
  (outgoing: the next free leaf is allocated when the Cheque No is left empty), cheque no, drawer bank,
  amount, currency (always the bank account's; company currency while an incoming cheque has no bank),
  exchange rate, references to invoices / orders (the allocation becomes final at the party posting; what
  is no longer outstanding then stays an unallocated advance, with a message).
- **Distinct dates**: issue, handover / receipt, original due, effective due (moved by postponements),
  deposit, actual clearance, bank transaction, party posting, bank posting. The print date is never stored.
- **Two statuses**, set only by the actions: operational (Created, Delivered / Received, Deposited,
  Cleared, Bounced, Returned, Endorsed, Discounted, Replaced, Cancelled; *Postponed* is a flag with a
  counter, *Overdue* an indicator) and accounting (Not Posted, Party Posted, Under Collection Posted, Bank
  Posted, Reversed). Every transition appends a status log row (from / to statuses, date and time, event
  date entered by the user, user, remarks, voucher).
- **Actions** (POST only; they check the permission on the cheque and, where required, the role): Deliver /
  Receive, Post to Party Now, Deposit, Clear (optionally with bank charges and the clearance rate), Post
  to Bank, Postpone (new due date, reason, contact person — Cheque Approver or Treasury Manager), Bounce
  (charges), Re-present, Replace (new cheque linked both ways), Return to Party, Cancel Cheque, plus the
  discounting actions. Every action's date may not precede the cheque's issue date or its latest event.
  Status, dates, links and vouchers cannot be written through the form or the REST API; identity fields
  (party, bank, amount, cheque no ...) lock once the cheque is handed over / received or posted.
- **Cheque Book**: bank account, leaf range, next number, cancelled leaves (with reason); numbers are
  allocated under a lock, never reused, and the book becomes Exhausted on its last leaf. Cheque numbers are
  unique per bank account across PDC and security cheques. **Void Leaf** (one leaf or up to 100 at once,
  reason type Damaged / Misprinted / Lost / Stolen / Other and a reason, also on a Closed book) records an
  unused leaf as *Void*; a cancelled cheque records its leaf as *Cancelled* with the cheque as its source.
  Every row keeps the user and the time, and the rows can only be written by these actions (not through the
  form or the REST API). The form shows used / cancelled / void / remaining leaves, the *Leaf Register* and
  the *Cheque Book Leaves* print (every leaf with its status).
- **Cheque printing (Cheque Layout)**: one layout per bank account (the default one, else the newest enabled)
  says where the date, payee, amount in figures, amount in words (two lines), crossing and signature label
  are printed, in mm from the top-left corner of the physical leaf, with printer calibration, fonts, date
  format (also box dates), Western or Arabic-Indic digits, "only" / currency name / fraction in words or
  figures and the currency / fraction names (e.g. "Fils" for QAR), on a cheque leaf or on an A4 carrier.
  *Test Print* prints a grid with every box outlined (with its left,top in mm) on blank paper; the form shows
  a live, scaled preview in Arabic or English (a click on a box goes to its fields, a box outside the leaf is
  outlined in red, an optional scan of a blank cheque can be shown behind it — never printed).
  **Print Cheque** (outgoing Created / Delivered cheques) first shows what will be printed (payee, date,
  figures, the amount in words on its two lines), asks for Arabic or English, registers the print
  (counter, last printed on / by, status log *Printed*) and opens the *Treasury Cheque* print; a printed
  cheque is printed again only with a reason (status log *Reprinted*) and, by default, only by a Treasury
  Manager (*Only a Treasury Manager may reprint a cheque* in Treasury Company Settings). *Payee Name on
  Cheque* replaces the party name on the leaf. A Cheque Batch prints all its printable cheques at once
  (*Cheque Batch Leaves*, all or nothing, *Print Unprinted Only* / *Reprint All*). Printing never posts
  anything. Without a layout, the bank keeps the Cheque Print Format of its PDC Account Mapping row or
  *PDC Cheque Print* (no counter). A leaf renders only for a registered print: from the saved cheque (never
  from a document sent by the browser), for a user with a Treasury role, for the print job of the URL
  registered by that same user, and while the payee, amount, currency, date, bank and number are the
  registered ones. Once printed, those fields cannot be edited (replace or cancel the cheque); a postponed
  date stops the leaf until a reprint is registered. *Print Unprinted Only* prints exactly the leaves it
  registered. The same printview URL opened again by the same user before any new print is not counted (a
  page view cannot write). Security cheques and incoming cheques are not printed on layouts.
- **PDC Bank Clearing** tool: suggests matches between open Bank Transactions and cheques (cheque no /
  amount, date tolerance), clears the selected cheques with the bank transaction date and reconciles the
  Bank Transaction against the bank Journal Entry — the engine of *On Bank Transaction Date*. It only
  lists the cheques and bank accounts the user may read.
- **Print formats**: *PDC Cheque Receipt* (receipt / delivery voucher with the print date = today, amounts
  in words in Arabic or English) and *PDC Cheque Print* (the cheque leaf); right-to-left in Arabic.
- **Multi-currency**: the cheque carries its rate (or the rate of the party posting date); the bank posting
  uses the clearance rate and ERPNext books the exchange gain / loss.

#### Cheque Batch (multi-cheque voucher)

- **What it is.** One voucher for many post-dated cheques to / from one party, e.g. 12 monthly rent cheques
  to a landlord. The *Installment Generator* fills the rows (count, frequency — monthly to yearly, weekly
  or every N days —, first due date, same amount per cheque or a total split evenly with the rounding
  difference going one unit each to the last cheques, first cheque number or the cheque book). Allocation
  per row: manual (one invoice / order per cheque), *FIFO Invoices* (the party's outstanding invoices, oldest
  due first, to the earliest due cheques; whatever is left is an advance — the summary also shows the
  outstanding the cheques do not cover, since one reference per row can leave both) or *Advance (no
  allocation)*. Saving with *FIFO Invoices* but nothing allocated shows a warning.
- **Submit** creates one PDC Cheque per row (cheque book leaves are allocated only now, in row order) and,
  if a Handover / Receipt Date is set, delivers / receives every cheque on it — which posts according to
  Treasury Company Settings exactly like a cheque entered on its own (e.g. `On Cheque Date`: the daily job
  posts each cheque on its own due date). Submit is all-or-nothing: if any row fails, nothing is kept (no
  cheque, voucher or leaf) and the message names the row.
- **After submit** every cheque lives its own life (postpone, clear, bounce, replace ... on the cheque);
  the batch shows their statuses, derives its own status (Partially Delivered / Received / Cleared, ...),
  offers *Deliver / Receive All* (each cheque on its own: failures are reported, the others go on), the
  *Cheque Batch Receipt* print and printing all cheque leaves. The PDC Register filters by batch. A
  replacement cheque stays in the batch of the cheque it replaces. The batch counts Cleared, Endorsed and
  Discounted cheques as settled ("Cleared").
- **The receipt matches the real cheques.** While the batch is submitted, the number, bank, amount, dates
  and references of its cheques cannot be edited on the cheque (only the cheque actions change a cheque,
  e.g. *Replace*). The receipt lists the live cheques — replacements with their own number, amount and due
  date, without Cancelled / Replaced ones — and totals them (figures and words). A draft or cancelled batch
  prints its rows marked as not a valid voucher. CHQB-OUT- / CHQB-IN- number the batches.
- **Size and concurrency.** A batch holds at most 120 cheques: submit with a handover date and *Deliver /
  Receive All* insert, hand over and post every cheque in one request. Every action on a cheque of a batch
  locks the batch row first, so the stored batch status never goes stale; during a batch-wide operation it
  is refreshed once at the end rather than after every cheque.
- **Cancel** is possible only while every cheque is still Created and not posted (or already cancelled /
  replaced): the cheques are cancelled and their leaves recorded as cancelled — this needs the Treasury
  Manager role, checked before anything is done. Otherwise the message lists the cheques to act on first.
  An amended batch gets fresh book leaves (hand-typed numbers are kept).
- **Why the Payment Entry is not changed.** A Payment Entry has one amount, one reference no / date and
  one clearance date, and the Bank Reconciliation matches it as a whole; a multi-cheque Payment Entry would
  need overriding ERPNext. Each cheque keeps its own Payment Entry / Journal Entries instead.

#### Security Cheques

- **What it is.** A guarantee cheque (شيك ضمان) held from a party (*Incoming*: tender, contract performance,
  advance payment, rent, employee custody ...) or given to a party (*Outgoing*), numbered SEC-IN- / SEC-OUT-.
  The party is a Customer, Supplier, Employee or *Other* (a free-text name). A cheque may reference the
  document it guarantees (Project, Contract, Sales / Purchase Order, Quotation, Supplier Quotation,
  Employee, Bank Facility), carries the *Required Amount* of that document, an optional cheque date (a
  security cheque may be undated) and an expiry date (end of the guarantee).
- **No accounting, ever.** A security cheque never creates a GL entry, Payment Ledger entry, Payment Entry
  or Journal Entry, whatever happens to it (receive, custody, return, lost, cancel). The tests check the
  company's ledgers after every action. The only way to accounting is *Encash*, through a PDC Cheque.
- **Lifecycle** (actions on the form; status, custody, dates, the PDC link and both logs change only
  through them — any other save touching them is rejected, and they are reset on insert):
  - Incoming: Created → *Receive* (straight into custody when a location is given) → Received → *Put in
    Custody* → In Custody → *Transfer Custody* (location and / or custodian) → *Return Cheque* / *Encash* /
    *Mark as Lost*.
  - Outgoing: Created → *Issue* → Issued → *Return Cheque* (the party gave it back) / *Encash* (the holder
    presented it) / *Mark as Lost*.
  - *Cancel Cheque* only while Created. Every action records its own date (receipt / handover, return,
    lost, encashment are separate fields) in the status log's Event Date; an action cannot be dated before
    the cheque's issue date or its latest event. Repeating an action raises an "already ..." error.
  - The custody log (from / to location and custodian, date, user, remarks) is append-only; returning,
    encashing or losing a cheque closes it with a final row.
  - Once received / issued, the cheque itself (party, cheque no, bank, amount, amount type, currency, issue
    and cheque dates) is locked. The expiry date then moves only through *Extend Expiry* (new date after
    the current one, reason required; a status log row "Expiry Extended" records old → new date), which
    works on any cheque that is not final. Remarks, purpose, reference and required amount stay editable
    while the cheque is live; once it is final (Returned, Encashed, Lost, Cancelled) the reference,
    required amount and expiry are frozen too, so the reports' history cannot be rewritten.
  - *Expired* (list / form indicator, virtual `is_expired` field): expiry date passed and still held /
    given. It is not a status.
- **Currency.** Outgoing: the currency of the bank account's GL account (read-only). Incoming: the company
  currency unless the user chooses another one before the first action. The user / site default currency
  Frappe fills into every new `currency` field is never kept: the form sets the company currency when the
  company changes, and a cheque inserted through the API without a currency gets the company currency.
- **Open / Blank cheques** carry no amount (the Amount must stay empty). Creating one (or turning a cheque
  into one), receiving or issuing it needs the **Treasury Manager** role.
- **Cheque book (outgoing).** An outgoing security cheque may take the next leaf of a Cheque Book of its
  bank account (Cheque No left empty). The leaf is consumed for good: cancelling, returning or losing the
  security cheque frees nothing, it is never allocated again, cannot be cancelled as an unused leaf, and
  counts as *Used* in Cheque Book Usage. A security cheque that took a leaf, or on which any action
  happened, cannot be deleted (cancel it instead).
- **Encash → PDC Cheque** (Treasury Manager): allowed for a Customer / Supplier party only. The user gives
  the encashment date, the amount (required for Open / Blank; a Fixed cheque is encashed for its own
  amount), the due date (default: the encashment date) and, for an incoming cheque, the bank account it is
  deposited to — it must be in the security cheque's currency (the dialog offers only those).
  - Incoming: a PDC Cheque (Incoming, same party, cheque no and drawer bank) is created and **received**
    on the encashment date, so it is posted per Treasury Company Settings like any cheque. Invoice / order
    references may be passed to settle them; without references the amount is an advance of the party.
  - Outgoing: a PDC Cheque (Outgoing, same bank account and cheque no, no cheque book) is created and
    **delivered** on the encashment date; clear it through the PDC flow when the bank pays it.
  - The PDC Cheque's issue date is the encashment date (an `On Creation` posting is never back-dated). It
    links back through its read-only *Security Cheque* field; the security cheque becomes Encashed with
    *Converted PDC Cheque* set. All or nothing: if the PDC Cheque cannot be created or received /
    delivered, nothing is kept. Replacing that PDC Cheque carries the *Security Cheque* link to the
    replacement.
- **Print.** *Security Cheque Receipt*: receipt (incoming) / handover acknowledgment (outgoing) with the
  print date (today), party, cheque, amount or "Open / Blank", purpose, reference, expiry, custody location
  and signatures; right-to-left in Arabic.

#### Bank Facilities

Credit facilities with banks: the agreement and its sub-limits, drawdowns with their repayment schedules,
repayments and interest, Letters of Guarantee, Letters of Credit and cheque discounting. Everything is our
own DocTypes; the Lending app is **not** used or required. All accounting goes through
`treasury.accounting.posting` as standard Journal Entries (and one Payment Entry for LC settlements)
linked back to the source document, idempotent per source, event and month.

- **Bank Facility** (not submittable; statuses by actions only — Treasury Manager): Draft → *Activate* →
  Active ⇄ *Suspend* (reason required) / *Reactivate* → *Close* (only with nothing utilised and no unpaid
  installment); *Renew* moves the expiry date and optionally the total / sub-limit amounts, and makes an
  Expired facility Active again. The daily job sets Active / Suspended facilities past their expiry date to
  Expired. Every action appends a status log row with the event date. Once activated, company, bank,
  currency, total limit, dates and the existing sub-limits (type, amount, liability account) change only
  through Renew (new sub-limits may be added). Accounts: Default Bank Account (a company account at this
  bank, in the facility currency), Interest / Commission Expense (company currency), Accrued Interest
  (liability, facility currency; it must differ from every sub-limit's liability account and cannot change
  while the facility has a submitted drawdown). Optional collateral notes and linked Security Cheques (no
  accounting).
- **Sub-limits** (*Bank Facility Limit*): Overdraft, Short / Long Term Loan, Letter of Guarantee, Letter of
  Credit, Cheque Discounting, Murabaha, Tawarruq, Other; rate type (fixed / variable reference + margin,
  the all-in rate is entered), day count (Actual/365, Actual/360), commission and cash margin. Each
  sub-limit is at most the total limit; together they may exceed it (lines share the total). Loans /
  Murabaha / Tawarruq / Other need a liability account in the facility currency.
- **Utilisation** (`treasury.bank_facilities.utilisation.get_utilisation(facility, limit_type, as_on)`) is
  computed from the ledger, never from stored numbers: loans = the balance of each drawdown's liability
  account on the Journal Entries linked to the drawdown or its repayments (other postings on the same
  account never count); Overdraft = max(0, − GL balance of the Default Bank Account's GL account). Available
  = min(sub-limit − its utilisation, total limit − total utilisation). Letters of Guarantee / Credit and
  Cheque Discounting add utilisation through the hook `treasury_facility_utilisation_sources` (functions
  `(facility_doc, as_on) -> [{"limit_type", "amount"}]`). The *Utilised* / *Available* fields on the
  facility are display only (refreshed after every action and daily); reports, cards and the limit checks
  use the API.
- **Facility Drawdown** (submittable): limit type (not Overdraft), amount, bank account credited (default:
  the facility's), terms (rate defaults to the sub-limit's; or a fixed Murabaha profit amount), tenor,
  frequency (Monthly / Quarterly / Semi-Annually / Annually / Bullet), method (Equal Principal / Equal
  Installment (annuity) / Bullet), first repayment date (default one period after the drawdown), grace months
  (interest only), upfront fees. *Generate Schedule* (draft only) builds the rows: day-count interest on the
  outstanding principal per period, annuity installment P·r / (1 − (1 + r)^−n) with r = rate × months / 12,
  a Murabaha profit spread straight-line, every amount rounded and the last row absorbing the rounding
  (principal always adds up to the amount), month-end dates kept on month ends. **Annuity**: the
  installment uses the nominal periodic rate while each row's interest uses the day count, so the last row
  can differ noticeably from the others (more so with Actual/360). Rows may be edited before submit as long
  as the principal adds up. Changing a term requires a new schedule: the form clears it (rebuilt on save)
  and the server refuses a changed term with an unchanged schedule. Submit checks the facility is Active and
  the date inside its period, and the available amount of the sub-limit and of the total, then posts Dr Bank
  / Cr Liability (and Dr Fees / Cr Bank for the upfront fees). Cancel is refused while repayments are
  submitted and cancels its vouchers (accruals included). System fields are reset on save and on submit,
  whatever the client sends.
- **Facility Repayment** (submittable): principal + interest / profit (empty: the next unpaid installment),
  optional penalty (+ account), allocation *Oldest First* or *Selected Row*. Posts Dr Liability (principal)
  + Dr Accrued Interest (the part already accrued, cleared first) + Dr Interest Expense (the rest) + Dr
  Penalty / Cr Bank, then records the paid amounts on the schedule rows (Pending / Partially Paid / Overdue /
  Paid; Overdue refreshed daily). **Prepayment**: principal beyond the installments due up to the repayment
  date is recorded as a paid prepayment row and the later untouched installments are rescheduled by
  **reducing the installment amount** (the tenor is kept). A repayment cannot be back-dated before a
  submitted one; only the last **submitted** one (highest *Submission No*, stamped at submit under the
  drawdown lock) can be cancelled, which restores the schedule exactly (snapshot) and cancels its Journal
  Entry.
- **Bank clearance**: drawdown, fees and repayment Journal Entries are cleared on their posting date, so
  they do not wait in the Bank Reconciliation Tool. Accrual Journal Entries have no bank row.
- **Concurrency**: drawdown submits lock the facility row, repayment submits / cancels and the accrual job
  lock the drawdown row; a stamp read before the lock is compared with the locking read, and the user is
  asked to try again if another transaction committed meanwhile.
- **Interest accrual** (Treasury Company Settings *Accrue Facility Interest Monthly*, default off): the
  daily job posts, per drawdown and month end, Dr Interest Expense / Cr Accrued Interest for the interest
  earned up to the month end less what was already accrued or expensed by repayments — idempotent (event
  `interest_accrual:YYYY-MM`), catches up missed months, one drawdown at a time (a failing one is rolled
  back and logged once a day, the others go on).
- **Forecast**: the unpaid part of every installment is an outflow on its due date in the Bank Position
  Forecast (hook `treasury_forecast_sources`); the summary gets an *Available Overdraft* card through the
  hook `treasury_forecast_summary_sources` (`(filters) -> [{"label", "value"}]`). An installment with a live
  installment cheque (see below) is left out: the cheque is the expected outflow.

**Lenders, Islamic products and repayment modes (Qatar facilities and finance companies)**

- **Lender**: a facility's *Lender Type* is Bank (the Bank is required) or Finance Company (the *Lender*
  Supplier is required, the bank optional; no Overdraft, LG, LC, Trust Receipt or Cheque Discounting
  sub-limit). The lender of a bank facility is the Bank's Supplier (Treasury Settings → Bank Suppliers),
  created and stored on the facility on first use. *Lender Name* is shown on the forms and reports. Lender
  type, lender and the Islamic flag are locked once the facility is activated.
- **Islamic**: a Murabaha, Tawarruq, Ijarah or Istisna'a sub-limit makes the facility Islamic (*Islamic
  Facility*): forms, reports and remarks say *Profit* instead of *Interest*. *Late Payment Mode*: paid to
  the lender with the installment, or *Payable to Charity*: the repayment's penalty is then not paid through
  the bank but booked Dr *Late Payment (Charity) Account* / Cr *Charity Payable Account* (its own Journal
  Entry, cancelled with the repayment).
- **Voucher** (Treasury Company Settings *Facility Voucher*, default Payment Entry): in the company currency,
  drawdowns (Receive from the lender: Dr Bank, Dr Fees / Cr Liability), repayments (Pay to the lender: Dr
  Liability, Dr Interest or Accrued Interest, Dr Penalty / Cr Bank), stage disbursements to the bank and
  Trust Receipts are **Payment Entries** with the lender as Supplier and the whole amount in deduction rows:
  no party GL row and no Payment Ledger Entry, the lender's balance is untouched. Foreign currency
  facilities (deduction rows are booked in the company currency) and the setting *Journal Entry* keep the
  Journal Entries. Reports read both voucher types.
- **Ijarah** (finance lease, liability + profit): the rentals are the schedule's installments (principal +
  profit); *Disburse To* Other Account (e.g. the asset / CWIP account) when the financier pays the supplier
  directly (Journal Entry Dr that account / Cr Ijarah Liability). Operating Ijarah (rental expense only) is
  not supported.
- **Istisna'a** (staged): the drawdown lists its *Disbursement Stages* (adding up to the amount); submit posts
  only the upfront fees, *Disburse Stage* (Treasury Manager) posts each stage (to the bank or another
  account). The pending stages count against the sub-limit as a commitment; the principal repaid cannot
  exceed what was disbursed. The schedule charges profit on the whole contract from the drawdown date; an
  *Auto Debit* installment due before its stage is disbursed waits: the daily job posts it (on its due date)
  by the first run after the disbursement.
- **Trust Receipt**: *Settle* on a Letter of Credit with *Finance by Trust Receipt* creates and submits a
  Trust Receipt drawdown for the settlement less the margin held (bullet, the sub-limit's *Default Tenor* and
  rate unless given), in the same transaction: a Trust Receipt above the available limit rolls the whole
  settlement back. A settled credit is never cancelled; its Trust Receipt (without submitted repayments) may
  be cancelled on its own, e.g. to re-enter it by hand with other terms: the credit stays settled, keeps the
  link and logs *Trust Receipt Cancelled*.
- **Invoice Discounting**: ERPNext's own Invoice Discounting gets a *Bank Facility* field; its total counts
  against the facility's *Invoice Discounting* sub-limit while Sanctioned / Disbursed (checked on submit).
  ERPNext posts its own entries; Treasury posts nothing. Historical utilisation uses the current status; the
  facility's stored utilisation is refreshed on its submit / cancel and on its loan Journal Entries
  (Disbursed / Settled). ERPNext 16.36 cannot cancel an Invoice Discounting (its own GL / Payment Ledger rows
  block Frappe's back-link check); for one under a facility Treasury lets those rows be ignored on cancel, as
  ERPNext does for its vouchers (a submitted loan Journal Entry still blocks it).
- **Repayment Mode** (per drawdown, may change after submit):
  - *Manual*: the user records the repayments (settled by the bank).
  - *Auto Debit*: the bank debits each installment. Treasury Company Settings *Auto Debit Posting*: *On Due
    Date* — the daily job records the repayment of every installment due (on its due date, catching up missed
    days; never blocked by the low balance guard), and *PDC Bank Clearing* only reconciles the statement line;
    *On Bank Statement Match* — *PDC Bank Clearing* proposes the open installment for a statement debit of
    the same amount (Strong: the drawdown or contract number in the description; Weak: near the due date) and
    records it on the statement date, cleared and reconciled (needs the Facility Repayment submit permission).
    A debit of another amount (extra charges) is recorded by hand.
  - *PDC to Lender* (company currency only): *Generate Installment Cheques* prepares a Cheque Batch with one
    cheque per open installment to the lender, for its unpaid amount, dated its due date. **No double
    posting**: an installment cheque posts to the party on its cheque date whatever the settings say, and
    that party posting *is* the repayment — one Payment Entry from PDC Payable (Dr Liability, Dr Interest /
    Cr PDC Payable) and a Facility Repayment settled by the cheque; the cheque's clearance then moves PDC
    Payable to the bank. A bounce / return / replacement undoes the repayment (the installment is unpaid
    again); cancelling or replacing the cheque frees the installment. An installment with a live cheque is
    paid by that cheque only (Oldest First skips it; no prepayment while later installments have cheques).
    These cheques are left out of the Projected Party Statement (they settle the facility, not the supplier).
- **Security cheque to the lender**: *Create → Security Cheque* on the facility prepares an outgoing security
  cheque (Facility Collateral) to the lender for the total limit, until the facility's expiry, linked to the
  facility's Security Cheques on save (no accounting).
- **Reports**: Cost of Financing splits Interest, Profit, Ijarah Rentals (Profit), Commission, Fees,
  Penalties and Late Payment (Charity) and includes the installment cheques' postings; the utilisation and
  schedule reports show the lender (filter *Lender Type*), the schedule report the repayment mode, the
  installment cheque and its status and the payment voucher.

**Letters of Guarantee and Letters of Credit (shared rules)**

- Both are submittable documents under a facility's *Letter of Guarantee* / *Letter of Credit* sub-limit.
  Company, bank and currency come from the facility; the bank account (default: the facility's) is in the
  facility currency. **Submit = issue / open**: the facility row is locked, the facility must be Active on
  the issue date, and the amount must fit the available sub-limit and total. Journal Entries (cleared on
  their date): cash margin **Dr Margin Account / Cr Bank** (margin = amount × *Cash Margin (%)*, default from
  the sub-limit; the margin account is an Asset account in the facility currency) and commission **Dr
  Commission Account / Cr Bank** (*Commission Rate (%)* × amount when a rate is set, else the amount
  entered).
- After submit, the status (and the open amount, margin held, dates) changes **only through the actions**
  (Treasury Manager + write permission); every action appends a row to the *Bank Instrument Status Log*
  (event date, from / to status, open amount after the event, voucher). Foreign currency: the margin is
  carried at the issue rate; the bank moves at the event's rate; the difference goes to the company's
  Exchange Gain / Loss account.
- Utilisation = the open amount of the open documents (for a past date: read from the status log). The
  daily job sets documents past their **effective** expiry date to Expired (no accounting). Cancel cancels
  every voucher of the document (not once Claimed / Settled).

**Letter of Guarantee**: LG type (Bid Bond, Performance, Advance Payment, Retention, Financial, Customs,
Other), beneficiary (Customer / Supplier / Other), reference document, optional read-only link to ERPNext's
own *Bank Guarantee* (never changed). **Off-balance sheet**: the guaranteed amount itself is never booked.
Actions: *Extend* (later expiry, optional commission), *Reduce* (margin released pro rata), *Release* (whole
remaining margin back), *Claim (Liquidation)* (Dr the chosen claim / party account; Cr Margin first, Cr the
LG sub-limit's Liability Account for what the bank paid beyond the margin), *Repay Claim* (Dr Liability /
Cr Bank). Expired guarantees keep their margin until released; the facility digest lists them.

**Letter of Credit (import)**: Sight / Usance, supplier (+ Purchase Order), tolerance %, latest shipment
date. Actions: *Amend* (amount / expiry / charges; an increase is checked against the limit; the margin
follows the amount), *Receive Documents* (documents amount ≤ amount + tolerance), *Accept* (Usance: maturity
date), *Settle* — the whole margin returns to the bank (Dr Bank / Cr Margin) and a standard **Payment
Entry** pays the supplier from the bank (optionally allocated to a Purchase Invoice; ERPNext books the
exchange difference). Both vouchers are cleared on the settlement date: match the bank statement's single
net debit against both. A settlement below the credit amount records the *Unused Amount*, released from the
facility. *Release Margin* for expired credits. An accepted Usance credit is an expected outflow on its
maturity date in the Bank Position Forecast.

**Cheque Discounting (خصم أوراق تجارية)**: a submittable batch under the *Cheque Discounting* sub-limit (which
needs a Liability Account, "Discounted cheques"). Rows: incoming PDC Cheques that are Received and Party
Posted (in PDC Receivable), due after the discount date, in the facility currency. Charges per cheque =
amount × rate / 100 × days to its due date / 365 or 360; commission = sub-limit rate × total unless set
manually. **Submit** (Treasury Manager): Dr Bank (net) + Dr Discount Expense / Cr Discounted Liability
(total); each cheque becomes **Discounted** (still Party Posted) and leaves the expected inflows. **Paid at
maturity** (*Settle Cheques* on the batch or on the cheque, not before its due date): Dr Discounted Liability
/ Cr PDC Receivable, the cheque becomes Cleared / Bank Posted. **Bounce** (recourse): Dr Discounted
Liability / Cr Bank, then the party posting is undone per *On Bounce*. Cancel only while no cheque is
settled or bounced.

#### Bank Transfers

A **Bank Transfer** is a transfer order: a **Party Payment** (any Party Type), a **Party Receipt**, or an
**Internal Transfer** between two company bank accounts, by method **Normal**, **Fawran** or **SWIFT**. It is
posted as **one Payment Entry** on its **execution date** (the value date), with the bank fee inside it:

| Type | Payment Entry | Bank | Party / target |
|---|---|---|---|
| Party Payment | Pay | Cr amount + fee | Dr amount (allocated to the references, the rest an advance) |
| Party Receipt | Receive | Dr amount − fee | Cr amount |
| Internal Transfer | Internal Transfer | Cr amount + fee | Dr amount (or the received amount) on the target bank |

The fee is a deduction row on its expense account (company currency; a foreign-currency fee is converted at
the transfer's rate). A rounding gap of up to 0.05 (company currency) between the separately rounded bank,
party and fee amounts is absorbed by the fee row — both a difference amount and, when the principal is fully
allocated to the references, a tiny unallocated amount (which would otherwise stay as a 0.01 advance).

**Fee rules** (Treasury Company Settings, *Transfer Fees*): per bank account, method, direction (Outgoing /
Incoming: the fee our bank takes from a received transfer), scope and amount range (bank account currency;
*Amount To* 0 = no upper limit): fixed fee + percentage, min / max, and for SWIFT the charge bearer and a
correspondent fee added for OUR. The exact scope wins over *Any*, an exact charge bearer over an empty one,
then the lowest row; overlapping rules are rejected. SWIFT with BEN books no fee (the beneficiary bears it).
The user may override the fee with a reason (logged as "Fee Overridden").

**Scope**: SWIFT is International; otherwise Same Bank when both sides are at the same Bank (the Bank links,
else the bank codes), else Other Bank (also when nothing is known: the fee applies). An IBAN carries the bank
code in characters 5-8 (e.g. `QA58DOHB…` = DOHB); IBANs are compared only within one country, an IBAN with
a SWIFT code only for Qatar (the SWIFT code starts with the bank code), two SWIFT codes by their first four
letters.

**Limits** (*Transfer Limits*): per-transaction and daily limits of a bank account and method (the daily
limit counts the submitted, not cancelled transfers of the same execution date). *Transfer Limit Action*
Warn shows a message; Block rejects the transfer. The limits are enforced on the server in **every** path
that posts or moves a transfer: submit, **Send Now** (on the send date), **Reschedule** (on the new date) and
the daily job (a blocked transfer stays Scheduled and is logged). Each of them first locks the Bank Account
row and then counts the day's transfers with a locking read, so two concurrent submits cannot both pass a
daily limit (the second waits for the first and sees it).

**Approval**: above the *Transfer Approval Threshold* (company currency) only a Treasury Manager may submit.
The value compared is what leaves the bank account — amount **plus fee** (an overridden fee as entered) for a
payment or internal transfer, the amount for a receipt — converted at the **higher** of the transfer's
exchange rate and the system exchange rate (Currency Exchange) of the execution date: a low rate typed on the
transfer can never bring it under the threshold (for an internal transfer into a company-currency account
the received amount counts too). Without a system rate for that date the value cannot be verified and only a
Treasury Manager may submit. The rate typed on the transfer is still the one the Payment Entry is posted at.

**Permissions of the form helpers**: the fee preview, the transfer settings and *make_bank_transfer* answer
only for a company the user may prepare Bank Transfers of (their User Permissions on Company apply). A
party's bank details (its default bank account, IBAN, bank) are returned by the preview, pre-filled by
*make_bank_transfer* and copied on save only when the user may read that party; otherwise the user enters the
beneficiary IBAN / bank account. *make_bank_transfer* rejects an invoice whose party currency is neither the
bank account currency nor the company currency (e.g. a USD supplier paid from an EGP bank).

**Statuses**: Draft → *Scheduled* (future execution date; the daily job sends it on its date, catching up
missed days; **Send Now** / **Reschedule**) → *Sent* (Payment Entry posted) → *Completed* (the bank
confirmed it: **Mark Completed** or the PDC Bank Clearing tool; the Payment Entry gets its clearance date).
*Transfer Completion* can complete on send (all, or Fawran only). **Mark Returned** (Treasury Manager; not
for a receipt — refund it with a new Party Payment) reverses the Payment Entry on the return date: an
Internal Transfer without fee by a reverse Payment Entry, anything else by a Journal Entry; the fee stays
booked unless *Refund Fees on Return* is set. The return date cannot be in the future. **Cancel** a Scheduled or Sent transfer (its Payment Entry is
cancelled); a Completed or Returned one is returned instead. Every change is in the status log.

**Internal Transfer between two currencies**: the rates are derived from the bank's own conversion (amount →
received amount), so the Payment Entry books no exchange difference; later rate movements are left to
Exchange Rate Revaluation.

**Statement matching** (PDC Bank Clearing): a Sent transfer is matched on its bank's statement line of the
full bank amount (fee included; strong when the line shows its bank reference or name). Clearing completes
it and reconciles the line; the target bank's deposit of an internal transfer is reconciled and logged
("Target Confirmed"). A statement showing the principal and the fee as two lines is reconciled in ERPNext's
Bank Reconciliation Tool.

Reports: **Bank Transfer Register** and **Transfer Fees Analysis** (per month, bank, bank account or method,
with a chart). Scheduled transfers are expected flows of the Bank Position Forecast; Sent ones not yet
confirmed show as a summary card (they are in the GL already).

#### Bank Charges

A **Bank Charge** records a charge the bank debited to a company bank account (account maintenance, cheque
book, SMS alerts, returned cheque ...) or, with *Entry Type* **Refund**, money the bank gives back. Each
charge has a **Bank Charge Category** (Treasury Setup), which gives its expense account (and cost center)
per company, a default description and the statement **keywords** that propose it. Six categories are created
once on install / migrate (Account Maintenance, Cheque Book, SMS Alerts, Returned Cheque, Transfer Fees,
Other), with the Bank Charges Account of the company's PDC Accounts when the company maps exactly one; a
category you delete is not created again.

**Voucher** (Treasury Company Settings → *Bank Charge Voucher*):
- *Payment Entry* (default): a Pay Payment Entry to the Bank's Supplier with the whole amount as one
  deduction on the expense account. It books only Cr Bank / Dr Expense: no party row and no Payment Ledger
  entry, so the supplier's balance and the payable reports are never affected. The Payment Entry takes the
  Mode of Payment and series of the Modes of Payment map (method *Bank Charge*). A refund is a Receive with
  a negative deduction (Dr Bank / Cr Expense).
- *Journal Entry*: one Bank Entry, Dr Expense / Cr Bank (a refund: the reverse).

**Bank Supplier**: Treasury Settings → *Bank Suppliers* maps each Bank to the Supplier its charges are paid
to. On the first charge of a bank without a row, an enabled Supplier named like the Bank is reused, or one is
created in the Payee Supplier Group, and the row is added. A Bank Account without a Bank can only book its
charges by Journal Entry.

**Clearance** (*Bank Charge Clearance*): *On Posting* clears the voucher on its posting date; *On Bank
Transaction* leaves it uncleared until it is reconciled with its statement line (PDC Bank Clearing, or a
Bank Transaction set on the draft charge, which is then reconciled on submit).

**Refunds**: *Record Refund* on a submitted charge prepares a refund for what is left; a refund uses the
charge's category and expense account and all refunds together cannot exceed the charge. A charge with
submitted refunds cannot be cancelled before them. **Cancel** cancels the voucher (Treasury Manager);
ERPNext then un-reconciles its statement line.

**Statement matching** (PDC Bank Clearing): a Posted charge is matched like a transfer (strong when the line
shows its bank reference or name, weak by its posting date). A withdrawal left unmatched whose description
or reference contains a category keyword (at least 3 characters, not case sensitive, the longest keyword
wins) is proposed as a *Keyword* row; clearing it creates, submits and reconciles the charge in one step, row
by row or in bulk. A tie between categories, or a category with *Pre-select Clearing Proposals* off, is
proposed unselected; choose the category in the row. The *Document* column shows the cheque, transfer or
charge of each row (*+ New Bank Charge* for a proposal); the buttons above the grid select all rows, the strong
matches only or the charge proposals, and *Set Charge Category* sets one category on every selected proposal.
The grid description counts what *Clear Selected* will do, and its confirmation lists the charges to create per
category.

**On the form**: a saved draft says what Submit will post (voucher, date, bank debit or credit, expense
account, clearance); a submitted charge heads with its Payment Entry (or Journal Entry) number, its clearance
or statement line and its refunds. *Reconcile with Statement Line* (Actions) reconciles one Posted charge
without the clearing tool. The list shows refunds with ↩ in green, the voucher of Journal Entry-mode
companies, and the *Refunded* / *Partly Refunded* states.

> **Warning**: a bank that shows a transfer's fee as its own statement line would get that fee proposed as a
> new charge, although the transfer already booked it. The default *Transfer Fees* category is therefore
> never pre-selected: do not create charges for those lines.

Report: **Bank Charges Analysis** — every bank charge booked by Treasury (bank charges and refunds, transfer
fees, cheque clearance charges, bounce charges recovered, LG / LC commissions and charges, facility fees) per
month, bank, bank account, category or source, with a chart and a detail view linking each voucher. Cheque
discounting charges and facility interest stay in Cost of Financing.

#### Bank Position Alerts

Treasury projects every bank's balance day by day over a short horizon (the Bank Position Forecast engine,
per bank) and warns before a bank runs short. It posts nothing.

- **Settings.** *Treasury Settings → Bank Position Alerts*: horizon (today and the next days, 1–7), digest
  hour (system time zone), daily digest and critical alerts on / off, the channels (in-system notification,
  e-mail — only with a default outgoing e-mail account), whether the digest lists days without movements,
  and the **Recipients** (roles and / or users; install adds *Treasury Manager* once). *Treasury Company
  Settings → Bank Balance Alerts*: **Balance Thresholds** per company bank account (Warning and Minimum
  Balance in the bank account currency; negative values allowed for overdraft accounts) and **On Low
  Projected Balance** (Ignore / Warn / Block, default Warn). Banks without a threshold row are listed in the
  digest but never alerted or checked. Bank Accounts sharing one GL account are one position (their money
  cannot be split) with the strictest thresholds.
- **Levels.** A day whose projected closing is below the minimum is *Critical*, below the warning balance
  *Warning*. The projection equals the report's Daily *Projected Closing Balance* (Bank Currency, Include
  Overdue): today's closing includes the overdue expected flows.
- **Daily digest** (hourly job, from the digest hour on, once per recipient, company and day): one table per
  bank (Critical first) with today's actual GL balance, the thresholds and one row per day (in, out,
  projected closing, status), plus a link to the bank's forecast. Each recipient only sees the companies and
  bank accounts their permissions and User Permissions allow, in their own language.
- **Critical / warning alerts** (hourly and right after every guarded entry): one alert per bank for its
  breached days, with the largest expected payments; a breached day is alerted once per day and again only
  when it gets worse ("Worse:"), or when a Warning day turns Critical (a Critical day improving to Warning
  sends nothing that day). Each recipient sees only the payments they may read; the others appear as one
  "Other payments / receipts" total. An alert nobody could receive is tried again at the next run.
- **Entry-time check.** Delivering an outgoing cheque (single, Cheque Batch submit / Deliver All, security
  cheque encashment), submitting / sending now / rescheduling a Bank Transfer, submitting a Facility
  Repayment or a Bank Charge compares the bank's projection before and after the entry. A day that the entry
  lowers below the minimum is reported: *Warn* shows a message, *Block* refuses the entry unless a Treasury
  Manager confirms the override (the *Override Low Balance* box of the document, which only Treasury and
  System Managers can set, or the confirmation of the action), which is logged. A batch is checked once and refused as a whole. Bank Charges created from a
  statement line only warn (the bank already took the money); scheduled system postings and cancellations
  are never checked.
- **Treasury Alert Log** keeps the digests, alerts and overrides (the dedupe keys; digests and alerts are
  cleared after 180 days by Log Settings, the override logs are kept permanently and cannot be deleted). Report **Bank Balance Outlook** lists every bank's current and lowest projected balance,
  level, first breach and shortfall; number card *Banks Below Minimum (7 days)* and chart *Lowest Projected
  Balance per Bank* are on the workspace.
- **Manager tools.** Treasury Settings, Treasury Company Settings and the Bank Balance Outlook menu offer
  *Preview Digest* (what an enabled System User would receive: their banks, limited to the banks you may
  see yourself, in their language; nothing is sent),
  *Send Test to Me* (the digest e-mailed to yourself, subject "[Test]"; the day's real digest still goes out)
  and *Run Alerts Now* (queues the alert check; alerts already sent today are not repeated). *Add All Bank
  Accounts* on the thresholds grid adds one row per company bank account: type only the minimums.
- **Block dialog.** A blocked entry shows the bank, the day, the balance before and after and the minimum; a
  Treasury Manager gets *Override and Continue* (actions) or *Override and Submit* (documents), recorded in
  the status log or timeline, the Treasury Alert Log and a line on the document.

#### Pay or receive from any screen

A **Treasury** button group on Sales / Purchase Invoice, Sales / Purchase Order, Journal Entry, Customer,
Supplier, Employee, Quotation, Supplier Quotation and (with HRMS) Expense Claim / Employee Advance opens a new,
**unsaved** Treasury document prepared from the one on screen: *Pay / Receive by Cheque* (PDC Cheque),
*Bank Transfer*, *Fawran*, *Installment Cheques* (Cheque Batch) and *Security Cheque* (a tender bond on a
quotation, a performance bond on an order). Nothing is written until the user saves it, through the normal validations; the buttons are hints
only (the server checks the permissions and the document again).

- **Amount**: what is still outstanding on the invoice / order / Journal Entry / Expense Claim, less what open
  (not posted) cheques and Scheduled transfers already allocate to it, allocated to it as a reference. A
  document already covered is refused, and so are returns, invoices on hold, closed / on hold / completed or
  fully billed orders. A party master opens an empty document (advance).
- **Bank and cheque book** (*Treasury Company Settings → Pay / Receive from Documents*): the default bank of
  the direction when it can settle the party's currency, else the first *PDC Accounts* bank in the party's
  currency (outgoing: else the only mapped bank); with *Use the Bank's Active Cheque Book* an outgoing cheque
  takes the next leaf of the bank's oldest active cheque book **when it is saved**. So an outgoing cheque from
  an invoice needs no typing at all, an incoming one only its cheque number.
- **Installment Cheques**: count and frequency from the settings, the outstanding split evenly. The batch
  remembers the document it was created from (*Created From*) and **Generate Cheques** allocates the cheques
  to it, earliest due first (the rest stays an advance; a batch in another currency than the party account is
  allocated by hand).
- **Journal Entry**: one of its open parties (asked when there are several). **Expense Claim / Employee
  Advance**: only where HRMS is installed and its Payment Entry accepts them as references.
- **Tender bond** (Quotation / Supplier Quotation only): *Tender Bond %* of the quotation's grand total
  (default 0 = enter the amount); a quotation to a Lead becomes a security cheque of party type *Other*.
- **Performance bond** (Sales Order / Purchase Order): *Performance Bond %* of the order's grand total
  (default 10; 0 = enter the amount), with the remark "Performance bond 10% for Sales Order …". Security
  cheques never post.
- Treasury always settles on the party's **default** party account: a Journal Entry row, Expense Claim or
  Employee Advance booked on another account (e.g. an employee advance account) is refused ("Settle it from
  ERPNext"). With *Book Advance Payments in Separate Party Account*, order advances are posted as in 1.0 (on
  the default party account).
- **On screen**: the labels say the direction (*Pay by Cheque* / *Receive by Cheque*; on a Journal Entry
  they follow its open parties, and the group is hidden when nothing is left to settle) and the kind of bond
  (*Security Cheque (Tender Bond / Performance Bond / Received / Custody)*). The opened document shows in a
  blue intro what is left to type (the cheque number, the amount of an advance, the First Cheque No of a
  batch ...). A quotation lists its tender bonds under its dashboard. Refusals come in a dialog titled
  *Treasury*. The "+" of the Treasury connections runs the same buttons; there is none beside vouchers or
  facility instruments (they are only created by posting). A batch's *Created From* shows as one row, is
  printed as *Against* on the Cheque Batch Receipt, and is dropped (with an alert) when the draft's party or
  company changes.

**Connections**: those documents (and Payment Entry, Journal Entry, Bank Account) show a *Treasury* group in
their Connections with the cheques, batches, transfers, security cheques (and on a Bank Account the facility
documents) linked to them; the Treasury documents show their vouchers (*Accounting*) and each other (facility
↔ drawdown ↔ repayment, facility ↔ security cheque, discounting ↔ cheques). Counts of references and vouchers
have no document type filter (names are unique per series); an incoming transfer's target bank is not counted
on the Bank Account.

#### Treasury Dashboard

The page **Treasury Dashboard** (`/app/treasury-dashboard`, the first shortcut of the Treasury workspace and
of its sidebar) shows the treasury of one company at a glance. It only reads data: one call
(`treasury.pdc.page.treasury_dashboard.treasury_dashboard.get_dashboard_data`, service
`treasury.pdc.overview`) built on the reports' own code, so every figure equals the report or list it opens
when clicked (with the same filters).

- **Filters**: Company, Bank Accounts (several; a bank sharing its GL account with a selected one is added,
  as in Bank Balance Outlook, unless a User Permission hides it from the user), Horizon 7 / 14 / 30 days. The last filters are remembered in the browser.
- **Bank positions** (bank filter applies): *Cash Today* and *Projected in {H} Days* (the closing at the end
  of the chosen horizon H, company currency),
  *Banks Below Minimum* / *Below Warning* (Bank Balance Outlook for the horizon), one row per bank (level,
  current, lowest and closing balance in the bank currency — equal to the Bank Balance Outlook row), the
  projected balance curve and the daily (weekly for 30 days) inflows / outflows. The positions come from one
  30-day Bank Position Forecast engine call: a day's projected closing never depends on the horizon.
- **Cheques** (bank filter applies): due this week (today to today + 7, the number-card convention) outgoing /
  incoming, post-dated issued / in hand (the *Outstanding PDC* cards) and bounced this month (Bounced and
  Postponed Cheques, one row per bounce).
- **Transfers awaiting confirmation** (Sent, from or to the selected banks) and **bank charges this month**
  (Bank Charges Analysis net, by category).
- **Company-wide** (never filtered by bank; the page says so when banks are selected): security cheques held /
  given (Security Exposure), facilities with their utilisation (Facility Utilisation, at most 20 facilities,
  "and N more"), overdue and next installments (the facility cards), Letters of Guarantee / Credit expiring
  within 30 days (LG / LC Register, *Expiring Within (Days)* 30) and the 30-day maturities chart by category.
- **Setup banner**: the number of Setup Checklist items missing (red while a *Required* one is), for users who
  may read the company's Treasury Company Settings (the "Create Company Settings" button only for users who
  may create them).
- **Permissions**: the page is open to Treasury Manager / User, Cheque Approver, Accounts Manager / User and
  System Manager. A section whose DocType the user may not read for the company (or that a User Permission
  forbids, e.g. on a Bank Account) is left out; a user who may read none gets a permission error. Amounts in the
  company currency use today's rate.

### Reports and workspace

The **Treasury** workspace (sidebar + desktop icon; shown to Treasury Manager / User, Accounts Manager /
User and System Manager) opens with the **Treasury Dashboard** shortcut (page `treasury-dashboard`) and holds
the number cards, the *Expected Cash Flow*, *PDC by Status* and *Lowest Projected Balance per Bank* charts,
the report links and the **Treasury Setup** card (the setup masters in setup order: Treasury Company
Settings, Treasury Settings, Mode of Payment, Cheque Book, Cheque Layout, Bank Charge Category, Custody
Location, Document Naming Settings, Document Naming Rule). The sidebar has sections for the security cheques
and the bank facilities. Reports only read data, with the user's permissions and User Permissions (a user
restricted to other companies gets a permission error for a forbidden Company filter). Company currency
amounts of other currencies use today's rate.

| Module | Reports | Number cards |
|---|---|---|
| PDC | Bank Position Forecast, Bank Balance Outlook, Projected Party Statement, PDC Register, PDC Maturity, Bounced and Postponed Cheques, Cheque Book Usage, Cheque Leaf Register | Outgoing / Incoming Cheques Due Today, Due in 7 Days, Overdue; Outstanding PDC Payable / Receivable; Banks Below Minimum (7 days) |
| Bank Transfers | Bank Transfer Register, Transfer Fees Analysis, Bank Charges Analysis | — |
| Security Cheques | Security Cheque Register, Security Cheques Summary, Security Cheque Movements, Security Exposure, Security Cheques by Custody, Expired Security Cheques Not Returned | Security Held, Security Given, Security Cheques Expiring in 30 Days, Expired Security Cheques Not Returned |
| Bank Facilities | Facility Utilisation, Facility Repayment Schedule Report, Cost of Financing, LG Register, LC Register, Discounted Cheques | Total Facility Limits, Total Facility Utilised, Installments Due in 30 Days, Overdue Facility Installments, Auto Debits Awaiting Statement, Open Letters of Guarantee, Open Letters of Credit, Discounted Cheques Outstanding |

- **Page**: *Treasury Dashboard* (see [Treasury Dashboard](#treasury-dashboard)).
- **LC Register**: *Expiring Within (Days)* narrows to the Opened / Amended credits expiring by today + that
  many days (as in LG Register). **Bounced and Postponed Cheques**: *Bank Account* filter.

- **Bank Position Forecast**: opening and actual movements come from the bank GL only (it moves on the bank
  posting of a cheque); every cheque whose bank posting is still to come is an expected flow on its
  effective (possibly postponed) due date — or, for a cheque cleared but not bank posted yet (Manual mode),
  on the bank transaction / clearance date. Past-dated flows form the *Overdue* row (counted as today);
  with a From Date after today, the flows from today up to it form a *Brought Forward* row. Other modules
  add flows through the `treasury_forecast_sources` hook (see `treasury/pdc/forecast_sources.py`):
  facility installments and accepted Usance LCs. Discounted cheques are never expected inflows.
- **Projected Party Statement**: the party's GL statement plus the unposted / uncleared cheques as if paid
  on their due date (actual and projected balance columns); the exposure summary counts every unposted
  cheque of the party, whatever its due date.
- **Bounced and Postponed Cheques**: a bounce is dated by the bounce date the user entered (status log
  *Event Date*); the postponement view counts the party's postponements.
- **Number cards vs. the forecast** (deliberate): the cards count cheques still in circulation — outgoing
  Created + Delivered, incoming Received + Deposited. "Due in 7 Days" means today to today + 7, the same
  convention as the *0-7 Days* bucket of PDC Maturity.
- **Security Exposure**: held from others vs given to others, by party or by reference document with
  Required / Actual / Shortfall (a document's requirement is the highest Required Amount on its cheques;
  Open / Blank cheques are counted, never summed — "Covered by Open / Blank").
- **Cost of Financing**: interest, commission, fees and penalties per facility and month / quarter / year,
  from the GL (facility-linked vouchers only).
- **Cheque Leaf Register**: every leaf of the outgoing cheque books (at most 10,000 per run; view *Books* for
  the totals only): Unused, Written, Issued (PDC), Cleared, Bounced, Replaced, Cancelled Leaf, Void or the
  status of the security cheque on it, with party, amount, dates, Payment Entries and the document. A number
  typed by hand on a cheque without a book belongs to the book of the bank account whose range covers it (a
  book that is not Closed first, then the newest). Leaf statuses are shown to whoever may read the books; the
  details of a cheque the user may not read stay blank. *Cheque Book Usage* counts void leaves in their own
  column.
- **Security Cheques Summary**: held from others / given to others per party, purpose or status, with the
  live cheques aged by expiry date (Expired, configurable ranges such as 0-30 / 31-60 / 61-90 / 91+, No
  Expiry) and, when selected, the amounts of closed cheques. **Security Cheque Movements**: every status and
  custody movement in a period (by event date, else by the date it was recorded), a custody change shown on
  its status row.

### Roles and permissions

- **Treasury Manager**, **Treasury User** and **Cheque Approver** are *process* roles: they give access to
  the Treasury doctypes and to the actions. The actions check the user's permission on the document and,
  where required, the role:

  | Action | Role |
  |---|---|
  | Create / deliver / receive / post to party / deposit / clear / post to bank / return a PDC Cheque; Cheque Batch; security cheque create / receive / issue / custody / return / cancel / extend expiry; draft Cheque Discounting | Treasury User (write permission) |
  | Postpone a PDC Cheque | Cheque Approver or Treasury Manager (the approver needs only read access) |
  | Bounce, Re-present, Replace, Cancel a PDC Cheque; cancel a Cheque Batch; security cheque Mark as Lost / Encash / Open-Blank cheques; Cheque Discounting submit / cancel / settle; every Bank Facility, LG and LC action | Treasury Manager |
  | Treasury Settings / Treasury Company Settings | System Manager, Treasury Manager (Treasury User reads) |
  | Treasury Dashboard page | Treasury Manager, Treasury User, Cheque Approver, Accounts Manager, Accounts User, System Manager; each section only for users who may read its DocType for the company (the others are hidden) |

- **Elevated posting.** The Payment Entries and Journal Entries are created, submitted and cancelled by the
  posting service (`treasury/accounting/posting.py`) in an elevated context, only after the permission and
  role check on the source document. A Treasury user does not need (and the app does not grant) any
  permission on Payment Entry, Journal Entry, Account, Bank Transaction, etc. The vouchers' `owner` /
  `modified_by` is the user who ran the action (Administrator for scheduled postings); their GL entries and
  version history show Administrator.
- **Masters still need read access.** To pick a Customer / Supplier / Bank Account / Company in the forms
  and to open the linked vouchers, users need read access to those masters through a **standard ERPNext
  role** (e.g. *Accounts User*) or a role of your own. User Permissions (e.g. per company) are respected.
- The app changes no standard permission of ERPNext doctypes and creates no Custom DocPerm. Development
  versions granted the Treasury roles read access to some core doctypes through Custom DocPerm; the first
  `bench migrate` removes those rows once and resets doctypes whose permissions were only a copy of the
  standard ones.

### Scheduled jobs

| Frequency | Job | What it does |
|---|---|---|
| daily_long | `treasury.pdc.scheduler.post_due_party_postings` | party postings of cheques in *On Cheque Date* mode due up to today (catches up missed days; one cheque at a time, errors logged once a day) |
| daily_long | `treasury.pdc.scheduler.send_due_alerts` | digest of the cheques due within the alert days, per company and user, in the user's language |
| daily_long | `treasury.bank_transfers.scheduler.post_scheduled_transfers` | sends the Scheduled bank transfers due up to today (catches up missed days; a transfer blocked by a limit stays Scheduled and is logged) |
| daily_long | `treasury.bank_facilities.scheduler.refresh_facilities` | facility / LG / LC expiry, installment statuses (Overdue), displayed utilisation |
| daily_long | `treasury.bank_facilities.interest.post_interest_accruals` | month-end interest accruals (when enabled) |
| daily_long | `treasury.bank_facilities.auto_debit.post_due_auto_debits` | repayments of the Auto Debit installments due (setting *On Due Date*; catches up missed days on their due dates) |
| daily_long | `treasury.bank_facilities.scheduler.send_facility_alerts` | installments due / overdue, facilities and guarantees expiring |
| daily | `treasury.security_cheques.scheduler.send_security_expiry_alerts` | security cheques expiring or expired and not returned |
| hourly | `treasury.balance_alerts.scheduler.run_hourly` | bank position digest (once a day from the digest hour) and critical / warning balance alerts |

Scheduled postings run as Administrator; alerts go to Treasury Manager / User as notifications and, if outgoing email is
set up, by email, listing only the documents each recipient may read.

### Known limitations and follow-ups

- **PDC**: *Endorsed* is a status the reports understand, but there is no endorsement action yet
  (endorsing a received cheque to a supplier). No list-view bulk
  actions (bulk deposit / clear): use *PDC Bank Clearing* or the Cheque Batch actions. No approval workflow
  for postponement / cancellation beyond the role checks. No customer / supplier portal.
- **Cheque Batch**: party cheques only; one reference per row; FIFO needs the party account in the cheque
  currency; at most 120 cheques per batch; the batch link on a cheque is set only by the batch.
- **Pay / receive from documents**: installments are not prefilled from an invoice's Payment Schedule; an
  Employee Advance booked on an advance account cannot be paid from Treasury (no custom party account yet);
  a supplier on payment hold is refused by ERPNext only when the cheque is posted; a tender bond is not
  re-linked when its Lead becomes a Customer.
- **Security Cheques**: encashing against a claim or penalty income account by Journal Entry is not
  implemented (an encashment settles the party's invoices or stays an advance); encashment for Other
  parties, alerts before the end of the linked contract, an approval workflow for Open / Blank
  cheques and memo (off-balance-sheet) accounts are follow-ups.
- **Bank Facilities**: a variable rate is informational only (nothing is repriced when the reference rate
  moves); a prepayment reduces the installment, never the tenor, and a Murabaha profit is not recomputed;
  overdraft interest is not accrued (book it from the bank statement) and overdraft utilisation is not
  covered by the concurrency stamp; import loans are financed by a Trust Receipt at the LC settlement (the
  margin held is used first); a settled Letter of Credit (financed or not) cannot be cancelled;
  Sight LCs are not forecast; no print formats for LG / LC / Cheque Discounting (the standard print view
  applies); discounted cheques are settled by a user action (no daily job). Phase 7g: Operating Ijarah, a
  Murabaha profit recomputation, installment cheques to the lender and Payment Entry vouchers of foreign
  currency facilities, Invoice Discounting charges in Cost of Financing, an auto debit of another amount
  than the schedule's (record it by hand) and a tenor-reducing prepayment are not supported.
- **Treasury Dashboard**: the security, facility and LG / LC sections are company-wide (those documents are
  not per bank account); company currency figures and charts are converted at today's rate; at most 20
  facilities are listed; no auto-refresh (click *Refresh*); the reports taking a single bank open for all
  banks of the company when several banks are selected.
- **Naming**: Treasury adds no naming mechanism. A Document Naming Rule counter is one number that does not
  restart each year; the direction series of PDC Cheque / Cheque Batch / Security Cheque can be changed only
  through Document Naming Rules (their controllers pick the series by direction).
- **Translations**: Arabic only. The core Arabic wording of many generic financial words is wrong for a
  treasury ("Amount" = quantity, "Issue Date" = the date of a lawsuit, "Total" = total excluding tax ...),
  and a context-less entry in the app would replace it in every ERPNext screen. So the app never translates
  such words without a context (`msgctxt`): field / section labels and Select options have entries under
  their DocType name (the context Frappe itself uses for them), and the generic words of reports, dialogs,
  print formats and alert emails use the context `Treasury` (`_("Amount", context="Treasury")`,
  `__("Amount", null, "Treasury")`); older report-only words keep the context `Treasury Report`. Only
  app-specific sentences and a few PDC status words (Received, Delivered, Returned ...) keep a plain entry.
  Limitations: the few Frappe screens that translate a label without its DocType context (e.g. the Form
  Builder of Customize Form) show the core wording of generic labels, and a label added later through
  Customize Form has no Arabic entry until one is added to `locale/ar.po`.
- **Uninstall**: `bench uninstall-app treasury` removes the app's DocTypes, reports, workspace and its
  custom fields on Payment Entry / Journal Entry, and leaves the Treasury roles. As usual in Frappe, the
  database columns of deleted custom fields (`treasury_*` on `tabPayment Entry` / `tabJournal Entry`) are
  kept (empty) to protect data; they are reused if the app is installed again.

#### Uninstall: what is kept

Besides the roles, uninstalling keeps on purpose what has become the company's own data: the Modes of
Payment created by *Generate Modes of Payment*, the Payment Entry naming series added by *Add Series to
Payment Entry* (Property Setters of Payment Entry; the uninstall prints the list, remove them in Document
Naming Settings if not needed), the *Treasury Payees* Supplier Group with its suppliers and the Series
counters.

Deleted with the app's doctypes (they would be orphans): every **Property Setter** (e.g. series options set in
Document Naming Settings), **Document Naming Rule** and **Custom Field** (e.g. the accounting dimension fields
ERPNext added) that targets a Treasury DocType — `bench uninstall-app` itself does not delete those without a
module. Property Setters on core doctypes (the Payment Entry series) are kept.

Not deleted (remove them by hand before uninstalling if you made any): other customisations that point to a
Treasury DocType — Client Scripts, Server Scripts, Custom DocPerms (role permissions changed in Role
Permissions Manager), Workflows, Notifications, non-standard Print Formats / Reports, and User Permissions
whose *Allow* is a Treasury DocType. They stay as orphan records that no longer do anything.

### Testing

```bash
bench --site <test-site> set-config allow_tests true
bench --site <test-site> run-tests --app treasury
```

938 tests in the last full run before 1.1.0 (`frappe.tests.IntegrationTestCase`; phase 7h adds the dashboard,
setup checklist, naming and release tests), independent of the execution order: each
test file builds its own fixtures (test company in EGP, EGP and USD bank accounts, PDC account mapping,
customer, supplier, invoices — `treasury/tests/utils.py`) and everything is rolled back. They assert exact
GL rows, Payment Ledger outstanding, both statuses and idempotency for every posting-mode combination,
security cheques leaving every ledger untouched, reports, alerts, translations (`test_translations.py`:
valid `ar.po`, no duplicates, every `_()` / `__()` string translated, every DocType label under its DocType
context, no plain generic core words in `ar.po` nor treasury terms without context in the code) and the
install / uninstall field list (`test_install.py`). One scheduler test is skipped when `freezegun` is not
installed.

### Contributing

This app uses `pre-commit` (ruff, eslint, prettier, pyupgrade):

```bash
cd apps/treasury
pre-commit install
```

Every user-facing string is wrapped in `_()` / `__()` and translated in `treasury/locale/ar.po`; all GL
effects go through `treasury/accounting/posting.py`; patches go in `patches.txt`.

### License

MIT
