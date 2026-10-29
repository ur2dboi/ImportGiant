# Deployment and test checklist

> **Experimental:** No app, OAuth exchange, Shopify GraphQL query, package download, or host configuration has been validated against a real Shopify store. The provided TOML examples have not been validated by Shopify CLI against your account. The following is a deployment plan, not a claim that the app is ready for production. Start with two development stores and get a security review before considering a live store.

## 1. Architecture

Create **two distinct Shopify apps** and **two separate instances** of the same codebase. Each instance must have its own app client ID/secret, app URL, allowed shop domain, and private persistent volume. A volume binds itself to the first role/shop that uses it; pointing the other deployment at that volume fails closed. The source does not need destination credentials; the destination does not need source credentials.

| Setting | Source deployment | Destination deployment |
| --- | --- | --- |
| `IMPORTGIANT_ROLE` | `source` | `destination` |
| Shopify app scopes | `read_products,read_inventory` only | `read_products,read_inventory,write_products,write_inventory,write_files` |
| `SHOPIFY_ALLOWED_SHOP` | Original source `*.myshopify.com` domain | Original destination `*.myshopify.com` domain |
| Credentials | Source app client ID and secret | Separate destination app client ID and secret |
| Volume | Private source volume | A different private destination volume |

The source app rejects any non-read app scopes and allows only the fixed shop/status queries and the fixed bulk product-read mutation. A bulk-read job and app installation are administrative activity, even though no source product data is changed. The destination uses the authenticated staff member's online token, checks their scopes, requires a typed domain and explicit acknowledgement, and creates drafts. Source and destination currencies must match.

The allowed domain is the original shop domain in Shopify session tokens/API identity, not necessarily a custom storefront domain or a secondary connected `myshopify.com` domain. Verify it with Shopify rather than guessing from the Domains list. A wrong domain fails authentication.

## 2. Host the Python service

Choose hosting that supports a Docker container, HTTPS, long requests, a **single running replica**, and a persistent writable volume large enough for the source image data, ZIPs, temporary files, destination uploads, and reports. The application listens on `0.0.0.0:$PORT` and provides `GET /healthz`. Its Python process runs as UID/GID `10001`; the mounted directory must be writable by that user and accessible only to authorized operators. The image defaults to `/data`; mounts and existing ACLs may require host-side configuration.

Connect your private GitHub repository to the host using the provider's own authorization flow. Deploy it twice with separate secret/environment settings. Enter real credentials only in the hosting provider's secret manager; do not put them in GitHub, terminal transcripts, screenshots or chat.

| Environment variable | Value |
| --- | --- |
| `IMPORTGIANT_ROLE` | `source` or `destination` |
| `SHOPIFY_API_KEY` | That deployment's Shopify app client ID |
| `SHOPIFY_API_SECRET` | That deployment's Shopify app client secret; secret manager only |
| `SHOPIFY_ALLOWED_SHOP` | That deployment's exact original lowercase `shop.myshopify.com` |
| `DATA_DIR` | Private writable persistent volume path, e.g. `/data` |
| `PORT` | Port assigned by the host, normally `3000` |
| `SHOPIFY_API_VERSION` | `2026-07` by default; API upgrades require testing |
| `PORTER_TRANSFER_MAX_BYTES` | Optional upload/archive safety cap; defaults to 5 GiB |

The server makes new files private on POSIX systems where possible, but it does not repair existing directory permissions or Windows ACLs. Enable host-side disk/backup encryption, TLS and access controls. Avoid proxy logging of request bodies, authorization headers, or Shopify tokens. Configure upload limits and timeouts consistently with the expected package size. Files are not automatically deleted on uninstall.

Do not run multiple workers or replicas, share a volume, or enable automatic live deployments until distributed locking, session handling, job recovery and storage have been redesigned.

## 3. Configure Shopify apps

Use development stores first. Create two distinct embedded apps in Shopify's Dev Dashboard with appropriate distribution/ownership for the stores. Do not silently change or replace an existing installed live app: its settings and permissions may already be in use.

1. Point the **source** App URL at the source deployment's HTTPS URL, mark it embedded, and configure only `read_products,read_inventory`.
2. Point the **destination** App URL at the other HTTPS URL, mark it embedded, and configure the destination scopes listed above.
3. Use Shopify-managed installation and install each app on its intended store. Shopify prompts the merchant to approve the app's requested access. Staff using the app also need the corresponding Shopify permissions. An active configuration/version is not proof that installation succeeded.
4. Open each app from its store's Shopify Admin. App Bridge supplies a short-lived ID token; the server validates it and exchanges it for that user's online token. The Python authentication implementation is custom and must be independently reviewed. It does not use the client-credentials flow that previously returned HTTP 400.

Example configuration files are in `config/`. They are placeholders and are not automatically published. To use the CLI approach, copy them to `shopify.app.source.toml` and `shopify.app.destination.toml` in the project root, fill in the appropriate client IDs/URLs, and link/validate them against the correct two Shopify apps. Those real config files are gitignored by default. Use a current Shopify CLI and its current Node.js requirement (as of CLI 5.0, Node 22.12 or later). Verify the app identity and scope diff before releasing a configuration. Depending on your app/project, Shopify CLI may require additional scaffolding or interactive authentication.

For example, after setup and only after confirming the selected app:

```powershell
shopify app config validate --config source
shopify app config validate --config destination
# Later, after review, an authorized operator can release each correct config:
# shopify app deploy --config source
# shopify app deploy --config destination
```

`shopify app deploy` releases app configuration/extensions, **not** the Python backend. It can alter a Shopify app version, so do not run it on a live app merely to test the example. Shopify CLI validation and deployment require your own authorized login; no Shopify or GitHub account is connected to this workspace.

## 4. Development-store verification

1. Confirm the source installation asks for read scopes only; reject any source write scope. Confirm the destination is a different store and explicitly approve its write scopes.
2. Export a small representative development-store catalog. Check counts, variants, image bytes and each expected product metafield. Check the source catalog was unchanged. Remember an administrative bulk job is created.
3. Test download and upload from the embedded desktop browser, including your expected large file sizes and timeouts. Embedded/mobile download restrictions vary; native form streaming is implemented but has not been verified in Shopify Admin.
4. Review the package and warnings in the destination app. Confirm same-store and currency mismatch are blocked. Attempt without the typed domain and acknowledgement; it must not write.
5. Import only into a disposable development destination. Confirm new products are drafts, existing handles were not modified, expected metafields/media arrived, and the report captures failed or unresolved fields. Test interruption, rerun, permissions revocation, and a large representative package.
6. Arrange independent review of auth, dependency security, hosting and privacy/retention. Only then decide whether to authorize a live source read-only export and later a separate destination import.

On any uncertain write result, do not blindly retry: inspect the destination and the report first. Never request or paste secrets in chat.
