# ImportGiant — experimental Shopify product migration

This is an **experimental, single-store Shopify App Home prototype** for moving products from one Shopify store to another through the app. It is not a production-ready Matrixify replacement. The Shopify authentication, Shopify API calls, app installation, deployment configuration, and embedded downloads have **not been verified against a real store** or independently security-reviewed.

It has passed offline tests with simulated Shopify responses. Do not run a destination import on a valuable store until the deployment, permissions, downloads, and a representative migration have been validated on separate development stores and reviewed by a qualified developer. The previous live-store client-credentials HTTP 400 has not been resolved, and this new app has not been installed or tested there.

## What it does

1. **Source app: read-only export.** In Shopify Admin, an authenticated staff member starts a Shopify bulk product read. The app creates a ZIP containing product records, variants, image files, and accessible **product-level** metafield values. Source permissions must be read-only; the backend blocks other Shopify GraphQL operations. Starting a bulk read creates an administrative bulk-operation job, and installing the app changes the installed-app list, but the app does not modify source catalog data.
2. **Review.** Download the unencrypted ZIP and check its summary and warnings. The destination checks its structure, counts, image checksums, and declared source identity again before importing. Packages are not signed: these checks detect some corruption but do not authenticate who created or changed a package.
3. **Destination app: separately authorized import.** Upload the ZIP in the other store. Explicitly type the destination domain and acknowledge writes. New products are created as **drafts**; matching handles are skipped. A downloadable report lists results and issues. Files may be staged at Shopify as part of the import. Products already created before an error are not automatically rolled back.

The source and destination are deliberately **separate app configurations and separate hosted deployments**, each restricted to one original `*.myshopify.com` store domain and one role. They must use **different persistent volumes**. The same codebase can be deployed twice; it is not a general multi-tenant service.

## Important limits

- Packages and reports are **not encrypted** by this app. They may contain private catalog and metafield values. Use TLS, private encrypted storage/backups, appropriate access controls and a secure transfer channel. Delete files and backups according to your own retention policy.
- Product metafield **values** are included where the source app can read them. Metafield definitions are not copied. Other apps' protected namespaces may be inaccessible and therefore absent. Some store-specific references cannot be mapped and require manual review.
- Inventory quantities/locations, publications, collections, translations, variant metafields and non-product metafields are not migrated. Non-image video/3D files are not transferred. Multiple image links per variant and linked option metafields may require review.
- Gift cards, bundles, combined listings, required selling plans, and products with more than 250 variants, media entries or option values need a separate workflow; applicable products are reported/skipped. Prices are imported only when the store currencies match. Verify tax, shipping, inventory, image processing, themes and sales channels before publishing drafts.
- Default maximum uploaded ZIP: 5 GiB. The uncompressed JSONL catalog is limited to 512 MiB; each image to 20 MiB; the archive to 200,000 entries. Hosting proxies and available disk may impose lower limits. These are safety caps, not a performance guarantee.
- Jobs run in one application process. Use exactly **one replica** per deployment. Jobs are not durable/resumable; restarting interrupts them. Online tokens may expire after 24 hours or on logout, causing partial operations. If a write outcome is uncertain, the job stops; inspect the destination and report before retrying.
- There is no built-in retention/deletion UI, webhook-based uninstall cleanup, staff-by-staff separation of stored files, backup management, billing, Shopify review/privacy compliance, or audit-grade security monitoring. Staff with the required app/user access to one deployment can access its stored operations.
- CSV/Excel/Google Sheets imports, bulk edits, scheduling, sync, FTP/SFTP, encryption, and the rest of the requested Matrixify-style functionality are **not implemented in this embedded migration build**. The separate local-first Porter prototype is not a substitute for production Shopify integration.

See [SECURITY.md](SECURITY.md) and [DEPLOYMENT.md](DEPLOYMENT.md) before connecting any store.

## Windows: put the source in GitHub

The ZIP delivered with this project contains no Shopify credentials or live store data. Extract it, then create an **empty private GitHub repository** using the GitHub website. With Git for Windows installed, open PowerShell in the extracted `importgiant-shopify` folder:

```powershell
git init
git add .
git status --short
git commit -m "Add ImportGiant migration prototype"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/YOUR_REPOSITORY.git
git push -u origin main
```

Replace the URL with your actual repository. Review `git status --short` before the commit. Do **not** add real `.env` files, client secrets, Shopify tokens, store ZIPs, databases, or hosting credentials. Authenticate GitHub using its normal sign-in/credential manager, not in chat. `.gitignore` excludes common sensitive paths, but it cannot catch every possible secret.

The included GitHub Actions workflow runs only offline tests and syntax checks. It does not deploy, release an app version, or write to Shopify.

## Run the offline tests

With Python 3.12+ and Node.js installed, in PowerShell:

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -W error -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m py_compile app.py migration.py
node --check public/app.js
```

The tests use synthetic stores, signed test tokens and a local mock HTTP server; they cannot prove real Shopify compatibility.

## How deployment works

GitHub holds the source; it does **not** run the app. A host with HTTPS and persistent private storage must run `app.py` (a Dockerfile is supplied). Shopify configuration must point to the hosted app. `shopify app deploy` releases app configuration/extensions to Shopify; it does not host the Python server. See [DEPLOYMENT.md](DEPLOYMENT.md) for the two-deployment setup and testing checklist.

Official references: [Shopify deployment](https://shopify.dev/docs/api/shopify-cli/app/app-deploy), [App configuration](https://shopify.dev/docs/apps/build/cli-for-apps/app-configuration), and [embedded token exchange](https://shopify.dev/docs/apps/build/authentication-authorization/implement-token-exchange).
