# Per-user authentication and privacy

The repository contains workflow code only. Domo and UniSync authentication is
owned by the macOS user running the workflow and must never be copied with the
repo or onto a Pegasus delivery.

## New user onboarding

Run these commands while logged into the recipient's own macOS account:

```bash
cd "$HOME/Documents/Scripts/Python/UPM Release WorkFlow Automation/files"
make install
python3 auth_manager.py --setup domo
python3 auth_manager.py --setup unisync
python3 auth_manager.py --status
```

- Domo opens an isolated Playwright profile and waits for that user's Microsoft
  SSO/MFA. The resulting cookies stay under
  `~/.upm_release_workflow/domo_browser_profile`.
- UniSync opens its installed app. The user signs in there; the workflow never
  collects the credential. UniSync's local preferences stay at
  `~/Library/SMUniSync/UniSync.xml`.
- Status output is deliberately redacted. It reports only configured/missing
  and whether permissions are private.
- macOS Keychain remains owned by the application and the current user. The
  workflow never reads, exports, logs, or deletes Keychain secrets.

Directories are forced to mode `0700`; auth/preference files and diagnostic
screenshots are forced to `0600`. Browser children inherit a restrictive
creation mask so new cookie databases are private immediately.

## Offboarding or workstation reassignment

Quit UniSync and any workflow Domo browser, then use the recoverable reset:

```bash
python3 auth_manager.py --reset all --confirm-reset
```

Artifacts are moved into a timestamped directory in `~/.Trash`, not permanently
deleted. If UniSync still signs in automatically, use UniSync's own **Sign Out**
command to remove any app-managed Keychain session. Then onboard the next user
with the setup commands above.

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
