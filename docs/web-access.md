# Web access and GUI origins

ASR Studio keeps `ASR_PUBLIC_URL` as the canonical GUI origin. It uses that origin for the default
CLI link, extension web/result links and the legacy browser session. `ASR_WEB_ORIGINS` adds explicit
GUI origins for the same installation. The application does not discover origins from request
headers, subnets, forwarded headers or Tailscale identity headers.

## Local loopback access

Compose publishes the app only on `127.0.0.1`. The default origin is:

```dotenv
ASR_PUBLIC_URL=http://127.0.0.1:8765
ASR_WEB_ORIGINS=http://localhost:8765,http://[::1]:8765
```

HTTP is valid only for the literal loopback names `localhost`, `127.0.0.1` and `[::1]`. A loopback
alias can use HTTP even when the canonical origin is HTTPS. Adding an origin does not create a
listener: the default Compose publication is IPv4-only, so `[::1]` requires a separately configured
IPv6 loopback listener. Do not apply a blanket HSTS policy to
loopback hosts when HTTP and HTTPS loopback ports must coexist.

## LAN or VPN access through a TLS proxy

Keep the Compose bind on loopback. Terminate TLS at a reverse proxy and preserve the configured
Host header. Configure every browser-facing HTTPS origin explicitly:

```dotenv
ASR_PUBLIC_URL=https://asr.example.test
ASR_WEB_ORIGINS=https://10.20.30.40,https://asr.vpn.example.test
```

The LAN or VPN proxy must use a certificate valid for each configured hostname or IP address. It
must redirect or reject remote plaintext HTTP before the request can reach the login UI. Do not
expose the Compose port on `0.0.0.0`. Do not enable forwarded-header trust in Uvicorn or the app.
The backend can receive HTTP from the local proxy because the application selects the configured
origin from the preserved Host header.

Network reachability is separate from GUI trust. An address is accepted only when it is in the
configured origin set. A valid certificate does not replace the access token or browser session.

## Private Tailscale Serve

Use a private Tailscale HTTPS hostname as one explicit GUI origin:

```dotenv
ASR_PUBLIC_URL=https://asr.tailnet.example
ASR_WEB_ORIGINS=https://asr.vpn.example.test
```

Point private Tailscale Serve at the loopback Compose port and preserve the Tailscale Host header.
Keep Funnel disabled unless a separate deployment decision authorizes public exposure. Tailscale
network identity does not replace ASR Studio authentication. The app does not trust Tailscale
identity headers or infer a tailnet from the client address.

## Origin rules

- `ASR_PUBLIC_URL` and each `ASR_WEB_ORIGINS` value must be an exact HTTP or HTTPS origin.
- The origin cannot contain credentials, a wildcard, a control character, a path, a query or a
  fragment. The application normalizes the scheme, hostname and effective port.
- Remote HTTP origins are rejected. HTTP is limited to the three literal loopback hosts.
- The application rejects duplicates and Host authorities that could select two origins.
- A configured GUI origin does not become an extension API endpoint. Configure extension endpoints
  separately with `ASR_EXTENSION_URLS`, and pair each extension device with its intended endpoint.
- Unknown Host values, extension-only Host values and forwarded-header substitutions remain
  rejected.

## Login, cookies and rotation

Open the canonical link with:

```sh
docker compose exec -T app python -m asr_studio.cli access show
```

Select a configured GUI alias before the CLI reads authentication state:

```sh
docker compose exec -T app python -m asr_studio.cli access show \
  --origin https://asr.tailnet.example
```

The command rejects an unconfigured origin and prints no access link. The token remains in the URL
fragment until the access page exchanges it in a JSON request. The page removes the fragment before
the request and the token does not become a query parameter or cookie.

Each configured origin gets a distinct host-only session cookie. The canonical cookie keeps the
existing `asr_studio_session` name. Alias cookies use deterministic origin-specific names. New
sessions are signed v2 sessions bound to the normalized origin. An old v1 session can work only on
the canonical origin. A cookie copied to another scheme, host or port is rejected, even if its name
is changed. HTTPS origins set `Secure`; HTTP loopback aliases do not unless `ASR_COOKIE_SECURE=true`.
All cookies remain `HttpOnly`, `SameSite=Strict`, `Path=/`, and omit `Domain`.

Browsers scope cookies by hostname and path, not port. Distinct names prevent session collisions;
the signed origin binding prevents ASR Studio from accepting a session at another GUI origin.
It does not make cookie delivery private to a port. Use different hostnames for services with
different trust boundaries.

Rotate the installation token with the existing command:

```sh
./scripts/rotate-token.sh
```

Rotation changes the authentication generation. It invalidates old links and every session format
without deleting recordings, transcripts, provider data or other application data.

## Provider callback

The ChatGPT callback remains a separate exact HTTP loopback URL, for example
`http://127.0.0.1:8765/auth/chatgpt/callback`. A callback Host exception applies only to the
callback GET route. It does not authorize the access page or another GUI route. When the browser
cannot reach the loopback callback, use the existing callback paste flow from the configured GUI.
