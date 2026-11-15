# Extension devices in ASR Studio

Device management belongs to **Settings → Devices** in the existing ASR Studio web app. The extension API runs in the same application process and uses the application's persistent data and auth volumes. A separate prototype server is not part of this deployment.

This delivery adds pairing and device access. Audio ingest, automatic transcript/notes and the installable production client are tracked in [#218](https://github.com/nguyencanhtrung/asr-studio/issues/218), [#219](https://github.com/nguyencanhtrung/asr-studio/issues/219) and [#220](https://github.com/nguyencanhtrung/asr-studio/issues/220). Capabilities report upload unavailable until ingest is implemented. The earlier prototype bundle uses a different protocol and cannot pair with this API.

## Pair a device

Sign into the regular ASR Studio web app. Open **Settings → Devices**, choose how long the device may connect, then create a pairing code. Copy the code into the extension on the client computer. The code's short validity is separate from the selected device lifetime. “Never expires” is an explicit choice for a trusted computer; revoke it in Settings when access is no longer needed.

The list shows pending, active, expired and revoked devices. Creating another code for a selected device explicitly authorizes re-pairing its existing ownership. A newly supplied client device identifier cannot recover another device's ownership. Successful re-pairing invalidates the previous credentials; revoking access also invalidates current sessions. Neither action deletes recordings or cancels server processing.

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

The database also remembers the installation's public identity. After initialization, a missing, corrupt or mismatched private auth file prevents startup; API reads never recreate it. Restore the matching auth state rather than deleting the file to reset pairing. This preserves server identity even before the first device pairs.

## API authority

| Caller | Routes | Authority |
| --- | --- | --- |
| Signed-in operator | `/api/extension/devices`, `/api/extension/pairing-codes`, `/api/extension/devices/{id}/revoke` | Existing web cookie and same-origin policy |
| Extension discovery | `/api/extension/v1/capabilities` | Public metadata only |
| Extension pairing | `/api/extension/v1/pair` | Single-use pairing code and exact extension Origin |
| Extension session exchange | `/api/extension/v1/session` | Device bearer and exact extension Origin |
| Future ingest | `/api/extension/v1/recording-sessions` | Session bearer, current device policy and endpoint binding |

CORS preflight does not grant access. Cookies cannot replace extension credentials, and bearer credentials cannot authorize operator routes. Device expiry, revocation and credential generation are checked when authorizing access, including existing sessions.

Production discovery identifies `asr-extension-v1` and `prototype:false`. The stable extension identity is tied to the bundled public manifest key. Do not change that key when packaging an internal client.

## Verification boundary

Automated tests use disposable state and synthetic requests. They do not establish physical microphone quality, echo behavior or long-meeting reliability. Client microphone checks remain in [#205](https://github.com/nguyencanhtrung/asr-studio/issues/205); long recordings remain in [#206](https://github.com/nguyencanhtrung/asr-studio/issues/206). Backup and storage encryption are deferred plans under [#211](https://github.com/nguyencanhtrung/asr-studio/issues/211).
