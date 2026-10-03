# Post-Packaging Delivery Endpoints

This document records the agreed delivery routing and automation requirements
for packages produced by the UPM release workflow. It is a design record only;
credentials, access tokens, generated passwords, and private recipient details
must never be stored here or elsewhere in the repository.

## Shared delivery rules

- Delivery automation starts only after the relevant final package and
  verification gates have succeeded.
- Every endpoint supports a non-mutating dry run and fails closed on missing,
  ambiguous, duplicate, or unexpected remote content.
- Remote delivery is verified before `delivery_state.py` advances the partner
  from `pending` to its endpoint-specific completion state. For most endpoints,
  a fully verified upload is the `delivered` boundary. SoundMouse is explicitly
  two-phase: native Uploader completion is only `uploaded`; it becomes
  `delivered` only after website metadata processing finishes with zero errors.
- Each run records an auditable receipt containing non-secret remote IDs,
  filenames, sizes, and timestamps.
- Authentication material is collected interactively and stored only in the
  current macOS user's Keychain or another endpoint-approved private store. It
  must never enter argv, environment variables, logs, reports, screenshots, or
  repository files.
- The operator has granted standing authorization for routine connection and
  authentication requests issued by the configured SynchTank and TuneSat
  endpoints during an otherwise authorized delivery. First-seen SSH host keys
  may be accepted and pinned automatically. A changed pinned host key, changed
  destination, expanded permission scope, or unrelated system authorization is
  not covered by this standing authorization and must continue to fail closed
  or request separate approval.
- NTT DATA, JMD/TSS, Qwire, and Scripps run independently on the 1st through
  `monthly_delivery_workflow.py`. Each run covers the complete previous
  calendar month and uses the current delivery month for its folder names,
  filenames, MediaBox/email labels, and subjects. The root is
  `UPM-YYYY-MM-MONTHLY`; the Monday batch is `UPMYYYYMM01`. NTT downloads the
  complete previous-month Japan audio manifest directly, with no carry-forward
  from rolling releases. Rolling runs must not create, export, download NTT
  audio, upload, draft, or mark these four endpoints delivered.

## Sony Ci Media Cloud

Status: design agreed; live implementation is waiting for a Sony Ci developer
key (`client_id` and `client_secret`). Do not paste those values into chat or
commit them to the repository.

Workspace: `UPM-Audio`

Archive layout:

- `Content Updates/<ctx.storage_root>/`
  - Discovery final package
  - Japan NTT DATA final package
  - Japan JMD and TSS metadata package
- `HD Updates/<ctx.storage_root>/`
  - MP3 UDrive package
  - WAV UDrive package
- `SoundMouse Updates/<ctx.storage_root>/`
  - Completed SoundMouse delivery directory, for archive only

The Ci folder name uses the existing workflow storage value
`ctx.storage_root`. The batch folder's Ci metadata must set the field named
`Batch` to the canonical workflow value `ctx.release_id`; folder spelling and
batch identity intentionally remain separate.

### MediaBoxes

Create a new MediaBox for every batch. Existing named MediaBoxes are templates
for discovering recipients and preferences only; never modify or reuse them as
the new delivery. Before creation, fail if an exact-name MediaBox already
exists so an idempotent retry cannot notify recipients twice.

Dynamic MediaBox names:

- `Universal Production Music <release label> - Discovery`
- `UPM Japan <release label> - NTT Data New Release Delivery`
- `UPM Japan <release label> - JMD and TSS Metadata Delivery`
- `Universal Production Music <release label> New Releases - MP3`
- `Universal Production Music <release label> New Releases - WAV`

Here, `<release label>` is the context's client-facing release label, such as
`August 2026 Part 2` or the resolved rolling date range.
For NTT DATA and JMD/TSS after the rolling transition, use their monthly label
instead (for example, `UPM Japan October 2026 - NTT Data New Release Delivery`
and `UPM Japan October 2026 - JMD and TSS Metadata Delivery`). Prepare them in
the standalone first-of-month workflow and do not attach them to a rolling run.

MediaBox policy:

- Discovery and both HD MediaBoxes are Secure.
- NTT DATA and JMD/TSS MediaBoxes are Protected and receive a newly generated
  password for each delivery. Never reuse or log a historical password.
- Email notifications are enabled.
- Expiration is 30 days.
- Source download is enabled for delivered package content.
- Discovery, NTT DATA, and JMD/TSS recipients and other non-secret preferences
  are cloned from their matching existing MediaBoxes through the Ci API.
- HD recipients are not rediscovered from an older MediaBox. Load the approved
  private runtime rosters supplied by the operator: 12 recipients for MP3 and
  8 recipients for WAV. The rosters must never be committed, logged, or copied
  into a receipt. Validate the exact normalized recipient set before notifying;
  fail closed if a roster is missing, contains a duplicate, or contains
  `CIMT-TV`.
- Discovery contains only the current Discovery batch folder.
- NTT DATA contains only the current monthly NTT DATA batch folder, including
  the complete Japan audio and metadata for the previous calendar month.
- JMD/TSS contains the current metadata workbook.
- MP3 and WAV are separate HD MediaBoxes. Each contains its corresponding
  UDrive folder, the release album-list PDF, and the U-Drive user guide.
- SoundMouse is never sent through a MediaBox; its Ci copy is archive-only.

After the protected monthly NTT DATA and JMD/TSS MediaBoxes have been created,
uploaded, and verified, create two Outlook drafts. These are draft-only
handoffs: do not send them automatically. Resolve the approved recipient and
CC from private operator configuration; identities must not enter repository
documentation. Subjects use the
**content month represented by the package**, not the following month's
first-of-month execution date:

- JMD/TSS: `UPM Japan JMD / TSS Data <content month> Delivery`
- NTT DATA: `NTT DATA <content month>`

For example, the October 1, 2026 monthly run contains September content, so
the subjects are `UPM Japan JMD / TSS Data September 2026 Delivery` and
`NTT DATA September 2026`. Do not add `Part 1` or `Part 2` to a normal monthly
subject.

JMD/TSS body:

```text
Hi Jonny,

Please see the link below for the most recent UPM metadata for JMD and TSS

If there are any issues, please let me know.

<current JMD/TSS MediaBox link> | Password: <current JMD/TSS MediaBox password>

Best,
Joe
```

NTT DATA body:

```text
Hi Jonny,

Please see the link below to download the latest releases for NTT Data. If there are any issues, please let me know.

<current NTT DATA MediaBox link> | Password: <current NTT DATA MediaBox password>

Best,
Joe
```

Always copy the link and newly generated password from the exact MediaBox just
created for that monthly package. Never reuse a historical link or password,
and never place either password in repository data, logs, or receipts.

Both HD MediaBoxes use this notification message, with the first line populated
from the context's client-facing release label:

```text
<release label>

Please see the link below to download the latest releases from Universal Production Music.

If you have any questions, please reach out to: <delivery contact name> (<delivery contact email>).

**We are now requiring users to log in with a SonyCi account to access our updates. Please create a FREE account before downloading.**
```

The delivery contact comes from private runtime configuration. Preserve the
wording, paragraph breaks, capitalization, `SonyCi` spelling, and the literal
double-asterisk markers shown above. Do not insert a historical month/part;
always derive `<release label>` from the active context.

## Other delivery routes

- ESPN: deliver through the authenticated ESPN Media Shuttle Share portal at
  `https://espn-file-transfers-shr.mediashuttle.com/memberLogin`. The portal
  credential pair is enrolled with
  `python3 auth_manager.py --enroll-espn-keychain`; it remains in Login
  Keychain and is filled only when the retained private session has expired.
  The writable destination is `from_killer_tracks/`. Upload the complete final ESPN folder
  as one top-level item, named exactly
  `Universal Production Music <release label> - ESPN`; its existing `Music/`
  label/album hierarchy must remain intact. The installed Signiant App is the
  preferred transport for the directory upload, but it is only the transfer
  engine: the browser must first hold an authenticated ESPN portal session and
  initiate the authorized upload. The desktop app cannot independently choose
  or authenticate to this ESPN Share destination. Do not upload the files
  individually or flatten their paths. Before starting, reject an existing
  exact-name destination unless it is a verified resumable transfer. Completion
  requires Media Shuttle's **My Transfers** history to show
  `Uploaded 1 file(s)` for that exact folder name and the destination folder to
  be visible beneath `from_killer_tracks/`. `Transfer interrupted` is not
  success and must be resumed and reverified before delivery state advances.
  Media Shuttle keeps authenticated content on the `/memberLogin` route, so
  authentication must be verified from the portal controls rather than the
  URL. A supervised run uses `--interactive-native-selection`: after the
  adapter opens the exact writable destination and starts Signiant, select the
  exact canonical package in the native picker. The adapter verifies the
  staged folder name before it clicks the portal's final Upload button.
- Netmix: deliver through the authenticated Music Tracker browser portal at
  `https://www.cndmusictracker.com/auth/web/`. Use **Upload Music** to select the
  complete final Netmix folder as one master directory. The browser uploader's
  directory picker preserves the current `Music/<label>/<album>/` hierarchy and
  accepts the existing single metadata CSV plus WAV/AIFF audio and one JPG/PNG
  cover in each audio folder. This route therefore preserves the exact package
  already produced by Step 10 and does not require temporary cover URLs.
  Before submission, require exactly one CSV, unique audio basenames, one CSV
  row per audio file, an exact `Filename` match, and no album directory with
  zero or multiple cover images. Monitor browser-transfer completion, then use
  **View Uploads** and the dashboard's processing counters to confirm that the
  batch was accepted for ingestion; do not mark the partner uploaded while any
  track from the submitted manifest remains rejected or unaccounted for.

  The guarded portal adapter is implemented in `netmix_portal_delivery.py` and
  is available through the unified post-packaging runner. It stores only a
  private retained browser session, never credentials. The transport boundary
  is API-first: when a future API gateway is configured it is always attempted
  first, and the portal may be used only when that gateway explicitly reports
  that no remote mutation occurred and fallback is safe. An uncertain API
  result blocks a duplicate portal upload.

  The two published API pages are complementary, not alternate formats:
  **Upload Flow** defines login, `/api/v1/import/upload`, and asynchronous status
  checks, while **Upload JSON format** defines the per-track metadata body. API
  delivery is the intended future replacement if it supports the full existing
  package. API transport remains deferred pending CND access; the guarded
  portal transport is active in the meantime. Request all of:
  API enablement for the Universal Production Music account, the API base URL,
  a dedicated username/password, the account-specific Postman collection and
  PHP example, documented multipart form-field names, rate/concurrency limits,
  retry/idempotency guidance, and retention rules for upload/status records.
  Also ask CND to confirm how local JPG/PNG album covers should be delivered by
  API: direct multipart image upload, another endpoint, embedded artwork, or a
  temporary signed `url_album_image`. Do not use unrelated existing S3 buckets
  to publish covers without explicit authorization. Enroll the retained portal
  session once with `python3 netmix_portal_delivery.py --setup-auth`; live
  delivery still requires the exact release confirmation and workflow gates.

  When access is received, store the API credentials only in the current
  macOS user's Keychain and validate them against `/api/v1/login` before
  replacing the portal route. The candidate API implementation will upload
  each WAV directly with its mapped JSON rather than publishing audio URLs,
  use `Trackid` as stable `id_client`, map rows by unique `Filename`, retain
  every returned `upload_id`, and poll `/api/v1/import/list?upload_id=...`
  until every track has a non-null `ingest_id`. Any non-null `last_error`,
  missing track, duplicate mapping, or unresolved cover blocks delivery state.
  Keep the browser master-folder uploader as the fallback until a synthetic
  test and one explicitly approved live pilot demonstrate full audio,
  metadata, and artwork parity.
- Qwire: send the final Qwire custom-metadata CSV as ordinary Outlook message
  attachment(s) to `libraries@qwire.com`, with no CC or BCC unless separately
  instructed. Each CSV may contain at most 500,000 data records, excluding its
  header row. If the delivery exceeds that limit, split it deterministically
  into numbered CSV files of no more than 500,000 records each, repeat the
  original header in every part, preserve the original row order and values,
  and verify that the union of the parts exactly equals the source file with no
  missing or duplicate rows.

  Qwire is delivered only on the shared monthly metadata trigger described
  above. Its folder, CSV filename, and subject use the current calendar month
  and year, while the CSV contains exactly the previous calendar month's
  content.

  Subject:

  `Universal Production Music - <release label> Metadata Delivery`

  Example for an October 1 monthly run:
  `Universal Production Music - October 2026 Metadata Delivery`.

  Message body before the signature:

  ```text
  Hi,

  Please see attached for the latest metadata from Universal Production Music.

  If you have any questions, please let me know.

  Thanks,
  ```

  Create the draft through the connected Outlook Email plugin. Its direct
  attachment writer accepts files smaller than 3 MiB. When the final CSV is at
  or above that limit, create a lossless ZIP containing only that unchanged
  CSV, verify the archive and contained filename, and require the ZIP itself to
  be below the plugin limit. Use the approved plain-text rendering of the
  operator's signature; the plugin cannot reproduce Exclaimer images or rich
  styling. Keep signature identity data out of repository files and logs.
  Before sending, verify the exact recipient, subject, body, attachment
  filename, attachment count, contained CSV filename and byte identity,
  per-part record counts, and that no unrelated attachment is present.
  Treat the final Send action as an external communication requiring the
  specific batch to be authorized. After sending, verify one matching message
  in Outlook Sent Items before writing a private receipt and marking Qwire
  delivered.
- Scripps: send the final **Scripps custom metadata** CSV through Outlook to
  `patrick.magee@scripps.com`. This is a metadata-file-only **FullPull**
  delivery: attach the single current Scripps CSV and no audio, artwork,
  album-list document, or unrelated file.

  Scripps is delivered only on the shared monthly metadata trigger described
  above. Its folder, CSV filename, and subject use the current calendar month
  and year, while the CSV contains exactly the previous calendar month's
  content.

  Subject:

  `Universal Production Music - <release label> Metadata Delivery`

  Example for an October 1 monthly run:
  `Universal Production Music - October 2026 Metadata Delivery`. Use the
  workflow's normal client-facing capitalization and do not include a leading
  space before `Universal`.

  Message body before the signature:

  ```text
  Hi,

  Please see attached for the latest metadata from Universal Production Music.

  If you have any questions, please let me know.

  Thanks,
  ```

  Create the draft through the connected Outlook Email plugin. Its direct
  attachment writer accepts files smaller than 3 MiB. When the final CSV is at
  or above that limit, create a lossless ZIP containing only that unchanged
  CSV, verify the archive and contained filename, and require the ZIP itself to
  be below the plugin limit. Use the approved plain-text rendering of the
  operator's signature; the plugin cannot reproduce Exclaimer images or rich
  styling. Keep signature identity data out of repository files and logs.
  Before sending, verify the exact recipient, subject label, body, one CSV or
  one lossless single-CSV ZIP attachment, and absence of CC, BCC, or unrelated
  attachments. Treat the final Send action as an external communication
  requiring the specific batch to be authorized. After sending, verify one
  matching message and attachment in Outlook Sent Items before writing a
  private receipt and marking Scripps delivered.
- SoundExchange: deliver through SoundExchange Direct at
  `https://sxdirect.soundexchange.com/login/?next=%2Fcatalog%2Fsubmit%2F`.
  Enroll its separate portal pair with
  `python3 auth_manager.py --enroll-soundexchange-keychain`. The normal adapter
  first reuses its private retained session and otherwise performs a bounded
  Keychain-backed sign-in before requiring protected catalog controls.
  Reuse the user's retained browser login, then always navigate through
  **My Catalog → Submit Recordings**. Do not hardcode or deep-link a historical
  Rights Owner URL: select and reverify the displayed registrant, registrant
  ID, and Rights Owner on every run because the live MGB route has already
  differed from a previously supplied direct link.

  Process these two registrants independently and in this order:

  1. Registrant `Universal Music - Mgb Na Llc`, ID `2178141344`, currently
     displayed under Rights Owner `Universal Mgb_Na_Llc`. Bulk-import every
     current final-package workbook matching
     `ISRC Ingest Form - MGB NA LLC - Part <n>.xlsx` in ascending part order.
  2. Registrant `Universal Music – Z Tunes, Llc`, ID `2178141458`, currently
     displayed under Rights Owner `Firstcom Music`. Bulk-import every current
     final-package workbook matching
     `ISRC Ingest Form - Z TUNES LLC - Part <n>.xlsx` in ascending part order.

  For each registrant, first require a clean recording-entry page with zero
  pending recordings so data from another run cannot be mixed into the batch.
  Upload the associated workbook(s) through **Bulk Import** and wait for every
  part to finish its real-time validations. The combined on-screen recording
  count must equal the combined nonempty data-row count in those workbooks;
  every entry must be valid, and no unexpected ISRC may appear. Invalid,
  rejected, missing, duplicate, or still-processing entries block submission.
  After processing and before submission, if any entry is invalid, write a
  private invalid-entry audit containing the registrant name/ID and Rights
  Owner, source workbook part, on-screen entry number, track title, ISRC, and
  every displayed validation message. Include the invalid and total counts and
  timestamp, but no login/session data. Log only the audit path and aggregate
  counts. Do not silently correct or remove invalid entries, and never click
  **Submit Recordings** while any invalid entry remains; leave resolution to an
  explicitly authorized recovery run.
  Only after those checks may the green **Submit Recordings** button be used
  once for that registrant. Then verify the submission through
  **My Catalog → Upload History → View Upload History** before clearing the
  local checkpoint and beginning the other registrant. A failure for either
  registrant leaves SoundExchange incomplete and must not advance overall
  delivery state. Record both registrant IDs, input part filenames and hashes,
  row counts, submitted recording counts, and non-secret history identifiers
  in the private receipt.
  SoundExchange may keep the authenticated application on its login URL and
  may not preserve a usable session across a separate setup/reopen. The
  Keychain-backed sign-in therefore occurs in the same browser process as both
  submissions. Use `--interactive-login` only when the portal requires a
  supervised challenge. Bulk Import begins when the
  `data-cy=bulk-upload-file` input is populated; there is no second Import
  button. Validation reads the Angular summary model rather than the
  virtualized 20-row table, and Upload History is accepted only after its
  generated CSV exactly matches the expected ISRC set.
- SourceAudio US and Ex-US: migrate delivery from the portal to SourceAudio's
  Import API. Status: design agreed; implementation and live validation are
  deferred until the dedicated API token and an approved HTTPS staging service
  are available. SourceAudio is a catalog-critical endpoint: incorrect routing
  of even one label, album, work, or track is a delivery failure and may expose
  content in the wrong catalog. Favor staging and operator review whenever the
  final placement cannot be proven before publication. The confirmed US portal
  and API base domain is
  `https://upm-us.sourceaudio.com`; the interactive login route is
  `https://upm-us.sourceaudio.com/login.php`. Both territories live on this
  same SourceAudio site but must be routed to different final catalogs:
  `Universal Production Music - US` (catalog ID `3840`) and
  `Universal Production Music - Ex-US` (catalog ID `19593`). Catalog ID `21143`
  is the site's `Staging Area`, described in the portal as temporary storage
  for uploads. It is used by the manual portal workflow, where tracks are
  published to staging first, metadata is added afterward, and only then are
  tracks moved to their final catalog. The API may bypass this intermediate
  catalog only after a non-production capability check proves that the site's
  API can apply and read back the complete metadata, artwork, and intended final
  catalog assignment before publication. If that capability is absent,
  incomplete, or uncertain, the API must publish into `Staging Area` first and
  use the same metadata-validation and final-routing safety gate as the manual
  process. The documented upload endpoint imports each audio file from an
  externally hosted URL and requires track uploads to run serially; it does not
  accept the local AIFF package directly.
  Do not reuse another partner's S3 bucket or expose a permanent public URL.

  Create a dedicated SourceAudio API token for this workflow and grant only the
  permissions required for Import plus the minimum read access needed to
  reconcile and verify tracks. Do not grant track deletion, user management,
  download, licensing, playlist, or unrelated administrative access. In
  particular, the token must not be able to call `/api/tracks/delete` or
  `/api/import/delete`; the latter accepts `upload_id=ALL` and can clear the
  entire import queue. Enroll the token
  through a hidden `auth_manager.py` prompt into the current macOS user's Login
  Keychain and validate it with a non-mutating API request. Send it only in a
  POST/JSON request body; never in a URL, argv, environment variable, log,
  report, screenshot, or repository file. A permission expansion or token
  replacement requires explicit operator approval.

  Use a new, explicitly named API job for each release and territory: one for
  SourceAudio US and one for SourceAudio Ex-US. Before uploading, validate the
  exact AIFF manifest against its territory's metadata CSV, reject missing or
  duplicate stable External IDs and filenames, and obtain the current import
  fieldset from `/api/import/getAvailableFields` rather than hardcoding it.
  SourceAudio API field keys use the standard fieldset even if the site's
  display terminology is renamed. Never send system-managed `SourceAudio ID`
  or `Duration` through the Import API. Treat an absent field and a blank value
  differently: omit fields that are not being changed, because a blank value in
  an imported field can delete the existing value. Enforce the documented field
  lengths and multi-value delimiters before upload.

  The supplied metadata includes `Master Filename` and
  `Nesting Sort Position`. Treat alternate-version nesting as its own mandatory
  verification gate. Manual testing has shown that pre-publish metadata does
  not trigger nesting, but the API may bypass the manual limitation only if a
  controlled capability test proves that it can establish and read back both
  the exact master/alternate relationships and their order before tracks become
  visible in a final catalog. A successful metadata response alone is not proof.
  If the API passes that test, no staging or post-publish metadata reapplication
  is required. Otherwise publish new tracks only into `Staging Area`, resolve
  each master and alternate to its assigned SourceAudio ID, and reapply the
  nesting metadata to the published tracks through the Tracks API by explicit
  `track_id`. Where the API
  requires `Master ID`, derive it only from the exact staged track whose filename
  equals `Master Filename`; never guess or use a title match. Apply
  Tracks-API-only `Nesting Sort Position` after that relationship exists.

  Before changing any final catalog placement, reject missing or duplicate
  master filenames, orphan alternates, self-references other than an intentional
  master identity, cycles, cross-album masters, and any relationship spanning
  the US and Ex-US jobs. Verify each staged group through `master_id` searches
  and `/api/tracks/getMixes`, including its expected master, exact alternate set,
  and order. Because `Nesting Sort Position` is set-only and is not returned by
  search or `getById`, require an ordered `getMixes` result or a retained manual
  portal verification for any group whose order cannot be proven by API.
  The selected final-package directory is authoritative for territory: the US
  package may target only catalog `3840`, and the Ex-US package may target only
  catalog `19593`. The mapped metadata is the mechanism that places each track
  into its correct catalog, label, album, and work hierarchy. Validate every
  placement encoded by the metadata against the expected package territory
  before allowing it to move content. Reject a mixed job; any row whose catalog,
  label, album, work, External ID, or existing remote association conflicts
  with the package territory must block the entire job before final publication.
  For API jobs, apply and read back all metadata before publishing when the site
  supports it. If the capability test also proves complete pre-publish nesting,
  publish directly to the intended final catalog. Otherwise run the nesting
  phase after publication in `Staging Area`; reapplying the complete validated
  metadata to those published tracks must establish nesting and move content
  from catalog `21143` into catalog `3840` for US or catalog `19593` for Ex-US.
  A job
  containing a wrong, missing, or ambiguous placement value must fail closed
  before that move. Direct final
  publication remains disabled until the capability check above has passed and
  the operator has explicitly approved enabling it; staging is the default.
  Stage each AIFF—and any artwork required by metadata—behind a unique,
  short-lived HTTPS URL whose lifetime safely exceeds SourceAudio processing.
  Never enable `autopublish`.

  Reconcile by stable External ID before each import. New tracks must not
  collide with an existing remote filename or ID; filenames must be unique
  because SourceAudio uses them to match third-party metadata when no
  SourceAudio ID is present. After initial publication, capture the assigned
  SourceAudio ID and bind it to the local External ID in the private receipt.
  Corrections may replace an existing track only by resolving that binding and
  supplying the explicitly verified `track_id`; never rely on SourceAudio's
  implicit same-filename replacement behavior. Remote
  removals remain manual and must stay in the existing SourceAudio Missing
  Audit; this automation must never delete remote tracks.

  Call `/api/import/upload` serially, checkpoint every returned `upload_id`,
  and apply the complete mapped metadata and artwork to the unpublished API job.
  Force `autopublish=0` on every upload and never use the site's shared default
  queue. SourceAudio's automatic mode can trigger after ten minutes without a
  new autopublish upload or when 1,000 tracks have accumulated, while a normal
  UPM US batch can exceed that count. The explicit dedicated `job_id` is
  therefore a hard containment boundary.
  Treat a nonempty API `error` field as failure even when the HTTP status is
  successful. Before publishing, verify every expected metadata value,
  External ID, master filename, track count, and intended territory catalog.
  When pre-publish metadata, routing, nesting, and order have all been proven,
  publish the dedicated territory job directly to its intended final catalog.
  Otherwise publish only to catalog `21143`, finish and verify post-publication
  metadata and nesting there, and promote it only after the staged manifest and
  every nesting group are exact. Poll
  `/api/import/checkJob` until no item is queued, in progress, or errored. Use
  `/api/import/list`, `/api/tracks/getByIds`, and catalog-filtered read-only
  searches to verify that US
  tracks are live only in catalog `3840`, or Ex-US tracks only in catalog
  `19593`, with no unexpected placement in `21143` or the other territory
  catalog. Search responses are limited to 1,000 tracks, so paginate until the
  reported result set is exhausted; never validate a large batch from only the
  first page. Final success requires a one-for-one read-back of every expected
  track and its catalog, label, album, work, and External ID associations. Any
  partial promotion or mismatch stops the endpoint immediately, leaves the
  other territory untouched, records a private exception audit, and requires
  operator review; never attempt an automatic compensating move or deletion.
  Never combine US and Ex-US tracks in one publish operation. Remove temporary
  externally hosted objects only after final verification has passed. Record
  separate US and Ex-US private receipts and delivery-state checkpoints so a
  retry cannot duplicate or prematurely publish a job.

  SourceAudio's hierarchy allows a track to occupy only one catalog, label, and
  album at a time. Post-publication verification must therefore prove the one
  intended hierarchy rather than merely finding the track somewhere on the
  site. Metadata indexing can be delayed for large batches; bounded monitoring
  may remain pending for up to the documented one-day worst case, but must never
  convert an unverified placement into success.

  The documented API changelog currently ends on January 30, 2020 and records
  only older documentation/API additions, while individual API pages have been
  updated more recently. Do not use the changelog as proof of present behavior;
  re-discover the live site fieldset and run non-production capability checks
  before enabling delivery. SourceAudio's DDEX ECHO/ERN ingestion is a separate,
  site-enabled delivery method and is not a fallback for this API integration.
- SoundMouse: first transfer the complete Step 16 package through the SoundMouse
  Uploader application. Its Ci copy is for archive purposes only. Always
  explicitly select and verify workspace `UPPM` and module `Music` before
  adding files. Native upload success requires the app's new queue rows to
  match the exact local package manifest and every row to reach completed
  status; this writes the uploader receipt and marks SoundMouse `uploaded`, not
  `delivered`.

  The second phase runs in the SoundMouse website. Process every metadata
  workbook included in the exact uploaded package, require a terminal result
  for each workbook, and require zero processing errors across all workbooks.
  Missing, duplicate, still-processing, warning-as-error, rejected, or
  unaccounted-for sheets block completion and leave SoundMouse `uploaded`.
  Only a website-processing receipt containing the exact workbook manifest and
  zero-error results may mark SoundMouse `delivered`. An Uploader receipt alone
  can never be acknowledged or promoted to delivery.
- SynchTank: deliver each complete package beneath an Amazon S3 prefix exactly
  matching the local package-folder name. Historical package prefixes remain
  in the bucket and are outside the active manifest. Upload
  `delivery.complete` inside the active package prefix only after every package
  object has passed remote size verification. The trigger name exactly matches
  BMAT's lower-case `delivery.complete` marker.
  The AWS access-key ID and secret are private credentials and must be enrolled
  through hidden Keychain prompts; neither value belongs in this document.
- Tunesat: deliver by SFTP on port 22 to the account's `/AudioFiles` remote
  path. Upload the complete partner package beneath a folder exactly matching
  the local package-folder name, so `Music/` and `Metadata/` appear inside that
  batch folder. Preserve historical package folders alongside it and do not
  add a completion marker. The server address is `upload.tunesat.com`. The username and password
  are private credentials and must be enrolled through hidden Keychain prompts;
  neither value belongs in this document.

## Unified runner

`post_packaging_delivery.py` is the common post-packaging entry point. Its
default mode is a non-mutating plan; live execution requires both `--execute`
and `--confirm-live-release <exact release id>`. It supports comma-separated
endpoint selection, refuses duplicate submissions for partners already marked
`delivered`, invokes the standalone upload/browser/connector modules, and
marks a fully verified upload `delivered` immediately for single-phase
endpoints. SoundMouse intentionally remains `uploaded` between native transfer
and zero-error website metadata processing; `--acknowledge-delivered` may not
bypass that processing gate. For other endpoints the legacy `uploaded` state
and acknowledgement command remain only to migrate older verified records.
After every successful live endpoint mutation or migration,
the runner immediately invokes the Monday API synchronizer and verifies the
resulting board statuses. A Monday failure is reported as a failed runner
result without rolling back an already verified remote upload. Pass
`--delivery-date YYYY-MM-01` to target the independent monthly release context;
the same selector is accepted by `delivery_state.py` for inspection or guarded
status maintenance.

ESPN and SoundExchange have completed their first production pilot. Their
visible adapters retain fail-closed checks while exposing bounded supervised
flags for the native Signiant picker and one-session SoundExchange login.
Qwire and Scripps expose an Outlook gateway boundary and write a
private, exact connector handoff. `outlook_connector_bridge.py` and the
repository's `outlook-delivery-bridge` skill consume that handoff through the
connected Outlook Email app. The app supports draft creation and inspection
but not Send, so the bridge uses native Outlook only for the separately
authorized final send action, then returns to the connector for exact Sent
Items verification. Preparing a handoff or draft never counts as sending.
Sony Ci remains visible as credential-blocked rather than being silently
skipped. Netmix is implemented with an API-priority portal fallback while CND
API access is pending. SourceAudio remains owned by its dedicated worktree
until that implementation is merged.
