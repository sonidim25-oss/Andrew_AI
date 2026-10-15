# Gmail API integration

## Status

Installed from `d64872e` on 2026-10-15 at 01:44 UTC. Browser/runtime commissioning and controller startup passed; controller and Letta are active. The owner subsequently completed Google OAuth; the helper confirmed the Gmail profile and encrypted token storage. After a separate owner request, draft/send/delete were enabled and the owner completed a new OAuth flow. The helper confirmed connection again; it requires the granted scopes to match the requested permission set before saving tokens. The administrator-owned file was independently read again: enabled/read/draft/send/delete are all true. Deletion remains limited to moving messages to Trash. Twelve offline permission, refresh and retry tests now pass, as does a real isolated Letta status callback using a fixture Gmail implementation. Actual mailbox search/read, token refresh and mutations remain untested. See [the reliability review](RELIABILITY_REVIEW_2026-10-15.md). The uploaded Desktop client JSON was validated without displaying secrets and restricted to mode 0600. Browser commissioning does not prove Gmail behavior. Installation evidence: `/var/lib/andrew/runtime-commissioning/20261015T014059Z`.

## Permissions

Gmail starts with `gmail.readonly`. Every operation checks both the administrator-owned permission file and the stored OAuth grant. Missing configuration disables Gmail. Writes are disabled by default.

| Capability | Default | Google scope when enabled | Andrew action |
|---|---|---|---|
| Read | Allowed after connection | `gmail.readonly` | Search/list and read message text without marking it read |
| Draft | Disabled | `gmail.compose` | Create a plain-text draft without sending |
| Send | Disabled | `gmail.send` | Send a new plain-text message to explicit recipients |
| Delete | Disabled | `gmail.modify` | Move one message to Trash |

Google's compose and modify scopes also allow sending, so Andrew independently enforces the send permission before any send request. Draft permission alone cannot invoke `gmail.send`. Permanent deletion and the broad `https://mail.google.com/` scope are not supported. See [Google's scope reference](https://developers.google.com/workspace/gmail/api/auth/scopes).

Permissions apply to the Gmail API tools. Tokens never enter the browser or code sandbox. The model instructions also prohibit using browser/code tools to bypass Gmail permissions. This is not a host-wide prohibition on every possible way of interacting with email.

## 1. Configure your Google Cloud project

1. Select the project and enable **Gmail API** in **APIs & Services → Library**.
2. Configure **Google Auth Platform → Branding** with the app name `Andrew Gmail` and contact/support email.
3. For a personal Gmail account, select **External** under **Audience** and add your account as a test user while the app is in Testing. Workspace-only apps can use Internal when available.
4. In **Data Access**, add only `https://www.googleapis.com/auth/gmail.readonly` initially.
5. Under **Clients**, create a **Desktop app** OAuth client and download its JSON file. Keep it outside the repository. The setup helper uses this client with a loopback callback, PKCE and a random OAuth state.

An API key cannot authorize access to a private mailbox. The OAuth consent flow grants the selected account access. Follow [Google's Gmail setup guide](https://developers.google.com/workspace/gmail/api/quickstart/python) and [Desktop OAuth instructions](https://developers.google.com/identity/protocols/oauth2/native-app).

Google documents a seven-day refresh-token lifetime for External apps in Testing when requesting Gmail scopes. For durable use, review the project's publishing/verification requirements; do not assume a test-user grant lasts indefinitely. See [Google's token expiration rules](https://developers.google.com/identity/protocols/oauth2#expiration).

## 2. Install the controller source

From the VPS, run the existing installer for this checkout:

```sh
sudo bash /home/daniel/andrew-browser-integration/scripts/enable-browser-runtime.sh
```

This renews the existing code-bound browser/runtime acceptance record and installs the new controller modules. It does not create a Google project, grant Gmail consent, enable Gmail writes or prove Gmail behavior. Do not copy individual modules into the live controller: its acceptance record binds the complete package.

## 3. Connect through your browser

Copy the downloaded client JSON to `/home/daniel/gmail-client.json` on the VPS and restrict it to your account (`chmod 600 /home/daniel/gmail-client.json`). Do not paste client secrets, authorization codes or tokens in Telegram/chat.

From your **main PC**, open an SSH session with loopback forwarding (replace `YOUR_VPS`):

```sh
ssh -L 127.0.0.1:8765:127.0.0.1:8765 daniel@YOUR_VPS
```

In that forwarded SSH session, run:

```sh
sudo bash /home/daniel/andrew-browser-integration/scripts/andrew-gmail connect /home/daniel/gmail-client.json
```

Open the printed Google authorization URL in the browser on your main PC. Choose the intended Gmail account and approve read-only access. The callback returns through the SSH tunnel. Keep the terminal open until it confirms the account and encrypted storage. The helper reads the Gmail profile to confirm the account; it does not send, draft or delete mail. It waits up to five minutes for consent.

The Desktop client JSON is passed as a temporary systemd credential. The initial access/refresh tokens and client secret are stored in the existing encrypted vault. Token refresh is automatic; refreshed access tokens stay in memory for that operation, and no plaintext token file is written. You may remove the uploaded JSON after connecting if you keep a secure copy for future consent changes.

The next model turn reloads Gmail enablement; a controller restart is unnecessary after consent alone. In Telegram, `/gmail` or **📬 Gmail** shows the local connection, permission and scope status. This status does not prove the remote token is still valid.

Optional explicit read-only check, which prints counts without message content:

```sh
sudo bash /home/daniel/andrew-browser-integration/scripts/andrew-gmail verify-read
```

You can then ask Andrew to list recent messages or search Gmail. Email content read by Andrew becomes model input through the existing guarded model provider and can remain in Letta's persistent conversation. Activity logs contain operation summaries, not subjects or message bodies. Read results are bounded and attachments are not downloaded or executed.

## 4. Change permissions explicitly

Permission changes require your administrator command. Gmail messages and model tools cannot change them. `/gmail` in Telegram is read-only.

```sh
# View the current switches.
sudo bash /home/daniel/andrew-browser-integration/scripts/andrew-gmail permissions

# Enable only draft creation; sending and deletion remain disabled.
sudo bash /home/daniel/andrew-browser-integration/scripts/andrew-gmail permissions draft on

# Independent permissions; run only for capabilities you want to authorize.
sudo bash /home/daniel/andrew-browser-integration/scripts/andrew-gmail permissions send on
sudo bash /home/daniel/andrew-browser-integration/scripts/andrew-gmail permissions delete on
```

After enabling a capability whose scope is missing, add the required scope to Google Auth Platform **Data Access** and repeat `connect` with SSH forwarding. OAuth requests exactly the scopes corresponding to the current switches, plus read-only access. Both local permission and matching consent must be present before an operation can run. Enabling writes authorizes Andrew to perform that class of action; this release does not add per-message confirmation prompts.

Disable a permission immediately for subsequent operations with `permissions send off`, `permissions draft off` or `permissions delete off`. A request already sent to Google cannot be cancelled by a later switch change. Local switches do not shrink the Google grant already issued; reconnect after changing scopes or revoke the app grant if you need to narrow it at Google too.

Disable all Gmail API tools with:

```sh
sudo bash /home/daniel/andrew-browser-integration/scripts/andrew-gmail permissions enabled off
```

Remove local tokens and attempt revocation at Google with:

```sh
sudo bash /home/daniel/andrew-browser-integration/scripts/andrew-gmail disconnect
```

If remote revocation cannot be confirmed, remove the app connection in your Google account as well. This command does not delete email.

## Model tools and boundaries

- `gmail.status`: current local permissions and stored OAuth scope status.
- `gmail.read`: `list` with query/limit/page token, or `get` by message ID. GET operations preserve unread state.
- `gmail.draft`: create one draft from explicit `to`, `subject`, `body` and `request_id`.
- `gmail.send`: send one new plain-text message with the same fields; existing-draft sending, aliases, CC/BCC and attachments are not implemented.
- `gmail.delete`: move one `message_id` to Trash with a `request_id`; no bulk or permanent deletion.

Mutations are recorded before sending. Reusing a confirmed request ID returns the stored result; reusing it with different content is rejected. An uncertain outcome is held for inspection instead of automatically retried. This reduces duplicate actions but is not exactly-once delivery: a new request ID can represent a new request. Inspect the mailbox after an uncertain outcome before deciding to retry.

Google endpoint URLs are fixed, redirects and environment proxies are disabled, TLS verification remains on, response sizes and request waits are bounded, and errors omit OAuth secrets and response bodies. Mail is explicitly labeled untrusted content. Existing owner pause/sleep and model budget enforcement remain in place.

## Budget visibility

`check_budget` is already in the controller's explicitly allowed Letta tools. It reads local spending records without requiring approval or a provider call of its own and remains callable during pause/sleep. The model gateway now includes current spending, reserved usage, protected reserve and remaining allowance in each permitted model request. The added context is included in the normal input-cost reservation. Exhausted budgets still block new model requests; reading a budget does not override that gate.
