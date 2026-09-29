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
- **Bank Position Forecast**: opening and actual movements come from the bank GL only (it moves on the
  bank posting of a cheque); every cheque whose bank posting is still to come is an expected flow on its
  effective (possibly postponed) due date. Other modules add flows through the `treasury_forecast_sources`
  hook (see `treasury/pdc/forecast_sources.py`).
- Generic words of the reports are translated with the context `Treasury Report` (see `locale/ar.po`),
  so the app never changes ERPNext's own translation of the same words.

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
