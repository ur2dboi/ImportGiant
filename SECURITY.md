# Security notes and known gaps

This project is experimental. Its custom session verification and deployment setup have not been independently reviewed or verified with Shopify. It must not be used as a production migration service without a security and privacy review, representative development-store tests, secure hosting and operational controls.

## Implemented safeguards

- Separate source and destination roles, app credentials, permitted store domains and bound data volumes. One role/shop per process and volume.
- App Bridge ID tokens verified server-side for HS256 signature, expiration, not-before, audience, subject/session and matching Shopify issuer/destination; ID tokens are exchanged for user-bound online tokens. The role's app and user scopes are checked, with a fresh exchange for import confirmation and download tickets. Tokens are kept in process memory, not deliberately stored in the database/browser or returned in responses.
- Source sessions reject non-read app scopes. A second server-side guard only permits the exact product bulk-read mutation plus the specific shop/status queries; destination clients and import endpoints are denied. Shopify's bulk operation is administrative activity, not a literal absence of all store-side events.
- Destination imports require a separate app/user authorization, matching domain, matching currency, an acknowledgement, and draft creation. Existing handles are skipped. Ambiguous write failures stop rather than retrying automatically. Staged media uploads and products created before a later failure may remain at the destination.
- Uploaded packages are checked for ZIP traversal, duplicates, sizes, checksums, counts and source/destination identity. Demo packages are blocked from destination import. The app uses single-use short-lived download tickets passed in POST bodies rather than URL query parameters.
- `.gitignore` and `.dockerignore` exclude common credentials/runtime data. POSIX process umask restricts permissions on newly created files; an existing mount's permissions and ACLs remain the operator's responsibility.

## Known gaps

- No independent security audit, real OAuth/Shopify installation verification or Shopify review/privacy compliance. Prefer official Shopify libraries/templates where practical and assess this custom implementation before use.
- No encryption for migration ZIPs, staged uploads, reports, or database files at the application layer. Packages are unsigned; checksums are self-declared and cannot authenticate the creator or prevent deliberate tampering. No built-in data deletion/retention management or automatic shop-data removal on uninstall.
- Single process only. In-memory online-token cache, download tickets, locks and jobs are not coordinated between replicas and are lost at restart. Jobs do not resume and can leave partial destination changes; user logout or token expiry may interrupt them. A single store's appropriately authorized staff share operation history/files.
- Shopify UI/frame download behavior, large files, Shopify API schema compatibility, quotas, third-party-owned metafields, proxy upload limits and account permissions require real-store testing. Mobile embedded downloads may be restricted.
- Backend logs and reports may contain catalog handles, metafield values, error details and operation metadata. Configure secure logging, access, backups, retention, and deletion. Do not log authorization headers or request bodies.
- Direct dependencies are pinned to versions exercised in offline tests, not guaranteed free of vulnerabilities; review and update them, their transitive dependencies, the container base image, and GitHub Actions before deployment.
- Gitignore patterns are a convenience, not a secret scanner. Never commit `.env`, app secrets, tokens, database files, migration ZIPs, or actual customer/store data. If anything sensitive is exposed, revoke/rotate it promptly and follow your incident process.

No secrets should be sent in chat. Use the Shopify dashboard and hosting provider's secret manager directly.
