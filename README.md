### Treasury

Post-dated cheques, security cheques, multi-cheque vouchers and bank facilities for ERPNext

### Installation

You can install this app using the [bench](https://github.com/frappe/bench) CLI:

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app $URL_OF_THIS_REPO --branch develop
bench install-app treasury
```

### Roles and permissions

- **Treasury Manager**, **Treasury User** and **Cheque Approver** are *process* roles: they give access to
  the Treasury doctypes (PDC Cheque, Cheque Book, PDC Bank Clearing, settings) and to the cheque actions
  (Deliver, Receive, Deposit, Clear, Bounce, ...). The actions check the user's permission on the cheque
  and, where required, the role (e.g. Bounce / Replace / Cancel need Treasury Manager).
- **Posting needs no accounting permission.** The Payment Entries and Journal Entries of a cheque are
  created, submitted and cancelled by the posting service (`treasury/accounting/posting.py`) in an elevated
  context, only after the permission check on the cheque. A Treasury user does not need (and the app does
  not grant) any permission on Payment Entry, Journal Entry, Account, Bank Transaction, etc.
- **Masters used in the forms still need read access.** To pick a Customer / Supplier / Bank Account /
  Company in the cheque form and the PDC Bank Clearing tool, and to open the linked vouchers, users need
  read access to those masters, normally through a standard ERPNext role (e.g. *Accounts User*) or your
  own role configured in Role Permission Manager. User Permissions (e.g. per company) are respected: the
  PDC Bank Clearing tool only lists the cheques and bank accounts the user may read.
- The vouchers' `owner` / `modified_by` is the user who ran the cheque action (Administrator for scheduled
  postings), like the cheque's status log; their GL entries and version history show Administrator, the
  user they were technically saved as.
- The app changes no standard permission of ERPNext doctypes. Development versions before this one granted
  the Treasury roles read access to some core doctypes through Custom DocPerm; the first `bench migrate`
  removes those rows and resets doctypes whose permissions were only a copy of the standard ones.
- Uninstalling the app keeps the three roles and their assignment to users (they have no permission left);
  delete them in *Role* if they are no longer wanted.

### Reports and workspace

- The **Treasury** workspace (sidebar + desktop icon) holds the PDC number cards, the *Expected Cash Flow*
  and *PDC by Status* charts and links to the six PDC reports: Bank Position Forecast, Projected Party
  Statement, PDC Register, PDC Maturity, Bounced and Postponed Cheques, Cheque Book Usage.
- Reports only read data. Cheques are read with the user's permissions and User Permissions; a user
  restricted to other companies gets a permission error for a forbidden Company filter.
  The workspace and its desktop icon are shown to Treasury Manager / User, Accounts Manager / User and
  System Manager. Bank accounts excluded by a User Permission (Bank Account, for PDC Cheque) are left out
  of the forecast's GL balances.
- **Bank Position Forecast**: opening and actual movements come from the bank GL only (it moves on the
  bank posting of a cheque); every cheque whose bank posting is still to come is an expected flow on its
  effective (possibly postponed) due date — or, for a cheque cleared but not bank posted yet (Manual
  mode), on the bank transaction / clearance date. Past-dated flows form the *Overdue* row (counted as
  today); with a From Date after today, the flows from today up to it form a *Brought Forward* row. Other
  modules add flows through the `treasury_forecast_sources` hook (see `treasury/pdc/forecast_sources.py`).
- **Projected Party Statement**: the projected rows stop at To Date; the exposure summary counts every
  unposted cheque of the party, whatever its due date.
- **Bounced and Postponed Cheques**: a bounce is dated by the bounce date the user entered, stored in the
  status log's `Event Date` (every dated lifecycle action and voucher records its date there).
- **Number cards vs. the forecast** (deliberate): the cards count cheques still in circulation — outgoing
  Created + Delivered, incoming Received + Deposited — so a written but not handed over outgoing cheque is
  outstanding, and a cheque already cleared by the bank (Manual bank posting pending) is not, although the
  forecast still expects its bank posting. "Due in 7 Days" means due today or in the next 7 days (0 to 7
  days, both included), the same convention as the *0-7 Days* bucket of PDC Maturity. Without a company
  and a default company, the sum cards show 0.
- Generic words of the reports are translated with the context `Treasury Report` (see `locale/ar.po`),
  so the app never changes ERPNext's own translation of the same words.

### Cheque Batch (multi-cheque voucher)

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
- **Scope limits.** Party cheques only (Customer / Supplier): a payee without a party (e.g. rent booked
  straight to an expense account, PLAN.md §6) is not supported yet, since a PDC Cheque requires a party.
  One reference per row. FIFO needs the party account in the cheque currency. The batch's `cheque_batch` /
  `cheque_batch_row` link on a cheque is set only by the batch and cannot be changed.

### Security Cheques

- **What it is.** A guarantee cheque (شيك ضمان) held from a party (*Incoming*: tender, contract performance,
  advance payment, rent, employee custody ...) or given to a party (*Outgoing*), numbered SEC-IN- / SEC-OUT-.
  The party is a Customer, Supplier, Employee or *Other* (a free-text name). A cheque may reference the
  document it guarantees (Project, Contract, Sales / Purchase Order, Quotation, Supplier Quotation,
  Employee — and Bank Facility once that DocType exists), carries the *Required Amount* of that document,
  an optional cheque date (a security cheque may be undated) and an expiry date (end of the guarantee).
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
- **Roles.** Treasury Users create and handle security cheques (receive, issue, custody, return, cancel);
  *Mark as Lost* and *Encash* need Treasury Manager.
- **Cheque book (outgoing).** An outgoing security cheque may take the next leaf of a Cheque Book of its
  bank account (Cheque No left empty). The leaf is consumed for good: cancelling, returning or losing the
  security cheque frees nothing, it is never allocated again, cannot be cancelled as an unused leaf, and
  counts as *Used* in Cheque Book Usage. Our outgoing cheque numbers are unique per bank account across
  security and PDC cheques. A security cheque that took a leaf, or on which any action happened, cannot be
  deleted (cancel it instead).
- **Encash → PDC Cheque** (Treasury Manager): allowed for a Customer / Supplier party only (Employee and
  Other are refused for now). The user gives the encashment date, the amount (required for Open / Blank; a
  Fixed cheque is encashed for its own amount), the due date (default: the encashment date) and, for an
  incoming cheque, the bank account it is deposited to — it must be in the security cheque's currency (the
  dialog offers only those; a bank in another currency is refused before anything is created).
  - Incoming: a PDC Cheque (Incoming, same party, cheque no and drawer bank) is created and **received**
    on the encashment date, so it is posted per Treasury Company Settings like any cheque (e.g. `On
    Receipt`: Payment Entry at once; `On Cheque Date`: by the daily job). Invoice / order references may
    be passed to settle them; without references the amount is an advance of the party.
  - Outgoing: a PDC Cheque (Outgoing, same bank account and cheque no, no cheque book) is created and
    **delivered** on the encashment date; clear it through the PDC flow when the bank pays it.
  - The PDC Cheque's issue date is the encashment date (an `On Creation` posting is never back-dated). It
    links back through its read-only *Security Cheque* field (set only by the encashment), the security
    cheque becomes Encashed with *Converted PDC Cheque* set. All or nothing: if the PDC Cheque cannot be
    created or received / delivered, nothing is kept (the PDC Cheque, its `On Creation` posting and the
    security cheque's Encashed state are rolled back together).
  - Replacing that PDC Cheque carries the *Security Cheque* link to the replacement; the security cheque's
    *Converted PDC Cheque* keeps pointing at the first one, whose *Replaced By* leads on.
- **Alerts.** A daily job (`treasury.security_cheques.scheduler.send_security_expiry_alerts`) sends each
  Treasury Manager / User one digest per company and day of the cheques still held / given that expired
  (not returned) or expire within *Security Cheque Alert Days* (Treasury Settings, default 15) — only the
  cheques they may read, in their language, as a notification and, if outgoing email is set up, by email.
  A setting never stored counts as 15 (and `bench migrate` stores the missing Treasury Settings defaults);
  an explicit 0 means "expired cheques only".
- **Reports and cards** (Security Cheques section of the Treasury workspace): *Security Cheque Register*,
  *Security Exposure* (held from others vs given to others, by party or by reference document with Required
  / Actual / Shortfall — the requirement of a document is the highest Required Amount on its cheques, not
  their sum; a cheque without a reference is its own row; Open / Blank cheques are counted, never summed,
  and when a document's fixed cheques fall short but it also has Open / Blank ones the shortfall is left
  blank — "Covered by Open / Blank" — and not counted in the Total Shortfall; a document guaranteed by
  several parties shows "Several Parties"),
  *Security Cheques by Custody*, *Expired Security Cheques Not Returned*; number cards *Security Held*
  (incoming Received + In Custody), *Security Given* (outgoing Issued), *Security Cheques Expiring in 30
  Days* (today to today + 30) and *Expired Security Cheques Not Returned*. Amounts are in the company
  currency (a cheque in another currency is converted at today's rate; company currency cheques are never
  converted); reports and cards read with the user's permissions and User Permissions. The register totals
  each direction separately (held and given are never added together). A party of type Other has no
  Party link in the reports (its name is in Party Name).
- **Print.** *Security Cheque Receipt*: receipt (incoming) / handover acknowledgment (outgoing) with the
  print date (today), party, cheque, amount or "Open / Blank", purpose, reference, expiry, custody location
  and signatures; right-to-left in Arabic.
- **Scope limits / follow-ups.** Encashing against a claim or penalty income account by Journal Entry
  (PLAN.md §7: "a penalties / income account") is not implemented: an encashment settles the party's
  invoices or stays an advance of the party, to be reconciled or adjusted later. Encashment for Employee /
  Other parties, alerts before the end of the linked contract, approval workflow for open / blank cheques
  and memo (off-balance-sheet) accounts are follow-ups.

### Contributing

This app uses `pre-commit` for code formatting and linting. Please [install pre-commit](https://pre-commit.com/#installation) and enable it for this repository:

```bash
cd apps/treasury
pre-commit install
```

Pre-commit is configured to use the following tools for checking and formatting your code:

- ruff
- eslint
- prettier
- pyupgrade
### CI

This app can use GitHub Actions for CI. The following workflows are configured:

- CI: Installs this app and runs unit tests on every push to `develop` branch.
- Linters: Runs [Frappe Semgrep Rules](https://github.com/frappe/semgrep-rules) and [pip-audit](https://pypi.org/project/pip-audit/) on every pull request.


### License

mit
