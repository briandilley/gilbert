# Google Calendar Setup

This guide configures the `google_calendar` backend for Gilbert's Calendar
service. It is intended for agents and operators setting up Calendar UAT or a
local install.

## What Gilbert Needs

For Google Calendar, Gilbert needs:

- A Google Cloud project with the Google Calendar API enabled.
- A service-account JSON key.
- A target Google Calendar shared with the service-account email, or a Google
  Workspace domain-wide delegation grant.
- A Gilbert calendar account whose `backend_name` is `google_calendar`.

Do not commit the service-account JSON. Keep it in a gitignored local path such
as `.gilbert/google-calendar-sa.json` or another secret store, then paste the
JSON content into Gilbert's calendar account form.

## Personal Gmail Setup

Use this path for a regular Gmail account. No Workspace admin is required.

1. Create or select a Google Cloud project.
2. Enable the Google Calendar API:

   ```bash
   gcloud services enable calendar-json.googleapis.com
   ```

3. Create a service account:

   ```bash
   gcloud iam service-accounts create gilbert-calendar \
     --display-name="Gilbert Calendar"
   ```

4. Create a JSON key and store it outside git:

   ```bash
   mkdir -p .gilbert
   gcloud iam service-accounts keys create .gilbert/google-calendar-sa.json \
     --iam-account=gilbert-calendar@PROJECT_ID.iam.gserviceaccount.com
   chmod 600 .gilbert/google-calendar-sa.json
   ```

5. In Google Calendar, open the target calendar's settings and share it with the
   service-account email from the JSON key, for example:

   ```text
   gilbert-calendar@PROJECT_ID.iam.gserviceaccount.com
   ```

6. Grant `Make changes to events` if Gilbert should create, update, or delete
   events. Read-only calendar UAT can use a lower permission, but event mutation
   tests will fail.
7. In Gilbert, add a Calendar account:

   | Field | Value |
   | --- | --- |
   | Name | Any local display name |
   | Email address | The Gmail address that owns the calendar |
   | Backend | `Google Calendar` |
   | Service Account Json | Paste the full JSON key content |
   | Delegated User | Leave blank |
   | Calendar | The Google Calendar ID, usually the Gmail address for the primary calendar |
   | Timezone | The calendar timezone, for example `America/Los_Angeles` |

8. Save the account with polling disabled first if using the UI's two-step flow.
9. Reopen the account and click `Probe calendars`. For service-account sharing,
   Gilbert can fall back to a direct lookup by `Calendar`, because shared
   calendars do not always appear in `calendarList().list()`.
10. Enable polling and save.

## Google Workspace Setup

Use this path only when the calendar belongs to a Google Workspace domain and a
Workspace super admin can grant domain-wide delegation.

1. Create a service account and JSON key as above.
2. Enable domain-wide delegation for the service account.
3. In the Workspace Admin console, authorize the service account client ID with
   these scopes:

   ```text
   https://www.googleapis.com/auth/calendar
   https://www.googleapis.com/auth/calendar.events
   ```

4. In Gilbert, set:

   | Field | Value |
   | --- | --- |
   | Email address | The Workspace user mailbox/calendar owner |
   | Service Account Json | Paste the full JSON key content |
   | Delegated User | The Workspace user to impersonate |
   | Calendar | `primary` or an explicit Google Calendar ID |

The same service account can also be used for Gmail if the Workspace grant
includes Gmail scopes. A plain personal Gmail account is not enough for the
current Gmail backend because Gmail impersonation requires Workspace
domain-wide delegation.

## Local Smoke Probe

Before configuring Gilbert, an operator can validate the shared-calendar model
directly:

```bash
uv run --with google-api-python-client --with google-auth python - <<'PY'
import datetime as dt
import json
from pathlib import Path
from google.oauth2 import service_account
from googleapiclient.discovery import build

key_path = Path(".gilbert/google-calendar-sa.json")
calendar_id = "user@example.com"
info = json.loads(key_path.read_text())
creds = service_account.Credentials.from_service_account_info(
    info,
    scopes=["https://www.googleapis.com/auth/calendar"],
)
svc = build("calendar", "v3", credentials=creds, cache_discovery=False)
cal = svc.calendars().get(calendarId=calendar_id).execute()
print(cal["id"], cal.get("timeZone"))
now = dt.datetime.now(dt.timezone.utc)
events = svc.events().list(
    calendarId=calendar_id,
    timeMin=now.isoformat(),
    timeMax=(now + dt.timedelta(days=7)).isoformat(),
    singleEvents=True,
    orderBy="startTime",
    maxResults=5,
).execute().get("items", [])
print(f"events={len(events)}")
PY
```

## Troubleshooting

- `invalid_grant: Invalid JWT`: check the machine clock. Google rejects
  service-account JWTs when the local clock is outside its accepted time window.
  Enable NTP and retry.
- `404 Not Found`: the `Calendar` field is wrong, or the calendar has not been
  shared with the service-account email.
- `403 Forbidden`: the Calendar API may not be enabled, or the service account
  does not have enough sharing permission on the target calendar.
- `Probe calendars` returns one direct match instead of a full list: this is
  expected for many personal-Gmail shared calendars. Shared calendars do not
  always auto-appear in a service account's calendar list.
- The JSON key is masked as `********` in normal account payloads. Owners and
  admins can reveal it through the edit drawer; the value is still stored in the
  local SQLite database, so protect `.gilbert/gilbert.db` and backups.
