# Per-user authentication and privacy

The repository contains workflow code only. Domo, DAMS, BMAT SFTP, SynchTank
S3, TuneSat, ESPN, SoundExchange, SoundMouse, UniSync, and Monday authentication is
owned by the macOS user running the workflow and must never be copied with the
repo or onto a Pegasus delivery.

## New user onboarding

Run these commands while logged into the recipient's own macOS account:

```bash
cd "$HOME/Documents/Scripts/Python/UPM Release WorkFlow Automation/files"
make install
python3 auth_manager.py --enroll-domo-keychain
python3 auth_manager.py --enroll-domo-api-keychain
python3 auth_manager.py --setup domo
python3 auth_manager.py --setup dams
python3 auth_manager.py --setup unisync
python3 auth_manager.py --enroll-bmat-keychain
python3 auth_manager.py --enroll-synchtank-keychain
python3 auth_manager.py --enroll-tunesat-keychain
python3 auth_manager.py --enroll-espn-keychain
python3 auth_manager.py --enroll-netmix-keychain
python3 auth_manager.py --enroll-soundexchange-keychain
python3 auth_manager.py --enroll-soundmouse-keychain
python3 soundmouse_web_auth.py
python3 auth_manager.py --enroll-monday-keychain
python3 auth_manager.py --status
```

- `--enroll-domo-keychain` collects the UMG email and password once each through
  labeled hidden Terminal prompts. Python passes each exact in-memory value to
  Keychain for storage and confirmation; values never enter command arguments,
  environment variables, logs, or files. The stored values are checked exactly.
- `--enroll-domo-api-keychain` separately collects a Domo API client ID and
  client secret through hidden prompts. It requests a short-lived token from
  `api.domo.com` using HTTP Basic authentication and the minimal `data` scope,
  then stores both values through macOS Security.framework only after Domo
  accepts them. The pair is replaced atomically: a partial Keychain failure
  restores the previous values. Tokens and credential values are never logged.
- Domo opens an isolated Playwright profile and waits for that user's Microsoft
  SSO/MFA. The resulting cookies stay under
  `~/.upm_release_workflow/domo_browser_profile`. Normal runs can select the
  enrolled account and fill the password from Keychain entirely in memory.
- UniSync opens its installed app. The user signs in there; the workflow never
  collects the credential. UniSync's local preferences stay at
  `~/Library/SMUniSync/UniSync.xml`.
- Soundmouse Uploader owns its remembered native-app account and Keychain entry.
  Separately, `--enroll-soundmouse-keychain` stores the SoundMouse website
  username and password for unattended recovery of a fresh web session before
  metadata workbooks are processed. Neither pair is logged or written to the
  repository. The native uploader still requires workspace `UPPM` and module
  `Music` before adding a package.
- DAMS opens a separate private Playwright profile. The user chooses UMG
  Employee SSO during `--setup dams`; normal BMAT runs reuse that retained
  session for read-only album navigation and downloads. DAMS stores its
  application token in browser session storage rather than a persistent
  cookie, so setup snapshots that token into the same private auth directory
  and normal runs restore it before navigating. The snapshot is mode `0600`,
  never enters the repository, and is refreshed only after protected Albums
  access is verified.
- `--enroll-bmat-keychain` collects the SFTP username and password through
  hidden prompts, atomically stores and verifies both values in Login Keychain,
  and never writes either value to argv, the environment, logs, or files.
- `--enroll-synchtank-keychain` collects the AWS access-key ID and secret
  access key through hidden prompts, atomically stores and verifies both values
  in Login Keychain, and never writes either value to argv, the environment,
  logs, or files.
- `--enroll-tunesat-keychain` collects the separate TuneSat SFTP username and
  password through hidden prompts and atomically stores and verifies both in
  Login Keychain without placing either value in argv, the environment, logs,
  or files.
- `--enroll-espn-keychain`, `--enroll-netmix-keychain`,
  `--enroll-soundexchange-keychain`, and
  `--enroll-soundmouse-keychain` collect each
  portal's separate username/email and password through hidden prompts and
  atomically store the exact pairs in Login Keychain. Normal delivery runs
  fill only a uniquely identified login form from memory, then require
  protected portal controls before continuing. A changed or ambiguous login
  UI, rejected credential, CAPTCHA, or MFA challenge remains fail-closed.
- `soundmouse_web_auth.py` enters through `https://app.soundmouse.com/` and
  verifies that the private retained SoundMouse website profile reaches the
  protected UPPM Music page. On a fresh session
  it uses the SoundMouse Keychain pair in memory and fails closed on an
  ambiguous login form, rejected credential, CAPTCHA, or MFA challenge.
- `--enroll-monday-keychain` collects that operator's Monday personal API token
  once through a hidden Terminal prompt and validates it before changing
  Keychain. It uses macOS Security.framework directly rather than putting the
  token in a `security` command or exposing additional Keychain prompts. The token stays in the current
  user's Login Keychain and inherits that Monday user's board permissions; it
  is loaded into memory only for validation and Step 18 API requests. Enrollment
  verifies the stored value exactly; a rejected token is never stored. Monday API eligibility
  also requires an active admin/member account with a confirmed email; viewers,
  disabled users, and users with unconfirmed email addresses cannot use the API.
- Status output is deliberately redacted. It reports only configured/missing
  and whether permissions are private.
- macOS Keychain remains owned by the current user. The workflow reads its two
  Domo SSO items only when a Microsoft account/password form is visible and
  reads the separate API pair only for Domo API connections. It never exports
  or logs their values. UniSync continues to own its own Keychain data.

## Unattended release runs

Setup is the only interactive authentication operation. Normal workflow runs:

- use the Keychain Domo API client for Step 1, BMAT, and SoundMouse exports;
- reuse the private Domo profile only for bounded DataFlow lineage/run recovery;
- reuse the private DAMS profile for bounded silent UMG Employee SSO;
- load BMAT SFTP credentials into memory only for Step 17 and accept/persist a
  first-seen host key while rejecting a changed key on later runs;
- load SynchTank AWS credentials into memory only for its standalone S3
  delivery and upload `delivery.complete` only after exact remote verification;
- load TuneSat SFTP credentials into memory only for its standalone delivery,
  pin the first-seen host key, and reject later host-key changes;
- load ESPN and SoundExchange portal credentials from Keychain only when their
  retained private browser sessions need reauthentication, then verify
  protected destination/catalog controls before any delivery mutation;
- ESPN selects only the canonical package path in Signiant's native folder
  panel, then requires that exact package name to appear in Media Shuttle before
  allowing the final Upload action;
- load the Netmix upload-history login from Keychain when its retained history
  session expires, then verify both the history grid and upload controls before
  staging or uploading a package;
- reuse UniSync's current-user application/Keychain session after each relaunch;
- reuse Soundmouse Uploader's retained current-user sign-in without reading its
  native-app credentials, and load the separate SoundMouse website pair from
  Keychain only when a fresh web session requires sign-in before metadata
  processing;
- close completed UniSync Microsoft `Working...` tabs after each job by exact
  title, host, and UniSync Azure client-ID matching;
- use the same private Domo profile for Step 5's one-time stalled-source
  recovery: follow Card → DataSet → Edit ETL, run the owning DataFlow through
  its three-dot menu, verify the newest History entry is successful, and then
  replace only the affected export;
- load the current user's Monday token from Keychain and pin the API version;
- never prompt for a password, MFA response, or an Enter keypress; and
- allow up to three minutes for UMG's unattended Microsoft→Domo redirect, then
  fail with a redacted setup command if interactive reauthentication is needed.
- verify the result by opening a known protected Domo workspace page; merely
  returning to the Domo hostname is not counted as a successful login.

This provides unattended authentication without making a password available in
the repository, filesystem configuration, environment, process arguments, or
logs. Microsoft can still invalidate a session or require MFA under UMG policy.
That cannot safely be bypassed: rerun the corresponding
`auth_manager.py --setup ...` command outside the release run, then resume the
workflow. Running `python3 auth_manager.py --status` before a scheduled release
confirms that local enrollment exists, but cannot guarantee that a remote SSO
session has not expired.

Directories are forced to mode `0700`; auth/preference files and diagnostic
screenshots are forced to `0600`. Browser children inherit a restrictive
creation mask so new cookie databases are private immediately.

## Offboarding or workstation reassignment

Quit UniSync and any workflow Domo or DAMS browser, then use the recoverable reset:

```bash
python3 auth_manager.py --reset all --confirm-reset
python3 auth_manager.py --delete-domo-keychain --confirm-reset
python3 auth_manager.py --delete-domo-api-keychain --confirm-reset
python3 auth_manager.py --delete-monday-keychain --confirm-reset
python3 auth_manager.py --delete-bmat-keychain --confirm-reset
python3 auth_manager.py --delete-espn-keychain --confirm-reset
python3 auth_manager.py --delete-netmix-keychain --confirm-reset
python3 auth_manager.py --delete-soundexchange-keychain --confirm-reset
python3 auth_manager.py --delete-soundmouse-keychain --confirm-reset
```

Browser/XML artifacts are moved into a timestamped directory in `~/.Trash`, not
permanently deleted. The second command permanently deletes only the two Domo
Keychain items created by this workflow. The third command deletes only the
two workflow-owned Domo API items, and the fourth deletes only the Monday
token. If UniSync still signs in
automatically, use UniSync's own **Sign Out** command to remove its app-managed
Keychain session. Then onboard the next user with the setup commands above.

Never give another operator copies of your Domo browser profile, UniSync XML or
backup, macOS Keychain, local Application Support, or auth-related screenshots.

## Repository protection

`make install` configures `.githooks/pre-commit`. Every commit is rejected if
the staged snapshot contains:

- a corporate UMG-family email identity;
- Chromium cookie/login databases or a Domo profile;
- `UniSync.xml` or its backup;
- high-confidence literal passwords, tokens, API keys, or private keys.

Manual checks:

```bash
make security
make security-history
```

The scanner reports only a coordinate and rule; it never echoes the sensitive
value. `make verify` runs the worktree scan automatically.

## Per-user paths and overrides

Normal user-side paths derive from `Path.home()`. These environment variables
are available when a workstation uses a different layout:

- `UPM_TRACKLISTS_DIR`
- `UPM_EXPORTS_DIR`
- `UPM_LOGS_DIR`
- `UPM_SOUNDMINER_HOST`
- `UPM_SOUNDMINER_USER`
- `UPM_SOUNDMINER_REPO`

Do not use environment variables for passwords, cookies, or tokens.
