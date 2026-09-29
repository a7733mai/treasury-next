## Treasury

Post-dated cheques, security cheques, multi-cheque vouchers and bank facilities for ERPNext v16.

**English.** Treasury manages the cheques a company writes and receives (post-dated cheques with their full
lifecycle and configurable posting events), multi-cheque vouchers (e.g. 12 rent cheques in one document),
guarantee cheques that never touch the ledger, and bank credit facilities (limits, loans, Murabaha,
Letters of Guarantee / Credit, cheque discounting), with a bank position forecast and Arabic / English
print formats. Every accounting effect is a standard ERPNext Payment Entry or Journal Entry; no ERPNext or
Frappe file is changed.

**العربية.** تطبيق الخزينة لـ ERPNext بيدير الشيكات الآجلة الصادرة والواردة بدورة حالات كاملة وأحداث ترحيل
قابلة للضبط لكل شركة، وسند الشيكات المتعدد (زي 12 شيك إيجار في مستند واحد)، وشيكات الضمان من غير أي قيد
محاسبي، والتسهيلات البنكية (الحدود، والقروض، والمرابحة، وخطابات الضمان، والاعتمادات المستندية، وخصم
الأوراق التجارية)، ومعاهم تقرير مركز البنك المتوقع وطباعة عربي وإنجليزي. كل القيود بتطلع كـ Payment Entry
أو Journal Entry قياسية، ومن غير أي تعديل على كود ERPNext أو Frappe.

Version 1.0.0 — Frappe 16 / ERPNext 16, Python 3.14. License: MIT.

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

---

### Contents

1. [Installation](#installation)
2. [Configuration](#configuration)
3. [Modules](#modules): [PDC Cheques](#pdc-cheques), [Cheque Batch](#cheque-batch-multi-cheque-voucher),
   [Security Cheques](#security-cheques), [Bank Facilities](#bank-facilities)
4. [Reports and workspace](#reports-and-workspace)
5. [Roles and permissions](#roles-and-permissions)
6. [Scheduled jobs](#scheduled-jobs)
7. [Known limitations and follow-ups](#known-limitations-and-follow-ups)
8. [Testing](#testing)

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
  `treasury_cycle`) and nothing else from core. The three roles and their assignment to users are kept on
  purpose (without the app they grant nothing); delete them in *Role* if they are not wanted. The vouchers
  created by Treasury stay ordinary Payment / Journal Entries.

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
  unique per bank account across PDC and security cheques.
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
  hook `treasury_forecast_summary_sources` (`(filters) -> [{"label", "value"}]`).

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
the transfer's rate, a rounding gap of up to 0.05 is absorbed by the fee row).

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
Warn shows a message; Block rejects the submit (and a Reschedule onto a full day). **Approval**: above the
*Transfer Approval Threshold* (company currency) only a Treasury Manager may submit.

**Statuses**: Draft → *Scheduled* (future execution date; the daily job sends it on its date, catching up
missed days; **Send Now** / **Reschedule**) → *Sent* (Payment Entry posted) → *Completed* (the bank
confirmed it: **Mark Completed** or the PDC Bank Clearing tool; the Payment Entry gets its clearance date).
*Transfer Completion* can complete on send (all, or Fawran only). **Mark Returned** (Treasury Manager; not
for a receipt — refund it with a new Party Payment) reverses the Payment Entry on the return date: an
Internal Transfer without fee by a reverse Payment Entry, anything else by a Journal Entry; the fee stays
booked unless *Refund Fees on Return* is set. **Cancel** a Scheduled or Sent transfer (its Payment Entry is
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

### Reports and workspace

The **Treasury** workspace (sidebar + desktop icon; shown to Treasury Manager / User, Accounts Manager /
User and System Manager) holds the number cards, the *Expected Cash Flow* and *PDC by Status* charts and
the report links. Reports only read data, with the user's permissions and User Permissions (a user
restricted to other companies gets a permission error for a forbidden Company filter). Company currency
amounts of other currencies use today's rate.

| Module | Reports | Number cards |
|---|---|---|
| PDC | Bank Position Forecast, Projected Party Statement, PDC Register, PDC Maturity, Bounced and Postponed Cheques, Cheque Book Usage | Outgoing / Incoming Cheques Due Today, Due in 7 Days, Overdue; Outstanding PDC Payable / Receivable |
| Security Cheques | Security Cheque Register, Security Exposure, Security Cheques by Custody, Expired Security Cheques Not Returned | Security Held, Security Given, Security Cheques Expiring in 30 Days, Expired Security Cheques Not Returned |
| Bank Facilities | Facility Utilisation, Facility Repayment Schedule Report, Cost of Financing, LG Register, LC Register, Discounted Cheques | Total Facility Limits, Total Facility Utilised, Installments Due in 30 Days, Overdue Facility Installments, Open Letters of Guarantee, Open Letters of Credit, Discounted Cheques Outstanding |

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
| daily_long | `treasury.bank_facilities.scheduler.refresh_facilities` | facility / LG / LC expiry, installment statuses (Overdue), displayed utilisation |
| daily_long | `treasury.bank_facilities.interest.post_interest_accruals` | month-end interest accruals (when enabled) |
| daily_long | `treasury.bank_facilities.scheduler.send_facility_alerts` | installments due / overdue, facilities and guarantees expiring |
| daily | `treasury.security_cheques.scheduler.send_security_expiry_alerts` | security cheques expiring or expired and not returned |

Scheduled postings run as Administrator; alerts go to Treasury Manager / User as notifications and, if outgoing email is
set up, by email, listing only the documents each recipient may read.

### Known limitations and follow-ups

- **PDC**: *Endorsed* is a status the reports understand, but there is no endorsement action yet
  (endorsing a received cheque to a supplier). No list-view bulk
  actions (bulk deposit / clear): use *PDC Bank Clearing* or the Cheque Batch actions. No approval workflow
  for postponement / cancellation beyond the role checks. No warning when an outgoing cheque would make the
  projected balance negative. No customer / supplier portal.
- **Cheque Batch**: party cheques only; one reference per row; FIFO needs the party account in the cheque
  currency; at most 120 cheques per batch; the batch link on a cheque is set only by the batch.
- **Security Cheques**: encashing against a claim or penalty income account by Journal Entry is not
  implemented (an encashment settles the party's invoices or stays an advance); encashment for Other
  parties, alerts before the end of the linked contract, an approval workflow for Open / Blank
  cheques and memo (off-balance-sheet) accounts are follow-ups.
- **Bank Facilities**: a variable rate is informational only (nothing is repriced when the reference rate
  moves); a prepayment reduces the installment, never the tenor, and a Murabaha profit is not recomputed;
  overdraft interest is not accrued (book it from the bank statement) and overdraft utilisation is not
  covered by the concurrency stamp; import loans are manual (an LC settlement does not create a drawdown);
  Sight LCs are not forecast; no print formats for LG / LC / Cheque Discounting (the standard print view
  applies); discounted cheques are settled by a user action (no daily job).
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
Naming Settings if not needed) and the *Treasury Payees* Supplier Group with its suppliers. The accounting
dimension fields ERPNext added to PDC Cheque / Cheque Batch are removed with those doctypes.

### Testing

```bash
bench --site <test-site> set-config allow_tests true
bench --site <test-site> run-tests --app treasury
```

About 400 integration tests (`frappe.tests.IntegrationTestCase`), independent of the execution order: each
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
