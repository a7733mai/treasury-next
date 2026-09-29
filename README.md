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
