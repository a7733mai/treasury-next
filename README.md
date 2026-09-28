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
