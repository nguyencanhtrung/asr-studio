# Extension devices in ASR Studio

Device management belongs to **Settings → Devices** in the existing ASR Studio web app. The extension API runs in the same application process and uses the application's persistent data and auth volumes. A separate prototype server is not part of this deployment.

This delivery adds pairing and device access plus the bounded `asr-extension-v1` recording-session
ingest API from [#218](https://github.com/nguyencanhtrung/asr-studio/issues/218), together with the
installable internal client and automatic transcript/Notes processing from [#220](https://github.com/nguyencanhtrung/asr-studio/issues/220)
and [#219](https://github.com/nguyencanhtrung/asr-studio/issues/219). The earlier prototype bundle
uses a different protocol and cannot pair with this API.

## Pair a device

Sign into the regular ASR Studio web app. Open **Settings → Devices**, choose how long the device may connect, then create a pairing code. Copy the code into the extension on the client computer. The code's short validity is separate from the selected device lifetime. “Never expires” is an explicit choice for a trusted computer; revoke it in Settings when access is no longer needed.

The list shows pending, active and expired devices newest first; revoked devices are hidden by default. Turn on **Show revoked devices** to review a retained record and issue a re-pair code for the same ownership. The extension exchanges the new device token for an authenticated session before reporting pairing success; this activates the device immediately, without waiting for a recording. If that exchange fails, the extension reports an error; issue a new re-pair code for the same device and try again. Reopen the Devices tab to load the current server status and reset the revoked-device filter.

Creating another code for a selected device explicitly authorizes re-pairing its existing ownership. A newly supplied client device identifier cannot recover another device's ownership. Successful re-pairing invalidates the previous credentials; revoking access also invalidates current sessions. Revocation retains the device record so its recording ownership and explicit re-pair path remain recoverable; neither revocation nor re-pairing deletes recordings or cancels server processing.

The web page receives the pairing code once. It never receives the extension's device/session bearer tokens. Copy copies only the code; the expiry is displayed separately. If the pairing response is lost, generate a new code rather than replaying the consumed code.

## Choose the server address

`ASR_PUBLIC_URL` is the regular web application's configured origin and is allowed for extension access by default. To admit additional extension endpoints, set `ASR_EXTENSION_URLS` to comma-separated origins, without spaces or paths. For example:

```dotenv
ASR_PUBLIC_URL=https://asr.example.com
ASR_EXTENSION_URLS=http://127.0.0.1:8765
```

These addresses are examples, not fixed product endpoints. Additional origins apply only to the extension API; they do not add web login origins or configure a proxy/tunnel. Each endpoint requires its own pairing, including aliases of the same server. The server binds credentials to the approved endpoint and rejects a different Host. Ambiguous endpoint configuration is rejected at startup.

HTTP is allowed only for literal `localhost`, `127.0.0.1` and `[::1]`. On a client computer, localhost means **that client computer**, not a remote server. Remote access requires HTTPS. A TLS reverse proxy such as cloudflared can forward the configured public hostname to the same application; the application uses its configured external origin and does not infer trust from arbitrary forwarded headers. No tunnel is started by this feature.

Compose passes the optional endpoint setting to the app service. Keep the auth and data volumes persistent across restarts. Device identity and a separate private credential-hashing key live in auth storage; device and session state live in the application database. This is credential hashing, not audio/database encryption or backup.

Compose also passes `ASR_EXTENSION_MAX_STAGING_BYTES` and
`ASR_EXTENSION_MAX_ACTIVE_SESSIONS` from `.env`. The staging budget is shared by
all devices and includes chunks for sessions without a durable raw receipt plus
in-flight reservations. A durable raw receipt releases that session's staging
chunks automatically. Remove unneeded incomplete or canceled recordings through
the library to release staging that cannot receive a raw receipt.

`ASR_DATA_DIR` may point through a symlink to an existing data root. Extension
storage resolves that configured root before it checks its managed directories.
Managed child directories must remain private real directories. Child symlinks are rejected.

The database also remembers the installation's public identity. After initialization, a missing, corrupt or mismatched private auth file prevents startup; API reads never recreate it. Restore the matching auth state rather than deleting the file to reset pairing. This preserves server identity even before the first device pairs.

## API authority

| Caller | Routes | Authority |
| --- | --- | --- |
| Signed-in operator | `/api/extension/devices`, `/api/extension/pairing-codes`, `/api/extension/devices/{id}/revoke` | Existing web cookie and same-origin policy |
| Extension discovery | `/api/extension/v1/capabilities` | Public metadata only |
| Extension pairing | `/api/extension/v1/pair` | Single-use pairing code and exact extension Origin |
| Extension session exchange | `/api/extension/v1/session` | Device bearer and exact extension Origin |
| Recording ingest | `/api/extension/v1/recording-sessions` | Session bearer, current device policy and endpoint binding; bounded resumable chunks |
| Failed media retry | `/api/meetings/{meeting_id}/recording/retry` | Signed-in operator cookie and same-origin Origin; bounded requeue only |

CORS preflight does not grant access. Cookies cannot replace extension credentials, and bearer credentials cannot authorize operator routes. Device expiry, revocation and credential generation are checked when authorizing access, including existing sessions.

## Recording-session contract

The create and status responses include the negotiated `limits` object used by the promoted
client. Once the raw archive is durable, the receipt's `recording_id` is the extension session ID
(`upload.serverSessionId`); `recording.id` is the separate library recording-row ID. Clients must
match the receipt to the session ID and use the library ID only for library identity. The
`meeting_url` in an extension session response is an absolute URL under the configured canonical
web origin.

The history and meeting views expose pending, failed and ready artifact state. A failed media
recording can be retried from the operator UI only when its complete raw archive and receipt still
verify. The retry action uses the normal web session cookie and same-origin Origin policy; the
extension bearer is deliberately not an operator-processing credential. It requeues the existing
logical meeting and raw archive, preserves the receipt, and rejects incomplete, canceled, deleted
or foreign-scope recordings. It does not run media processing in the request.
Replaying an extension finalize request only returns the saved outcome; it does
not restart failed media processing. Archive verification for operator retry
runs outside the database write transaction so other library operations can proceed.

Each chunk body has a 30-second idle-read and 300-second total-read deadline. The original body is
limited to the negotiated chunk maximum (8 MiB by default); a replay body is limited to the smaller
of that maximum and the already committed chunk length. A stalled body returns retryable
`chunk_timeout` (HTTP 408), aborts its lease and releases its reservation and session lock, so the
same chunk can be retried and the meeting can be deleted. A timed-out or oversized replay never
rewrites or removes the committed chunk.

Startup recovery is bounded by pending work rather than the retained library: it selects nonterminal
or staging-bearing sessions and skips ready/failed terminal archives without staging. It enumerates
the raw and derivative temp directories once, groups validated session-prefixed names, and removes
only the names still present after taking each session's publication/delete lock. Temp files created
after that enumeration are deferred to a later recovery; this preserves a live archive or conversion
temp file at the cost of possibly leaving one extra interrupted temp until restart.

Ready publication is kept as a connection-aware database seam: the internal helper updates the
recording derivative fields and session state on a caller-owned SQLite connection and performs no
`BEGIN`, `COMMIT`, media, or network work. This leaves #219 room to atomically add workflow intent
without moving automated processing into #218.

Production discovery identifies `asr-extension-v1` and `prototype:false`. The stable extension identity is tied to the bundled public manifest key. Do not change that key when packaging an internal client.

## Verification boundary

Automated tests use disposable state and synthetic requests. They do not establish physical microphone quality, echo behavior or long-meeting reliability. Client microphone checks remain in [#205](https://github.com/nguyencanhtrung/asr-studio/issues/205); long recordings remain in [#206](https://github.com/nguyencanhtrung/asr-studio/issues/206). Backup and storage encryption are deferred plans under [#211](https://github.com/nguyencanhtrung/asr-studio/issues/211).

Receipt retries verify that the complete raw archive still exists with its original digest. If the
archive is missing or corrupt, the API reports `raw_corrupt` instead of authorizing deletion of the
client's local audio. An intact raw archive still receives its original receipt when conversion
fails. Finalize retries and cancellation do not wait for conversion; a busy chunk-file lock returns
a retryable `session_busy` response. These behaviors are tested in
`tests/test_extension_lifecycle.py`.
