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

### Bank Facilities

Credit facilities with banks: the agreement and its sub-limits, drawdowns with their repayment schedules,
repayments and interest. Everything is our own DocTypes; the Lending app is **not** used or required.
All accounting goes through `treasury.accounting.posting` as standard Journal Entries linked back to the
drawdown / repayment (`treasury_source_*`), idempotent per source, event and month.

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
  Credit, Cheque Discounting, Murabaha, Tawarruq, Other; rate type (fixed / variable reference + margin, the
  all-in rate is entered; **a variable rate is informational only — nothing is repriced** when the reference
  rate moves: enter the new rate on new drawdowns), day count (Actual/365, Actual/360), commission and cash margin. Each sub-limit is
  at most the total limit; together they may exceed it (lines share the total). Loans / Murabaha / Tawarruq
  / Other need a liability account in the facility currency.
- **Utilisation** (`treasury.bank_facilities.utilisation.get_utilisation(facility, limit_type, as_on)`) is
  computed from the ledger, never from stored numbers: loans = the balance of each drawdown's liability
  account on the Journal Entries linked to the drawdown or its repayments (other postings on the same
  account never count); Overdraft = max(0, − GL balance of the Default Bank Account's GL account). Available
  = min(sub-limit − its utilisation, total limit − total utilisation). Other documents (Letters of Guarantee
  / Credit, Cheque Discounting — see below) add utilisation through the hook
  `treasury_facility_utilisation_sources` (functions `(facility_doc, as_on) -> [{"limit_type", "amount"}]`).
  The *Utilised* / *Available* fields on the facility are display only (refreshed after every action and
  daily); reports, cards and the drawdown limit check use the API.
- **Facility Drawdown** (submittable): limit type (not Overdraft), amount, bank account credited (default:
  the facility's), terms (rate defaults to the sub-limit's; or a fixed Murabaha profit amount), tenor,
  frequency (Monthly / Quarterly / Semi-Annually / Annually / Bullet), method (Equal Principal / Equal
  Installment (annuity) / Bullet), first repayment date (default one period after the drawdown), grace months
  (interest only), upfront fees. *Generate Schedule* (draft only) builds the rows: day-count interest on the
  outstanding principal per period, annuity installment P·r / (1 − (1 + r)^−n) with r = rate × months / 12,
  a Murabaha profit spread straight-line, every amount rounded and the last row absorbing the rounding
  (principal always adds up to the amount), month-end dates kept on month ends (shared helper
  `treasury.utils.dates`). **Annuity**: the installment uses the nominal periodic rate while each row's
  interest uses the day count, so the rows' principal differs slightly from a textbook annuity and the last
  row (repaying whatever is left) can differ noticeably from the others — more so with Actual/360 (31-day
  months cost more than rate / 12). Rows may be edited before submit as long as the principal adds up.
  Changing a term (amount, dates, rate, day count, profit, tenor, frequency, method, grace) requires a new
  schedule: the form clears it (rebuilt on save) and the server refuses a changed term with an unchanged
  schedule. Submit checks the facility is Active and the date inside its period, and the available amount of
  the sub-limit and of the total, then posts Dr Bank / Cr Liability (and Dr Fees / Cr Bank for the upfront
  fees). Cancel is refused while repayments are submitted and cancels its vouchers (accruals included).
  System fields (vouchers, last accrual date, repayment status, the rows' paid amounts) are reset on save and
  on submit, whatever the client sends.
- **Facility Repayment** (submittable): principal + interest / profit (empty: the next unpaid installment),
  optional penalty (+ account), allocation *Oldest First* or *Selected Row*. Posts Dr Liability (principal)
  + Dr Accrued Interest (the part already accrued, cleared first) + Dr Interest Expense (the rest) + Dr
  Penalty / Cr Bank, then records the paid amounts on the schedule rows (Pending / Partially Paid / Overdue /
  Paid; Overdue refreshed daily). **Prepayment**: principal beyond the installments due up to the repayment
  date is recorded as a paid prepayment row and the later untouched installments are rescheduled by
  **reducing the installment amount** (the tenor is kept; interest recomputed on the reduced principal with
  the day count; a Murabaha profit is not recomputed). Reducing the tenor instead is not implemented. A
  repayment cannot be back-dated before a submitted one; only the last **submitted** one (highest *Submission
  No*, stamped at submit under the drawdown lock — drafts may be submitted in any order) can be cancelled,
  which restores the schedule exactly (snapshot) and cancels its Journal Entry.
- **Bank clearance**: the drawdown, fees and repayment Journal Entries are cleared on their posting date
  (`clearance_date` set, as the bank moved the money on that day), so they do not wait in the Bank
  Reconciliation Tool. Accrual Journal Entries have no bank row.
- **Concurrency**: drawdown submits lock the facility row, repayment submits / cancels and the accrual job
  lock the drawdown row. Every change the checks depend on moves a stamp on that row (the facility's
  utilisation stamp / status / modified; the drawdown's `schedule_version` / last accrual date), and the
  stamp read before the lock is compared with the locking read: if another transaction committed meanwhile
  the user is asked to try again instead of checking the limit or allocating on stale rows. (Overdraft
  utilisation moves with any bank posting and is not covered by this check.)
- **Interest accrual** (Treasury Company Settings *Accrue Facility Interest Monthly*, default off): the
  daily job posts, per drawdown and month end, Dr Interest Expense / Cr Accrued Interest for the interest
  earned up to the month end (day by day within each schedule period) less what was already accrued or
  expensed by repayments — idempotent (event `interest_accrual:YYYY-MM`), catches up missed months, one
  drawdown at a time (a failing one is rolled back and logged once a day, the others go on).
- **Overdraft interest is not accrued**: the bank charges it on the statement; book it from the bank
  statement (it does not appear in Cost of Financing, which reads facility-linked vouchers only).
- **Forecast**: the unpaid part of every installment is an outflow on its due date from the drawdown's bank
  account in the Bank Position Forecast (hook `treasury_forecast_sources`); paid installments disappear, a
  partly paid one shows the rest. The summary gets an *Available Overdraft* card for the selected banks
  through the new hook `treasury_forecast_summary_sources` (`(filters) -> [{"label", "value"}]`).
- **Reports and cards** (Bank Facilities section of the Treasury workspace): *Facility Utilisation* (bank →
  facility → sub-limit: limit, utilised, available, % used, expiry, days to expiry, company currency
  totals), *Facility Repayment Schedule Report* (upcoming / overdue installments with the principal /
  interest split and status), *Cost of Financing* (interest, commission, fees and penalties per facility
  and month / quarter / year, from the GL); number cards *Total Facility Limits*, *Total Facility Utilised*,
  *Installments Due in 30 Days* (sum) and *Overdue Facility Installments* (count). Company currency
  amounts use today's rate; everything is read with the user's permissions and User Permissions.
- **Alerts**: a daily digest per company and recipient (Treasury Manager / User) of the unpaid installments
  due within *Facility Alert Days* (Treasury Settings, default 7; overdue ones included) and of the
  facilities expiring within 30 days, as a notification and, if outgoing email is set up, by email.

#### Letters of Guarantee, Letters of Credit (shared rules)

- Both are submittable documents under a facility's *Letter of Guarantee* / *Letter of Credit* sub-limit.
  Company, bank and currency come from the facility; the bank account (default: the facility's) is in the
  facility currency. **Submit = issue / open**: the facility row is locked (same stamp check as drawdowns),
  the facility must be Active on the issue date, and the amount must fit the available sub-limit and total.
  Journal Entries (source = the LG / LC, cleared on their date): cash margin **Dr Margin Account / Cr Bank**
  (margin = amount × *Cash Margin (%)*, default from the sub-limit; the margin account is an Asset account in
  the facility currency, e.g. "LG cash margins") and commission **Dr Commission Account / Cr Bank**
  (*Commission Rate (%)* × amount when a rate is set — default from the sub-limit —, else the amount
  entered; the account defaults to the facility's Commission Expense Account).
- After submit, the status (and the open amount, margin held, dates) changes **only through the actions**
  (Treasury Manager + write permission); every action appends a row to the status log (*Bank Instrument
  Status Log*: event date, from / to status, open amount after the event, voucher). An action may not be
  dated before the issue date or the latest logged event. Foreign currency: the margin is carried at the
  issue rate; the bank moves at the event's rate (entered, or the rate of the date); the difference goes to
  the company's Exchange Gain / Loss account.
- Utilisation = the open amount of the open documents; for a past date it is read from the status log. The
  daily job sets documents past their **effective** expiry date (moved by extensions / amendments) to Expired
  the day after (no accounting). Cancel cancels every voucher of the document (not once Claimed / Settled).

#### Letter of Guarantee

- Fields: LG type (Bid Bond, Performance, Advance Payment, Retention, Financial, Customs, Other), bank
  reference, beneficiary (Customer / Supplier / Other with a name), reference document (Project, Contract,
  Sales Order, Purchase Order, Quotation), issue / original expiry / effective expiry / release / claim dates.
- **Off-balance sheet**: the guaranteed amount itself is never booked (no memo accounts); only the margin,
  commissions and claims are.
- **ERPNext Bank Guarantee**: an optional read-only *link* to ERPNext's own `Bank Guarantee` record (for
  users who already keep them); Treasury never creates or changes it.
- Actions: *Extend* (later expiry date; optional extension commission Dr Commission / Cr Bank), *Reduce*
  (lower open amount; the margin held is released pro rata Dr Bank / Cr Margin), *Release* (the whole
  remaining margin back: Dr Bank / Cr Margin; status Released), *Claim (Liquidation)* (amount ≤ open amount,
  debited to an account the user chooses — an expense / claim account, or the beneficiary's party account
  with the party; one Journal Entry: Dr that account; Cr Margin for the whole margin held — used first —;
  Cr the **Liability Account of the LG sub-limit** for what the bank paid beyond the margin; Dr Bank for any
  margin left over. Status Claimed.), *Repay Claim* (Dr Liability / Cr Bank). The bank-funded part of a claim
  counts in the LG utilisation (GL balance of that liability account on the LG's vouchers) until repaid; the
  sub-limit needs a liability account for such claims.
- Expired guarantees keep their margin until the user releases it: the facility digest lists guarantees
  expiring within *Facility Alert Days* and expired ones whose margin is still held.

#### Letter of Credit (import)

- Fields: Sight / Usance (usance days), supplier (+ optional Purchase Order of that supplier), bank
  reference, tolerance %, latest shipment date, opening / original expiry / effective expiry dates.
- Actions: *Amend* (Opened / Amended: new amount and / or later expiry date, charges Dr Charges / Cr Bank;
  an increase is checked against the available limit under the facility lock; the margin follows the new
  amount — top-up Dr Margin / Cr Bank or release Dr Bank / Cr Margin), *Receive Documents* (documents amount
  ≤ amount + tolerance becomes the open amount; charges), *Accept* (Usance only: maturity date, default
  acceptance + usance days), *Settle* (Sight after the documents, Usance after the acceptance), *Release
  Margin* (Expired only).
- **Settlement accounting** (two vouchers): the whole margin held returns to the bank (Dr Bank / Cr Margin
  at the settlement rate), and a standard **Payment Entry** pays the supplier from the bank (optionally
  allocated to one of the supplier's Purchase Invoices). ERPNext's Payment Entry allocates the invoice and
  books the exchange gain / loss of a foreign currency invoice itself; a single Journal Entry was not used
  because ERPNext forces a Journal Entry row that references an invoice to the invoice's exchange rate.
  Economically this equals "margin first, bank for the rest".
- **Import loans are manual**: settlement is always paid from the bank account. When the bank finances the
  settlement, record a *Facility Drawdown* for the loan (the LC does not create it).
- Forecast: an accepted Usance credit is an expected outflow on its maturity date (documents amount less the
  margin held) on its bank account in the Bank Position Forecast. Sight credits are not forecast (the
  documents' arrival date is unknown).
- Expiry: only credits without documents (Opened / Amended) expire; received documents are settled.

#### Cheque Discounting (خصم أوراق تجارية)

- *Cheque Discounting* (submittable batch) under the facility's *Cheque Discounting* sub-limit, which needs a
  **Liability Account** ("Discounted cheques"). Rows: incoming PDC Cheques that are **Received** and **Party
  Posted** (in PDC Receivable — not deposited, not under collection), due after the discount date, in the
  facility currency. Discount rate (default: the sub-limit rate), day count (default: the sub-limit's).
  **Charges per cheque = amount × rate / 100 × days to its effective due date / 365 (Actual/365) or 360
  (Actual/360)**, rounded per cheque; commission (default: the sub-limit commission rate × total); net
  proceeds = total − charges − commission; expense account default: the facility's Interest Expense Account.
- **Submit**: limit check (facility lock), then one Journal Entry **Dr Bank (net) + Dr Discount Expense
  (charges + commission) / Cr Discounted Liability (total)**, and each cheque → PDC action `discount`:
  operational status **Discounted**, accounting status unchanged (still Party Posted, in PDC Receivable);
  the cheque records the batch and the discount date. A discounted cheque cannot be postponed, deposited,
  cleared, returned or replaced.
- **Paid at maturity** (*Settle Cheques* on the batch — ticks the ones due by the date — or *Paid at
  Maturity (Discounted)* on the cheque): Journal Entry (source = the cheque) **Dr Discounted Liability / Cr
  PDC Receivable**; the cheque becomes **Cleared / Bank Posted** and its *Bank Journal* is this entry (the
  voucher that took it out of PDC Receivable; the bank itself credited the company at the discount, so there
  is no bank row and nothing to reconcile). Settling is always a user action (no daily job): whether the
  drawer paid is only known from the bank.
- **Bounce of a discounted cheque** (recourse): Journal Entry **Dr Discounted Liability / Cr Bank** (the bank
  debits the company; cleared on the bounce date), then the party posting is undone exactly like any bounce
  according to *On Bounce* (reversal Journal Entry, or cancel of the Payment Entry): the customer owes the
  amount again and PDC Receivable is cleared. The batch row records Settled / Bounced with the date and
  Journal Entry; the batch status follows (Discounted → Partially Settled → Settled).
- **Cancel** the batch only while no cheque is settled or bounced: the cheques go back to Received (logged)
  and the batch Journal Entry is cancelled.
- Utilisation: the discounted cheques not settled / bounced (for a past date: discounted by then and not
  closed by then). **Forecast**: discounted cheques are never expected inflows (the money came at the
  discount); the Projected Party Statement still lists them as posted, not cleared (the drawer has not paid).
- Reports and cards: *LG Register* (open / expiring guarantees, margins held), *LC Register* (status,
  amounts, maturity), *Discounted Cheques* (per batch: maturity, charges, outcome); number cards *Open
  Letters of Guarantee*, *Open Letters of Credit*, *Discounted Cheques Outstanding* (company currency).
  *Facility Utilisation* shows the LG / LC / Cheque Discounting sub-limits through the utilisation hook.
- No print formats are shipped for these documents (the standard print view applies).

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
