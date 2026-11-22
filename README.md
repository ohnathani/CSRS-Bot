# Features

- Loads current PE and NSTP subject tables from the authenticated Registration page.
- Shows PE options and only available NSTP options.
- Saves subject choices in the browser so they persist between app sessions.
- Opens on the login page and keeps passwords out of browser storage and files.
- Keeps the first Selenium window to load Registration and closes the other attempts; if all fail, their windows remain open.
- Retries transient login and Registration page loading every 5 seconds; stops if the portal explicitly rejects the credentials.
- Lets you enter 1–50 parallel Selenium instances using the same login credentials.
- Tiles all requested Selenium windows on the left, then waits until their login forms are ready before entering credentials.
- Re-enters credentials only after a submitted login form disappears and a new ready form appears.
- In manual mode, lets users review and confirm selected subjects before enlistment.
- BROKEN: Offers an optional, persistent auto-enlist toggle (off by default). It submits checked saved subjects as soon as Registration renders; in an active session, enabling it submits the checked subjects, and newly checked subjects are submitted immediately. Auto-enlist skips review and confirmation and never selects unchecked subjects.
- Uses Selenium to activate the matching live CSRS Add control for each subject.
- Reports each enlistment result and stops when the portal response is unconfirmed.
- Offers a retry button for failed and unconfirmed enlistments; retrying an unconfirmed outcome may duplicate an enlistment if the portal already accepted it.
- Opens the local interface in Chrome's Default profile and resizes only that window.

## Run

Install dependencies:

```powershell
python -m pip install -r requirements.txt
```

Start the app:

```powershell
python local_app.py
```
