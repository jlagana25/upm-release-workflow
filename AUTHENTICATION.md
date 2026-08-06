# Per-user authentication and privacy

The repository contains workflow code only. Domo and UniSync authentication is
owned by the macOS user running the workflow and must never be copied with the
repo or onto a Pegasus delivery.

## New user onboarding

Run these commands while logged into the recipient's own macOS account:

```bash
cd "$HOME/Documents/Scripts/Python/UPM Release WorkFlow Automation/files"
make install
python3 auth_manager.py --enroll-domo-keychain
python3 auth_manager.py --setup domo
python3 auth_manager.py --setup unisync
python3 auth_manager.py --status
```

- `--enroll-domo-keychain` asks macOS Keychain itself to collect and confirm the
  UMG email, then the password, through labeled hidden Terminal prompts. Python
  never receives the enrollment values, and they never enter command arguments.
  They are stored as two workflow-owned items in the current user's Login
  Keychain.
- Domo opens an isolated Playwright profile and waits for that user's Microsoft
  SSO/MFA. The resulting cookies stay under
  `~/.upm_release_workflow/domo_browser_profile`. Normal runs can select the
  enrolled account and fill the password from Keychain entirely in memory.
- UniSync opens its installed app. The user signs in there; the workflow never
  collects the credential. UniSync's local preferences stay at
  `~/Library/SMUniSync/UniSync.xml`.
- Status output is deliberately redacted. It reports only configured/missing
  and whether permissions are private.
- macOS Keychain remains owned by the current user. The workflow reads only its
  two Domo items when a Microsoft account/password form is visible and never
  exports or logs their values. UniSync continues to own its own Keychain data.

## Unattended release runs

Setup is the only interactive authentication operation. Normal workflow runs:

- reuse the private Domo profile and allow Microsoft/Domo silent SSO to finish;
- reuse UniSync's current-user application/Keychain session after each relaunch;
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

Quit UniSync and any workflow Domo browser, then use the recoverable reset:

```bash
python3 auth_manager.py --reset all --confirm-reset
python3 auth_manager.py --delete-domo-keychain --confirm-reset
```

Browser/XML artifacts are moved into a timestamped directory in `~/.Trash`, not
permanently deleted. The second command permanently deletes only the two Domo
Keychain items created by this workflow. If UniSync still signs in
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
