"""Product-only, file-based Shopify migration for Porter.

This is an experimental single-operator integration: the GraphQL client is real,
while the development test suite replaces it with a fake. No secrets are stored
in packages, the database, or the browser's persistent storage.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import shutil
import sqlite3
import tempfile
import time
import uuid
import zipfile
from collections import Counter, defaultdict
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from aiohttp import ClientError, ClientSession, ClientTimeout, FormData, web

MIGRATION_APP_KEY = web.AppKey("migration", object)
# A validated Shopify user and online access token are set only for an
# authenticated request. asyncio.create_task inherits its parent's context,
# so a transfer job gets that user's token without global store credentials.
CURRENT_SHOP = ContextVar("importgiant_current_shop", default=None)
FORMAT = "porter.product-bundle"
VERSION = 1
MAX_BUNDLE = int(os.environ.get("PORTER_TRANSFER_MAX_BYTES", str(5 * 1024**3)))
MAX_IMAGE = 20 * 1024**2
MAX_CATALOG = 512 * 1024**2
MAX_MANIFEST = 16 * 1024**2
MAX_ENTRIES = 200_000
MAX_PRODUCTS = 1_000_000
MAX_LINE = 16 * 1024**2
MAX_ARRAY = 250
SESSION_SECONDS = 30 * 60
ASSET_RE = re.compile(r"^images/[0-9a-f]{64}\.(?:jpg|jpeg|png|gif|webp|svg|tif|tiff|bmp|heic|avif)$")
STORE_RE = re.compile(r"^[a-z0-9][a-z0-9-]*\.myshopify\.com$")
# Product-level values only. Product and variant references can be resolved in a
# second pass; references to other object types remain in the archive/report.
REFERENCE_TYPES = {"product_reference", "variant_reference", "list.product_reference", "list.variant_reference"}

BULK_PRODUCT_QUERY = r'''{
  products {
    edges { node {
      __typename id handle title descriptionHtml vendor productType status tags
      templateSuffix isGiftCard requiresSellingPlan hasVariantsThatRequiresComponents
      combinedListingRole productComponentsCount { count }
      seo { title description } category { id }
      options { name position values linkedMetafield { namespace key } }
      variants { edges { node {
        __typename id title sku barcode price compareAtPrice taxable taxCode
        inventoryPolicy inventoryQuantity position showUnitPrice selectedOptions { name value }
        unitPriceMeasurement { quantityUnit quantityValue referenceUnit referenceValue }
        inventoryItem { sku tracked requiresShipping countryCodeOfOrigin
          provinceCodeOfOrigin harmonizedSystemCode unitCost { amount currencyCode }
          measurement { weight { value unit } } }
        media { edges { node { __typename id } } }
      } } }
      media { edges { node {
        __typename id alt mediaContentType status
        ... on MediaImage { mimeType image { url altText width height } }
      } } }
      metafields { edges { node { __typename id namespace key type value } } }
    } }
  }
}'''
BULK_HANDLES_QUERY = '{ products { edges { node { id handle } } } }'

MUTATION_BULK = '''mutation RunBulk($query:String!){
  bulkOperationRunQuery(query:$query, groupObjects:false) { bulkOperation { id status } userErrors { field message } }
}'''
QUERY_BULK = '''query BulkStatus($id:ID!){
  node(id:$id) { ... on BulkOperation { id status errorCode objectCount fileSize url partialDataUrl } }
}'''
QUERY_SHOP = '''query PorterShop { shop { id name myshopifyDomain currencyCode }
  currentAppInstallation { accessScopes { handle } } }'''
QUERY_BY_HANDLE = '''query PorterHandle($handle:String!){
  productByIdentifier(identifier:{handle:$handle}) { id handle }
}'''
MUTATION_STAGE = '''mutation PorterStage($input:[StagedUploadInput!]!){
  stagedUploadsCreate(input:$input) { stagedTargets { url resourceUrl parameters { name value } }
    userErrors { field message } }
}'''
MUTATION_SET_PRODUCT = '''mutation PorterProduct($input:ProductSetInput!){
  productSet(input:$input, synchronous:true) {
    product { id handle variants(first:250) { nodes { id selectedOptions { name value } } }
      media(first:250) { nodes { id mediaContentType status } } }
    userErrors { field message code }
  }
}'''
MUTATION_SET_METAFIELDS = '''mutation PorterMetafields($metafields:[MetafieldsSetInput!]!){
  metafieldsSet(metafields:$metafields) { metafields { namespace key } userErrors { field message code } }
}'''


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def jdump(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def shopify_read_only():
    """A local source-export guard, independent of the token's Shopify scopes."""
    session = CURRENT_SHOP.get() or {}
    return (session.get("role") == "source"
            or os.environ.get("IMPORTGIANT_ROLE", "").strip().lower() == "source"
            or os.environ.get("PORTER_SHOPIFY_READ_ONLY", "").strip().lower() in {"1", "true", "yes", "on"})


def get_config(role):
    if role not in {"source", "destination"}:
        raise ValueError("Choose the source or destination store.")
    session = CURRENT_SHOP.get()
    selected = session if session and session.get("role") == role else {}
    domain = str(selected.get("shop") or "").strip().lower()
    token = str(selected.get("token") or "").strip()
    version = os.environ.get("SHOPIFY_API_VERSION", "2026-07").strip()
    if domain and not STORE_RE.fullmatch(domain):
        raise ValueError(f"The {role} domain must be a myshopify.com domain without https://.")
    if not re.fullmatch(r"20\d\d-(01|04|07|10)", version):
        raise ValueError("SHOPIFY_API_VERSION must be a quarterly Shopify API version.")
    return {"domain": domain, "token": token, "version": version, "configured": bool(domain and token)}


def allowed_remote(url, purpose):
    """Restrict all asset/bulk/staged URLs to HTTPS, with no redirects to private hosts."""
    try:
        u = urlparse(url)
        host = (u.hostname or "").lower()
        if u.scheme != "https" or u.username or u.password or u.port not in (None, 443):
            return False
    except ValueError:
        return False
    suffixes = {
        "image": ("cdn.shopify.com", "shopifycdn.com", "shopifycdn.net", "shopify.com"),
        "bulk": ("googleapis.com", "amazonaws.com", "shopify.com", "shopifycdn.com", "shopifycdn.net"),
        "upload": ("googleapis.com", "amazonaws.com", "shopify.com", "shopifycdn.com"),
    }[purpose]
    return any(host == s or host.endswith("." + s) for s in suffixes)


def safe_extension(content_type, url=""):
    types = {"image/jpeg":"jpg", "image/png":"png", "image/gif":"gif", "image/webp":"webp", "image/svg+xml":"svg", "image/tiff":"tiff", "image/bmp":"bmp", "image/heic":"heic", "image/avif":"avif"}
    clean = (content_type or "").split(";", 1)[0].strip().lower()
    if clean in types:
        return types[clean], clean
    ext = Path(urlparse(url).path).suffix.lower().lstrip(".")
    if ext in {"jpg", "jpeg", "png", "gif", "webp", "svg", "tif", "tiff", "bmp", "heic", "avif"}:
        return ext, mimetypes.guess_type("x." + ext)[0] or "application/octet-stream"
    raise ValueError("Shopify returned an image with an unsupported or missing file type.")


def variant_key(options):
    return tuple(sorted((str(x.get("name", "")), str(x.get("value", ""))) for x in (options or [])))


def metafield_value(field, id_map):
    """Translate only portable types; never silently copy old store GIDs."""
    typ = str(field.get("type") or "")
    raw = field.get("value")
    if not typ or not isinstance(raw, str):
        raise ValueError("The metafield has no supported type or string value.")
    if typ in REFERENCE_TYPES:
        is_list = typ.startswith("list.")
        try:
            old_ids = json.loads(raw) if is_list else [raw]
        except (ValueError, TypeError):
            raise ValueError("The reference list is invalid JSON.")
        if not isinstance(old_ids, list) or not all(isinstance(x, str) for x in old_ids):
            raise ValueError("The reference contains invalid object IDs.")
        mapped = []
        for old in old_ids:
            if old not in id_map:
                raise ValueError(f"Reference could not be mapped to the destination: {old}")
            mapped.append(id_map[old])
        return jdump(mapped) if is_list else mapped[0]
    # All other references are store-specific. Rich text, JSON, lists and text
    # may also contain GIDs; flag them instead of producing broken values.
    if "reference" in typ or "gid://shopify/" in raw:
        raise ValueError("This store-specific reference type requires manual migration.")
    return raw


def package_preflight(path):
    """Validate a Porter bundle without extracting paths to the filesystem.

    Streams JSONL and asset hashes; validates counts against content. It refuses
    encrypted/duplicate/traversal entries, zip bombs and damaged files before
    a destination write can start.
    """
    path = Path(path)
    if not path.is_file() or path.stat().st_size > MAX_BUNDLE:
        raise ValueError("The package is missing or exceeds the configured transfer size.")
    try:
        with zipfile.ZipFile(path, "r") as z:
            infos = z.infolist()
            names = [i.filename for i in infos]
            if len(infos) > MAX_ENTRIES or len(names) != len(set(names)):
                raise ValueError("The package has too many or duplicate ZIP entries.")
            if any(i.is_dir() or i.flag_bits & 1 or i.filename.startswith(("/", "\\")) or "\\" in i.filename or ".." in i.filename.split("/") for i in infos):
                raise ValueError("The package contains unsafe or encrypted ZIP entries.")
            if not {"manifest.json", "products.jsonl"}.issubset(names):
                raise ValueError("This is not a Porter product package: manifest.json or products.jsonl is missing.")
            for i in infos:
                if i.filename not in ("manifest.json", "products.jsonl") and not ASSET_RE.fullmatch(i.filename):
                    raise ValueError("The package contains an unexpected file path.")
                if i.filename == "manifest.json" and i.file_size > MAX_MANIFEST:
                    raise ValueError("The package manifest is too large.")
                if i.filename == "products.jsonl" and i.file_size > MAX_CATALOG:
                    raise ValueError("The package catalog is too large.")
                if ASSET_RE.fullmatch(i.filename) and i.file_size > MAX_IMAGE:
                    raise ValueError("One of the package images is larger than 20 MB.")
            if sum(i.file_size for i in infos) > MAX_BUNDLE * 2:
                raise ValueError("The uncompressed package exceeds the safety limit.")
            try:
                manifest = json.loads(z.read("manifest.json"))
            except (ValueError, UnicodeDecodeError) as e:
                raise ValueError("The package manifest is invalid.") from e
            if not isinstance(manifest, dict) or manifest.get("format") != FORMAT or manifest.get("version") != VERSION:
                raise ValueError("This package format is not supported. Use a Porter product bundle.")
            source = manifest.get("source")
            if not isinstance(source, dict) or source.get("kind") not in ("shopify", "demo"):
                raise ValueError("The package source identity is missing or invalid.")
            if source["kind"] == "shopify":
                if not isinstance(source.get("domain"), str) or not STORE_RE.fullmatch(source["domain"].lower()):
                    raise ValueError("The package source Shopify domain is missing or invalid.")
                if not isinstance(source.get("shop_id"), str) or not source["shop_id"].startswith("gid://shopify/Shop/"):
                    raise ValueError("The package source Shopify ID is missing or invalid.")
                if not re.fullmatch(r"[A-Z]{3}", str(source.get("currency") or "")):
                    raise ValueError("The package source currency is missing or invalid.")
            assets = manifest.get("assets")
            if not isinstance(assets, list) or len(assets) > MAX_ENTRIES:
                raise ValueError("The package image list is invalid.")
            by_path = {}
            for a in assets:
                if not isinstance(a, dict) or not ASSET_RE.fullmatch(str(a.get("path", ""))):
                    raise ValueError("The package contains an invalid image entry.")
                p = a["path"]
                if p in by_path or p not in names or not re.fullmatch(r"[0-9a-f]{64}", str(a.get("sha256", ""))):
                    raise ValueError("The package image manifest is incomplete or duplicated.")
                if z.getinfo(p).file_size != a.get("bytes"):
                    raise ValueError(f"The declared size of {p} does not match the ZIP.")
                content_type = str(a.get("content_type") or "")
                if not content_type.startswith("image/"):
                    raise ValueError("The package includes a file that is not declared as an image.")
                safe_extension(content_type, p)
                by_path[p] = a
            if set(names) != {"manifest.json", "products.jsonl"} | set(by_path):
                raise ValueError("The package has image files not recorded in its manifest.")
            for p, info in by_path.items():
                digest = hashlib.sha256()
                with z.open(p) as f:
                    for chunk in iter(lambda:f.read(1024*1024), b""):
                        digest.update(chunk)
                if not hmac.compare_digest(digest.hexdigest(), info["sha256"]):
                    raise ValueError(f"The checksum for {p} does not match; the package may be damaged.")
            counts = Counter()
            handles, product_ids, variant_ids = set(), set(), set()
            reference_types = Counter()
            unsupported_products, risky_metafields = [], []
            risky_metafield_count = 0
            with z.open("products.jsonl") as catalog:
                for line in catalog:
                    if len(line) > MAX_LINE:
                        raise ValueError("A product entry exceeds the per-product safety limit.")
                    try:
                        p = json.loads(line)
                    except (ValueError, UnicodeDecodeError) as e:
                        raise ValueError("The package contains an invalid product record.") from e
                    if not isinstance(p, dict) or not isinstance(p.get("handle"), str) or not p.get("handle") or not isinstance(p.get("id"), str) or not p.get("id"):
                        raise ValueError("Every product must have a source ID and a handle.")
                    if p["handle"].casefold() in handles or p["id"] in product_ids:
                        raise ValueError("The package contains duplicate product handles or IDs.")
                    handles.add(p["handle"].casefold()); product_ids.add(p["id"])
                    variants, media, fields, options = (p.get(k, []) for k in ("variants", "media", "metafields", "options"))
                    if not all(isinstance(x, list) for x in (variants, media, fields, options)):
                        raise ValueError("The package contains invalid product lists.")
                    if not all(isinstance(v, dict) for v in variants + media + fields + options):
                        raise ValueError("A product has an invalid variant, media, metafield or option record.")
                    components = p.get("productComponentsCount")
                    if components is not None and (not isinstance(components, dict) or not isinstance(components.get("count", 0), int)):
                        raise ValueError(f"The bundle information for {p['handle']} is invalid.")
                    for option in options:
                        if not isinstance(option.get("name"), str) or not isinstance(option.get("values", []), list) or not all(isinstance(v, str) for v in option.get("values", [])):
                            raise ValueError(f"The option data for {p['handle']} is invalid.")
                    media_ids = set()
                    for m in media:
                        if not isinstance(m.get("id"), str) or not m["id"] or m["id"] in media_ids:
                            raise ValueError(f"The product media IDs for {p['handle']} are invalid or duplicated.")
                        media_ids.add(m["id"])
                    combinations = set()
                    for v in variants:
                        if not isinstance(v.get("id"), str) or not v["id"] or v["id"] in variant_ids:
                            raise ValueError("The package contains missing or duplicate variant IDs.")
                        variant_ids.add(v["id"])
                        selected, links = v.get("selectedOptions", []), v.get("media_ids", [])
                        if not isinstance(selected, list) or not all(isinstance(x, dict) and isinstance(x.get("name"), str) and isinstance(x.get("value"), str) for x in selected):
                            raise ValueError(f"The variant options for {p['handle']} are invalid.")
                        combination = variant_key(selected)
                        if combination in combinations or len({x["name"] for x in selected}) != len(selected):
                            raise ValueError(f"The product {p['handle']} has duplicate or ambiguous variant options.")
                        combinations.add(combination)
                        if not isinstance(links, list) or not all(isinstance(x, str) and x in media_ids for x in links):
                            raise ValueError(f"A variant image link for {p['handle']} does not match its product media.")
                    for field in fields:
                        if not all(isinstance(field.get(k), str) and field.get(k) for k in ("namespace", "key", "type")) or not isinstance(field.get("value"), str):
                            raise ValueError(f"A product metafield for {p['handle']} is malformed.")
                    counts["products"] += 1; counts["variants"] += len(variants); counts["product_metafields"] += len(fields)
                    if counts["products"] > MAX_PRODUCTS:
                        raise ValueError("This package exceeds the product safety limit.")
                    for m in media:
                        if m.get("mediaContentType") == "IMAGE":
                            if not m.get("asset") or m["asset"] not in by_path:
                                raise ValueError(f"The package is missing a product image for {p['handle']}.")
                            counts["images"] += 1
                        else:
                            counts["other_media"] += 1
                    for f in fields:
                        typ = f["type"]
                        if "reference" in typ:
                            reference_types[typ] += 1
                        is_external_ref = ("reference" in typ or "gid://shopify/" in f["value"]) and typ not in REFERENCE_TYPES
                        is_owned_namespace = f["namespace"].startswith(("app--", "shopify--"))
                        if is_external_ref or is_owned_namespace:
                            risky_metafield_count += 1
                            if len(risky_metafields) < 100:
                                risky_metafields.append({"handle":p["handle"], "namespace":f["namespace"], "key":f["key"], "type":typ,
                                                         "reason":"Store-specific reference" if is_external_ref else "Possibly protected app-owned namespace"})
                    reasons = []
                    if len(variants) > MAX_ARRAY or len(media) > MAX_ARRAY or any(len(o.get("values") or []) > MAX_ARRAY for o in options):
                        reasons.append("Over 250 variants, media items or values in one option")
                    if not variants: reasons.append("No variants")
                    if p.get("isGiftCard"): reasons.append("Gift card")
                    if p.get("requiresSellingPlan"): reasons.append("Requires a selling plan")
                    if p.get("hasVariantsThatRequiresComponents") or (p.get("productComponentsCount") or {}).get("count", 0): reasons.append("Bundle")
                    if p.get("combinedListingRole"): reasons.append("Combined listing")
                    if reasons:
                        unsupported_products.append({"handle":p["handle"], "reason":"; ".join(reasons)})
            expected = manifest.get("counts")
            if not isinstance(expected, dict):
                raise ValueError("The package counts are missing or invalid.")
            for k in ("products", "variants", "images", "product_metafields", "other_media"):
                if not isinstance(expected.get(k), int) or expected[k] < 0 or expected[k] != counts[k]:
                    raise ValueError(f"The {k} count does not match the manifest.")
            if not isinstance(manifest.get("warnings", []), list) or not all(isinstance(w, str) for w in manifest.get("warnings", [])):
                raise ValueError("The package warning list is invalid.")
            return {"format":FORMAT, "source":source, "created_at":manifest.get("created_at"),
                    "counts":dict(counts), "asset_count":len(by_path), "reference_types":dict(reference_types),
                    "unsupported_products":unsupported_products[:100], "unsupported_count":len(unsupported_products),
                    "risky_metafields":risky_metafields, "risky_metafield_count":risky_metafield_count,
                    "warnings":manifest.get("warnings") or [], "assets":assets}
    except (zipfile.BadZipFile, EOFError, RuntimeError, OSError, KeyError) as e:
        raise ValueError("Could not read the package ZIP; it may be damaged or encrypted.") from e


def build_product_input(product, staged, force_draft=True):
    """Construct create-only productSet input, never passing an existing ID."""
    variants, media, options = product.get("variants", []), product.get("media", []), product.get("options", [])
    if not variants:
        raise ValueError("This product has no variants.")
    if len(variants) > MAX_ARRAY or len(media) > MAX_ARRAY or any(len(o.get("values") or []) > MAX_ARRAY for o in options):
        raise ValueError("More than 250 variants, images, or option values per product are not supported in this version.")
    if product.get("isGiftCard"):
        raise ValueError("Gift card products require Shopify's special gift card workflow.")
    if product.get("requiresSellingPlan"):
        raise ValueError("A required selling plan needs a separate destination setup.")
    if product.get("hasVariantsThatRequiresComponents") or (product.get("productComponentsCount") or {}).get("count", 0):
        raise ValueError("Bundled products and variants require a separate bundle migration.")
    if product.get("combinedListingRole"):
        raise ValueError("Combined listings require a separate listing migration.")
    doc = {"title":product.get("title") or product["handle"], "handle":product["handle"], "status":"DRAFT" if force_draft else product.get("status", "DRAFT")}
    for key in ("descriptionHtml", "vendor", "productType", "tags", "templateSuffix", "seo"):
        if product.get(key) is not None:
            doc[key] = product[key]
    category = (product.get("category") or {}).get("id")
    if category:
        doc["category"] = category
    doc["productOptions"] = [{"name":o["name"], "position":o.get("position", i+1),
                              "values":[{"name":str(v)} for v in (o.get("values") or [])]}
                             for i, o in enumerate(options) if o.get("name")]
    files, file_by_media = [], {}
    for m in media:
        if m.get("mediaContentType") != "IMAGE":
            continue
        target = staged.get(m.get("asset"))
        if not target:
            raise ValueError("A product image was not staged successfully.")
        file_input = {"originalSource":target["resourceUrl"], "contentType":"IMAGE", "filename":target["filename"]}
        if m.get("alt") is not None:
            file_input["alt"] = m["alt"]
        files.append(file_input); file_by_media[m["id"]] = file_input
    if files:
        doc["files"] = files
    result_variants = []
    for v in variants:
        row = {}
        for k in ("sku", "barcode", "price", "compareAtPrice", "taxable", "taxCode", "inventoryPolicy", "position", "showUnitPrice"):
            if v.get(k) is not None:
                row[k] = v[k]
        measurement = v.get("unitPriceMeasurement")
        if measurement:
            row["unitPriceMeasurement"] = {k:measurement[k] for k in ("quantityUnit", "quantityValue", "referenceUnit", "referenceValue") if measurement.get(k) is not None}
        option_values = [{"optionName":x["name"], "name":x["value"]} for x in (v.get("selectedOptions") or []) if x.get("name") and x.get("value") is not None]
        if option_values:
            row["optionValues"] = option_values
        item = v.get("inventoryItem") or {}
        inventory = {k:item[k] for k in ("sku", "tracked", "requiresShipping", "countryCodeOfOrigin", "provinceCodeOfOrigin", "harmonizedSystemCode") if item.get(k) is not None}
        cost = item.get("unitCost") or {}
        if cost.get("amount") is not None:
            inventory["cost"] = cost["amount"]
        weight = ((item.get("measurement") or {}).get("weight") or {})
        if weight.get("value") is not None and weight.get("unit"):
            inventory["measurement"] = {"weight":{"value":weight["value"], "unit":weight["unit"]}}
        if inventory:
            row["inventoryItem"] = inventory
        for mid in (v.get("media_ids") or []):
            if mid in file_by_media:
                row["file"] = file_by_media[mid]
                break
        result_variants.append(row)
    doc["variants"] = result_variants
    return doc


class ShopifyClient:
    def __init__(self, role):
        if shopify_read_only() and role != "source":
            raise ValueError("Shopify destination access is disabled in read-only test mode.")
        self.role = role
        self.config = get_config(role)
        if not self.config["configured"]:
            raise ValueError(f"Configure the {role} Shopify domain and Admin API token on the server first.")
        self.endpoint = f"https://{self.config['domain']}/admin/api/{self.config['version']}/graphql.json"
        self.session = None

    async def __aenter__(self):
        self.session = ClientSession(timeout=ClientTimeout(total=120, connect=20, sock_read=90))
        return self

    async def __aexit__(self, *args):
        await self.session.close()

    async def graphql(self, query, variables=None, *, mutation=False):
        # Shopify starts a bulk *read* using a mutation. In read-only test mode
        # allow only our fixed product-query job and its status/shop queries;
        # reject every other GraphQL document regardless of the caller's flag.
        if shopify_read_only():
            is_read = query in (QUERY_SHOP, QUERY_BULK) and not mutation
            is_bulk_read = query == MUTATION_BULK and mutation and (variables or {}).get("query") == BULK_PRODUCT_QUERY
            if self.role != "source" or not (is_read or is_bulk_read):
                raise ValueError("This Shopify action is blocked in read-only test mode.")
        # Mutations are never automatically retried after ambiguous network failures.
        for attempt in range(4):
            try:
                async with self.session.post(self.endpoint, json={"query":query, "variables":variables or {}},
                                             headers={"X-Shopify-Access-Token":self.config["token"]}, allow_redirects=False) as r:
                    if r.status == 429:
                        if attempt < 3:
                            await asyncio.sleep(min(10, float(r.headers.get("Retry-After", "2"))))
                            continue
                    if r.status in (401, 403):
                        raise ValueError(f"Shopify rejected the {self.role} token or its permissions.")
                    if r.status != 200:
                        if mutation and r.status >= 500:
                            if query == MUTATION_BULK:
                                raise RuntimeError(f"Shopify returned HTTP {r.status} while starting a bulk read; the job outcome is unknown. The query does not modify product data.")
                            raise RuntimeError(f"Shopify returned HTTP {r.status} during a write; its outcome is unknown. Check the destination before retrying.")
                        raise ValueError(f"Shopify {self.role} returned HTTP {r.status}.")
                    body = await r.json(content_type=None)
            except (ClientError, asyncio.TimeoutError) as e:
                if mutation:
                    if query == MUTATION_BULK:
                        raise RuntimeError("Shopify did not confirm the bulk read; the job outcome is unknown. The query does not modify product data.") from e
                    raise RuntimeError("The Shopify write did not return a response; its outcome is unknown. Stop and check the destination before retrying.") from e
                raise ValueError(f"Could not reach the {self.role} Shopify store.") from e
            errors = body.get("errors") or []
            if errors:
                throttled = any((e.get("extensions") or {}).get("code") == "THROTTLED" for e in errors)
                if throttled and attempt < 3:
                    await asyncio.sleep(2 * (attempt + 1)); continue
                message = "; ".join(str(x.get("message", "Unknown error")) for x in errors[:3])
                if mutation and not throttled:
                    if query == MUTATION_BULK:
                        raise ValueError("Shopify could not start the bulk read: " + message)
                    raise RuntimeError("Shopify returned a GraphQL error during a write; its outcome may be partial. Check the destination before retrying: " + message)
                raise ValueError("Shopify GraphQL: " + message)
            throttle = ((body.get("extensions") or {}).get("cost") or {}).get("throttleStatus") or {}
            available = throttle.get("currentlyAvailable")
            if available is not None and available < 100:
                await asyncio.sleep(1)
            return body.get("data") or {}
        raise ValueError("Shopify is rate limiting requests; please try again later.")

    async def shop(self):
        result = await self.graphql(QUERY_SHOP)
        shop = result.get("shop") or {}
        if not shop.get("id") or not shop.get("myshopifyDomain"):
            raise ValueError(f"Could not verify the {self.role} shop identity.")
        return {**shop, "scopes":[x.get("handle") for x in ((result.get("currentAppInstallation") or {}).get("accessScopes") or [])]}

    async def bulk(self, query, progress):
        result = await self.graphql(MUTATION_BULK, {"query":query}, mutation=True)
        block = result.get("bulkOperationRunQuery") or {}
        errors = block.get("userErrors") or []
        if errors:
            raise ValueError("Shopify could not start the bulk query: " + "; ".join(x.get("message", "") for x in errors))
        bulk_id = (block.get("bulkOperation") or {}).get("id")
        if not bulk_id:
            raise ValueError("Shopify did not return a bulk operation ID.")
        for _ in range(7200):
            result = await self.graphql(QUERY_BULK, {"id":bulk_id})
            op = result.get("node") or {}
            status = op.get("status")
            await progress(f"Shopify bulk export: {(status or 'waiting').lower()}", int(op.get("objectCount") or 0), 0)
            if status == "COMPLETED":
                if not op.get("url"):
                    return
                if not allowed_remote(op["url"], "bulk"):
                    raise ValueError("Shopify returned an unexpected bulk download host.")
                async with self.session.get(op["url"], allow_redirects=False, timeout=ClientTimeout(total=None, sock_read=120)) as r:
                    if r.status != 200:
                        raise ValueError(f"Shopify bulk download failed (HTTP {r.status}).")
                    # Read bounded chunks instead of StreamReader.readline(), whose
                    # internal line limit can reject long product descriptions.
                    pending = bytearray()
                    async for chunk in r.content.iter_chunked(256*1024):
                        pending.extend(chunk)
                        while b"\n" in pending:
                            line, _, rest = pending.partition(b"\n")
                            pending = bytearray(rest)
                            if len(line) > MAX_LINE:
                                raise ValueError("A bulk query record exceeds the safety limit.")
                            if line.strip():
                                try:
                                    yield json.loads(line)
                                except ValueError as e:
                                    raise ValueError("Shopify returned invalid bulk JSONL.") from e
                        if len(pending) > MAX_LINE:
                            raise ValueError("A bulk query record exceeds the safety limit.")
                    if pending.strip():
                        try:
                            yield json.loads(pending)
                        except ValueError as e:
                            raise ValueError("Shopify returned invalid bulk JSONL.") from e
                return
            if status in ("FAILED", "CANCELED", "CANCELLED", "EXPIRED"):
                raise ValueError(f"Shopify bulk query {str(status).lower()}: {op.get('errorCode') or 'no error code'}.")
            await asyncio.sleep(2)
        raise ValueError("Shopify bulk query did not complete before the timeout.")

    async def download_image(self, url, destination, declared_type=None):
        if not allowed_remote(url, "image"):
            raise ValueError("An image URL was not on an approved Shopify CDN host.")
        digest = hashlib.sha256(); size = 0
        try:
            async with self.session.get(url, allow_redirects=False, timeout=ClientTimeout(total=180, sock_read=60)) as r:
                if r.status != 200:
                    raise ValueError(f"A source product image could not be downloaded (HTTP {r.status}).")
                ctype = r.headers.get("Content-Type") or declared_type
                ext, mime = safe_extension(ctype, url)
                with open(destination, "wb") as f:
                    async for chunk in r.content.iter_chunked(256*1024):
                        size += len(chunk)
                        if size > MAX_IMAGE:
                            raise ValueError("A source product image exceeds the 20 MB safety limit.")
                        digest.update(chunk); f.write(chunk)
        except (ClientError, asyncio.TimeoutError) as e:
            raise ValueError("Could not download a source product image.") from e
        if not size:
            raise ValueError("A source product image is empty.")
        return {"sha256":digest.hexdigest(), "bytes":size, "extension":ext, "content_type":mime}

    async def find_product(self, handle):
        data = await self.graphql(QUERY_BY_HANDLE, {"handle":handle})
        return data.get("productByIdentifier")

    async def stage_images(self, z, image_entries, assets):
        """Upload actual bundle bytes to Shopify staged targets, in order."""
        unique = list(dict.fromkeys(m["asset"] for m in image_entries if m.get("mediaContentType") == "IMAGE"))
        if not unique:
            return {}
        if len(unique) > MAX_ARRAY:
            raise ValueError("More than 250 images on a product are not supported.")
        inputs = []
        for path in unique:
            info = assets[path]
            filename = "porter-" + info["sha256"][:24] + Path(path).suffix
            inputs.append({"resource":"IMAGE", "filename":filename, "mimeType":info["content_type"], "httpMethod":"POST"})
        data = await self.graphql(MUTATION_STAGE, {"input":inputs}, mutation=True)
        block = data.get("stagedUploadsCreate") or {}
        if block.get("userErrors"):
            raise ValueError("Could not stage images: " + "; ".join(x.get("message", "") for x in block["userErrors"]))
        targets = block.get("stagedTargets") or []
        if len(targets) != len(unique):
            raise ValueError("Shopify returned an unexpected number of staged image targets.")
        result = {}
        for path, target, inp in zip(unique, targets, inputs):
            if not allowed_remote(target.get("url", ""), "upload") or not target.get("resourceUrl"):
                raise ValueError("Shopify returned an unexpected staged upload URL.")
            form = FormData()
            for param in target.get("parameters") or []:
                form.add_field(param["name"], param["value"])
            # A single image is limited to 20 MB. Bytes are kept only while this
            # upload runs; the full ZIP is never loaded in memory.
            content = z.read(path)
            form.add_field("file", content, filename=inp["filename"], content_type=inp["mimeType"])
            try:
                async with self.session.post(target["url"], data=form, allow_redirects=False) as r:
                    if r.status not in (200, 201, 204):
                        raise ValueError(f"A staged image upload failed (HTTP {r.status}).")
            except (ClientError, asyncio.TimeoutError) as e:
                raise ValueError("A staged image upload failed; the product was not created.") from e
            result[path] = {"resourceUrl":target["resourceUrl"], "filename":inp["filename"]}
        return result

    async def create_product(self, input_doc):
        result = await self.graphql(MUTATION_SET_PRODUCT, {"input":input_doc}, mutation=True)
        block = result.get("productSet") or {}
        if block.get("userErrors"):
            message = "; ".join(x.get("message", "") for x in block["userErrors"])
            partial = block.get("product") or {}
            if partial.get("id"):
                raise RuntimeError(f"Shopify returned product {partial['id']} with errors; review the destination before continuing: {message}")
            raise ValueError("Shopify product validation: " + message)
        product = block.get("product") or {}
        if not product.get("id"):
            raise RuntimeError("Shopify did not return a product ID; check the destination before retrying.")
        return product

    async def set_metafields(self, metafields):
        if not metafields:
            return []
        result = await self.graphql(MUTATION_SET_METAFIELDS, {"metafields":metafields}, mutation=True)
        block = result.get("metafieldsSet") or {}
        return block.get("userErrors") or []


class DiskIdMap:
    def __init__(self, connection):
        self.conn = connection

    def put(self, old, new, created=False):
        if old and new:
            self.conn.execute("INSERT OR REPLACE INTO id_map(old_id,new_id,created) VALUES(?,?,?)", (old, new, int(created)))

    def __contains__(self, old):
        return self.conn.execute("SELECT 1 FROM id_map WHERE old_id=?", (old,)).fetchone() is not None

    def __getitem__(self, old):
        row = self.conn.execute("SELECT new_id FROM id_map WHERE old_id=?", (old,)).fetchone()
        if not row:
            raise KeyError(old)
        return row[0]

    def created_product(self, old):
        row = self.conn.execute("SELECT new_id FROM id_map WHERE old_id=? AND created=1", (old,)).fetchone()
        return row[0] if row else None


def _api(handler):
    async def inner(self, request):
        try:
            return await handler(self, request)
        except web.HTTPException as e:
            return web.json_response({"error":e.reason or "Request denied."}, status=e.status)
        except (ValueError, json.JSONDecodeError) as e:
            return web.json_response({"error":str(e)}, status=400)
        except Exception as e:
            print(f"Migration API error {handler.__name__}: {type(e).__name__}: {e}", flush=True)
            return web.json_response({"error":"The migration request failed. Check the server logs."}, status=500)
    return inner


class MigrationService:
    def __init__(self, app, db_factory, all_records, base_dir, client_factory=ShopifyClient):
        self.app, self.db, self.all_records = app, db_factory, all_records
        self.root = Path(base_dir) / "data" / "migration"
        self.root.mkdir(parents=True, exist_ok=True)
        self.client_factory = client_factory
        self.sessions, self.attempts, self.tasks = {}, defaultdict(list), {}
        self.running_live_import = False
        with self.db() as c:
            c.executescript('''
                CREATE TABLE IF NOT EXISTS migration_operations (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL, mode TEXT NOT NULL,
                    status TEXT NOT NULL, message TEXT NOT NULL, current INTEGER NOT NULL DEFAULT 0,
                    total INTEGER NOT NULL DEFAULT 0, stats TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    filename TEXT, filepath TEXT, reportpath TEXT
                );
                CREATE INDEX IF NOT EXISTS migration_ops_created ON migration_operations(created_at DESC);
                CREATE TABLE IF NOT EXISTS migration_stages (
                    id TEXT PRIMARY KEY, mode TEXT NOT NULL, filename TEXT NOT NULL,
                    filepath TEXT NOT NULL, summary TEXT NOT NULL, created_at TEXT NOT NULL
                );
            ''')
            c.execute("UPDATE migration_operations SET status='interrupted', message='Server stopped during this operation. Review the destination and report before retrying.', updated_at=? WHERE status IN ('queued','running')", (timestamp(),))

    def key_ready(self):
        return len(os.environ.get("PORTER_MIGRATION_KEY", "")) >= 20

    def unlocked(self, request):
        token = request.cookies.get("porter_migration", "")
        until = self.sessions.get(token, 0)
        if until <= time.monotonic():
            self.sessions.pop(token, None)
            return False
        return True

    def require_auth(self, request):
        if not self.key_ready():
            raise web.HTTPServiceUnavailable(reason="Set PORTER_MIGRATION_KEY to at least 20 characters on the server and restart it.")
        if not self.unlocked(request):
            raise web.HTTPUnauthorized(reason="Unlock the migration workspace to use live stores and private packages.")
        origin = request.headers.get("Origin")
        if origin and request.method not in ("GET", "HEAD"):
            allowed_hosts = {request.headers.get("Host", ""), request.headers.get("X-Forwarded-Host", "")}
            if urlparse(origin).netloc not in allowed_hosts:
                raise web.HTTPForbidden(reason="This action must come from the Porter workspace.")

    def op(self, kind, mode):
        opid = uuid.uuid4().hex
        now = timestamp()
        with self.db() as c:
            c.execute("INSERT INTO migration_operations(id,kind,mode,status,message,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                      (opid, kind, mode, "queued", "Queued", now, now))
        return opid

    def update(self, opid, **values):
        allowed = {"status", "message", "current", "total", "stats", "filename", "filepath", "reportpath"}
        values = {k:(jdump(v) if k == "stats" and not isinstance(v, str) else v) for k,v in values.items() if k in allowed}
        values["updated_at"] = timestamp()
        with self.db() as c:
            c.execute("UPDATE migration_operations SET " + ",".join(f"{k}=?" for k in values) + " WHERE id=?", (*values.values(), opid))

    def operation(self, opid):
        with self.db() as c:
            row = c.execute("SELECT * FROM migration_operations WHERE id=?", (opid,)).fetchone()
        return dict(row) if row else None

    def public_op(self, row):
        result = {k:row[k] for k in ("id", "kind", "mode", "status", "message", "current", "total", "created_at", "updated_at", "filename")}
        result["stats"] = json.loads(row.get("stats") or "{}")
        result["download_url"] = f"/api/migration/download/{row['id']}" if row.get("filepath") and Path(row["filepath"]).is_file() else None
        result["report_url"] = f"/api/migration/report/{row['id']}" if row.get("reportpath") and Path(row["reportpath"]).is_file() else None
        return result

    def launch(self, opid, coro):
        async def run():
            self.update(opid, status="running")
            try:
                await coro
            except asyncio.CancelledError:
                self.update(opid, status="interrupted", message="Server stopped during this operation. Review any destination changes before retrying.")
                raise
            except Exception as e:
                print(f"Migration operation {opid}: {type(e).__name__}: {e}", flush=True)
                self.update(opid, status="failed", message=str(e)[:1500])
            finally:
                self.tasks.pop(opid, None)
        self.tasks[opid] = asyncio.create_task(run())

    @_api
    async def config(self, request):
        source, dest = get_config("source"), get_config("destination")
        return web.json_response({"source":{"configured":source["configured"], "domain":source["domain"] or None},
                                  "destination":{"configured":dest["configured"], "domain":dest["domain"] or None},
                                  "key_configured":self.key_ready(), "unlocked":self.unlocked(request),
                                  "read_only":shopify_read_only(), "max_upload":MAX_BUNDLE})

    @_api
    async def unlock(self, request):
        if not self.key_ready():
            raise web.HTTPServiceUnavailable(reason="Set PORTER_MIGRATION_KEY to at least 20 characters on the server and restart it.")
        if request.content_length is not None and request.content_length > 4096:
            raise ValueError("The unlock request is too large.")
        origin = request.headers.get("Origin")
        allowed_hosts = {request.headers.get("Host", ""), request.headers.get("X-Forwarded-Host", "")}
        if origin and urlparse(origin).netloc not in allowed_hosts:
            raise web.HTTPForbidden(reason="This action must come from the Porter workspace.")
        remote = request.remote or "unknown"
        now = time.monotonic()
        self.attempts[remote] = [x for x in self.attempts[remote] if now-x < 60]
        if len(self.attempts[remote]) >= 8:
            raise web.HTTPTooManyRequests(reason="Too many unlock attempts. Wait a minute and try again.")
        raw = bytearray()
        async for chunk in request.content.iter_chunked(4096):
            raw.extend(chunk)
            if len(raw) > 4096:
                raise ValueError("The unlock request is too large.")
        body = json.loads(bytes(raw))
        if not isinstance(body, dict):
            raise ValueError("Enter a migration access key.")
        candidate = body.get("key", "")
        if not isinstance(candidate, str) or not hmac.compare_digest(candidate.encode(), os.environ["PORTER_MIGRATION_KEY"].encode()):
            self.attempts[remote].append(now)
            raise web.HTTPUnauthorized(reason="That migration access key is incorrect.")
        self.attempts[remote] = []
        token = secrets.token_urlsafe(32)
        self.sessions[token] = now + SESSION_SECONDS
        response = web.json_response({"unlocked":True, "expires_in":SESSION_SECONDS})
        secure = request.secure or request.headers.get("X-Forwarded-Proto", "").split(",")[0].strip() == "https" or request.host.endswith(".e2b.app")
        response.set_cookie("porter_migration", token, max_age=SESSION_SECONDS, httponly=True, secure=secure, samesite="Strict", path="/")
        return response

    @_api
    async def lock(self, request):
        self.require_auth(request)
        self.sessions.pop(request.cookies.get("porter_migration", ""), None)
        response = web.json_response({"unlocked":False})
        response.del_cookie("porter_migration", path="/")
        return response

    @_api
    async def test_connection(self, request):
        self.require_auth(request)
        body = await request.json()
        role = body.get("role")
        if role not in ("source", "destination"):
            raise ValueError("Choose the source or destination store.")
        async with self.client_factory(role) as client:
            shop = await client.shop()
        return web.json_response({"shop":shop})

    @_api
    async def operations(self, request):
        live = self.unlocked(request)
        with self.db() as c:
            rows = [dict(r) for r in c.execute("SELECT * FROM migration_operations " + ("" if live else "WHERE mode='demo' ") + "ORDER BY created_at DESC, rowid DESC LIMIT 25")]
        return web.json_response({"operations":[self.public_op(x) for x in rows]})

    @_api
    async def start_export(self, request):
        body = await request.json()
        mode = body.get("mode", "shopify")
        if mode not in ("shopify", "demo"):
            raise ValueError("Choose Shopify or the demo workspace.")
        if mode == "shopify":
            self.require_auth(request)
            if not get_config("source")["configured"]:
                raise ValueError("Configure the source Shopify store first.")
            if any(self.operation(i)["kind"] == "export" and self.operation(i)["mode"] == "shopify" for i in list(self.tasks) if self.operation(i)):
                raise ValueError("A source export is already running.")
        opid = self.op("export", mode)
        self.launch(opid, self.export_worker(opid, mode))
        return web.json_response({"operation":self.public_op(self.operation(opid))}, status=202)

    @_api
    async def download(self, request):
        row = self.operation(request.match_info["opid"])
        if not row or not row.get("filepath") or not Path(row["filepath"]).is_file():
            raise web.HTTPNotFound(reason="The package is not available.")
        if row["mode"] != "demo":
            self.require_auth(request)
        r = web.FileResponse(row["filepath"])
        r.headers["Content-Disposition"] = f'attachment; filename="{row["filename"]}"'
        r.headers["Cache-Control"] = "no-store"
        return r

    @_api
    async def report(self, request):
        row = self.operation(request.match_info["opid"])
        if not row or not row.get("reportpath") or not Path(row["reportpath"]).is_file():
            raise web.HTTPNotFound(reason="The report is not available.")
        if row["mode"] != "demo":
            self.require_auth(request)
        r = web.FileResponse(row["reportpath"])
        r.headers["Content-Disposition"] = f'attachment; filename="porter-import-report-{row["id"][:8]}.jsonl"'
        r.headers["Cache-Control"] = "no-store"
        return r

    def save_stage(self, path, filename, mode):
        summary = package_preflight(path)
        source = summary.get("source") or {}
        if mode == "demo" and source.get("kind") != "demo":
            raise ValueError("Only a Porter demo package can be reviewed without unlocking.")
        stage_id = uuid.uuid4().hex
        with self.db() as c:
            c.execute("INSERT INTO migration_stages(id,mode,filename,filepath,summary,created_at) VALUES(?,?,?,?,?,?)",
                      (stage_id, mode, filename[:160], str(path), jdump(summary), timestamp()))
        public = dict(summary)
        public.pop("assets", None)
        public.update({"stage_id":stage_id, "filename":filename[:160], "mode":mode})
        return public

    @_api
    async def stage(self, request):
        # Only a server-created demo export can bypass the unlock gate.
        if request.content_type.startswith("application/json"):
            body = await request.json()
            row = self.operation(str(body.get("operation_id") or ""))
            if not row or row["kind"] != "export" or row["status"] != "completed" or not row.get("filepath"):
                raise ValueError("Select a completed export package first.")
            if row["mode"] != "demo":
                self.require_auth(request)
            mode = row["mode"]
            dest = self.root / ("stage-" + uuid.uuid4().hex + ".zip")
            await asyncio.to_thread(shutil.copyfile, row["filepath"], dest)
            filename = row["filename"]
        else:
            self.require_auth(request)
            if not request.content_type.startswith("multipart/"):
                raise ValueError("Choose a Porter ZIP package to upload.")
            try:
                reader = await request.multipart()
                part = await reader.next()
            except (ValueError, AssertionError) as e:
                raise ValueError("The upload form is malformed.") from e
            if not part or part.name != "file" or not part.filename or not part.filename.lower().endswith(".zip"):
                raise ValueError("Choose a Porter product ZIP package.")
            filename = Path(part.filename.replace("\\", "/")).name
            dest = self.root / ("stage-" + uuid.uuid4().hex + ".zip")
            mode = "shopify"
            size = 0
            try:
                with open(dest, "wb") as f:
                    while True:
                        chunk = await part.read_chunk(256*1024)
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > MAX_BUNDLE:
                            raise ValueError("This file exceeds the configured migration upload limit.")
                        f.write(chunk)
                if await reader.next() is not None:
                    raise ValueError("Upload one package at a time.")
            except BaseException:
                dest.unlink(missing_ok=True)
                raise
        try:
            summary = await asyncio.to_thread(self.save_stage, dest, filename, mode)
        except BaseException:
            dest.unlink(missing_ok=True)
            raise
        return web.json_response({"stage":summary})

    @_api
    async def start_import(self, request):
        self.require_auth(request)
        if shopify_read_only():
            raise web.HTTPForbidden(reason="Shopify imports are disabled in read-only test mode.")
        body = await request.json()
        stage_id = str(body.get("stage_id") or "")
        with self.db() as c:
            row = c.execute("SELECT * FROM migration_stages WHERE id=?", (stage_id,)).fetchone()
        if not row:
            raise ValueError("Review a product package first.")
        stage = dict(row); summary = json.loads(stage["summary"])
        if summary.get("source", {}).get("kind") == "demo":
            raise ValueError("A demo package cannot be imported into a real Shopify store.")
        dest = get_config("destination")
        if not dest["configured"]:
            raise ValueError("Configure the destination Shopify store first.")
        confirmation = body.get("confirm_domain", "")
        if not isinstance(confirmation, str) or confirmation.strip().lower() != dest["domain"] or body.get("acknowledge") is not True:
            raise ValueError("Type the destination myshopify.com domain and acknowledge the live writes.")
        if self.running_live_import:
            raise ValueError("A Shopify product import is already running.")
        # Reserve the single-writer slot before any await to prevent two
        # confirmations racing through preflight and writing concurrently.
        self.running_live_import = True
        try:
            if not Path(stage["filepath"]).is_file():
                raise ValueError("The reviewed package is no longer available. Upload it again.")
            # Revalidate after review and before any write, then verify the exact store.
            summary = await asyncio.to_thread(package_preflight, stage["filepath"])
            if summary.get("source", {}).get("kind") == "demo":
                raise ValueError("A demo package cannot be imported into a real Shopify store.")
            async with self.client_factory("destination") as client:
                shop = await client.shop()
            if shop["myshopifyDomain"].lower() != dest["domain"]:
                raise ValueError("The connected destination does not match the confirmed store domain.")
            source = summary.get("source") or {}
            if (source.get("shop_id") and source["shop_id"] == shop["id"]) or (source.get("domain") and source["domain"].lower() == dest["domain"]):
                raise ValueError("The source and destination are the same Shopify store. Import was blocked.")
            source_currency, destination_currency = source.get("currency"), shop.get("currencyCode")
            if not source_currency or not destination_currency or source_currency != destination_currency:
                raise ValueError(f"Source and destination currencies must match before importing prices (source: {source_currency or 'unknown'}; destination: {destination_currency or 'unknown'}).")
            scopes = set(shop.get("scopes") or [])
            # Shopify grants the matching read permission implicitly with a
            # write scope, even if it isn't separately listed by the API.
            scopes.update("read_" + s[6:] for s in tuple(scopes) if s.startswith("write_"))
            required = {"read_products", "write_products", "write_inventory"}
            if summary["counts"].get("images"):
                required.add("write_files")
            if not required.issubset(scopes):
                raise ValueError("The destination token is missing required scopes: " + ", ".join(sorted(required - scopes)))
            force_draft = body.get("force_draft", True) is not False
            opid = self.op("import", "shopify")
            self.launch(opid, self.import_worker(opid, stage, summary, shop, force_draft))
            return web.json_response({"operation":self.public_op(self.operation(opid)), "destination":shop}, status=202)
        except BaseException:
            self.running_live_import = False
            raise

    async def shutdown(self, app):
        for t in list(self.tasks.values()):
            t.cancel()
        if self.tasks:
            await asyncio.gather(*list(self.tasks.values()), return_exceptions=True)

    async def export_worker(self, opid, mode):
        package = self.root / f"products-{opid}.zip"
        partial = self.root / f"products-{opid}.partial"
        try:
            if mode == "demo":
                await asyncio.to_thread(self.make_demo_package, partial, opid)
            else:
                await self.make_shopify_package(partial, opid)
            self.update(opid, message="Checking package counts and image checksums")
            checked = await asyncio.to_thread(package_preflight, partial)
            os.replace(partial, package)
            name = f"porter-products-{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.zip"
            self.update(opid, status="completed", message="Package is ready to download and review.",
                        current=checked["counts"].get("products", 0), total=checked["counts"].get("products", 0),
                        stats={**checked["counts"], "unique_image_files":checked["asset_count"], "warnings":checked["warnings"]},
                        filename=name, filepath=str(package))
        finally:
            partial.unlink(missing_ok=True)

    def make_demo_package(self, destination, opid):
        products = self.all_records("products")
        fields = self.all_records("metafields")
        by_owner = defaultdict(list)
        for item in fields:
            if str(item.get("Owner Type", "")).lower() == "product":
                by_owner[str(item.get("Owner ID", ""))].append({"namespace":item.get("Namespace", "custom"), "key":item.get("Key", ""), "type":item.get("Type", "single_line_text_field"), "value":str(item.get("Value", ""))})
        sample = (Path(__file__).resolve().parent / "public" / "demo-product.png").read_bytes()
        digest = hashlib.sha256(sample).hexdigest()
        asset = f"images/{digest}.png"
        count = Counter()
        with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as z:
            z.writestr(asset, sample, compress_type=zipfile.ZIP_STORED)
            with z.open("products.jsonl", "w", force_zip64=True) as out:
                for i, p in enumerate(products):
                    pid = str(p.get("ID") or i)
                    vid = f"gid://porter-demo/ProductVariant/{pid}"
                    mid = f"gid://porter-demo/MediaImage/{pid}"
                    data = {"id":f"gid://porter-demo/Product/{pid}", "handle":str(p.get("Handle") or f"sample-{pid}"),
                        "title":p.get("Title", "Sample product"), "descriptionHtml":"<p>Example product in the Porter demo workspace.</p>",
                        "vendor":p.get("Vendor", ""), "productType":"", "status":str(p.get("Status", "DRAFT")).upper(),
                        "tags":[x.strip() for x in str(p.get("Tags", "")).split(",") if x.strip()], "seo":{"title":None, "description":None},
                        "options":[{"name":"Title", "position":1, "values":["Default Title"]}],
                        "variants":[{"id":vid, "sku":p.get("SKU", ""), "price":str(p.get("Price", "0.00")), "taxable":True,
                                     "inventoryPolicy":"DENY", "selectedOptions":[{"name":"Title", "value":"Default Title"}], "media_ids":[mid]}],
                        "metafields":by_owner.get(pid, []),
                        "media":[{"id":mid, "mediaContentType":"IMAGE", "alt":"Illustrative demo image", "asset":asset}]}
                    out.write((jdump(data)+"\n").encode("utf-8"))
                    count["products"] += 1; count["variants"] += 1; count["images"] += 1; count["product_metafields"] += len(data["metafields"])
            manifest = {"format":FORMAT, "version":VERSION, "created_at":timestamp(),
                "source":{"kind":"demo", "name":"Porter demo workspace", "domain":None, "shop_id":None},
                "counts":{k:count[k] for k in ("products", "variants", "images", "product_metafields", "other_media")},
                "assets":[{"path":asset, "sha256":digest, "bytes":len(sample), "content_type":"image/png"}],
                "warnings":["The image is an illustrative demo placeholder. Demo packages cannot be imported to a live store.",
                            "Inventory quantities, product publications, collections and non-product metafields are not included."]}
            z.writestr("manifest.json", jdump(manifest).encode("utf-8"))

    async def make_shopify_package(self, destination, opid):
        self.update(opid, message="Checking the source Shopify store")
        with tempfile.TemporaryDirectory(prefix="porter-export-", dir=self.root) as temp_dir:
            temp = Path(temp_dir)
            index = sqlite3.connect(temp / "index.sqlite")
            index.execute("PRAGMA journal_mode=OFF")
            index.execute("CREATE TABLE nodes(seq INTEGER PRIMARY KEY, parent TEXT, node_id TEXT, kind TEXT, payload TEXT)")
            index.execute("CREATE INDEX nodes_parent ON nodes(parent)")
            try:
                async with self.client_factory("source") as client:
                    shop = await client.shop()
                    cfg = get_config("source")
                    if shop["myshopifyDomain"].lower() != cfg["domain"]:
                        raise ValueError("The source token does not match the configured source domain.")
                    scopes = set(shop.get("scopes") or [])
                    if not {"read_products", "read_inventory"}.issubset(scopes):
                        raise ValueError("The source token needs read_products and read_inventory scopes.")
                    async def progress(message, current, total):
                        self.update(opid, message=message, current=current, total=total)
                    count = 0
                    async for obj in client.bulk(BULK_PRODUCT_QUERY, progress):
                        if not isinstance(obj, dict) or not obj.get("id"):
                            raise ValueError("Shopify bulk output contains a record without an ID.")
                        parent = obj.pop("__parentId", None)
                        kind = obj.get("__typename") or str(obj["id"]).split("/")[-2]
                        index.execute("INSERT INTO nodes(parent,node_id,kind,payload) VALUES(?,?,?,?)", (parent, obj["id"], kind, jdump(obj)))
                        count += 1
                        if count % 2000 == 0:
                            index.commit()
                    index.commit()
                    product_total = index.execute("SELECT COUNT(*) FROM nodes WHERE parent IS NULL AND kind='Product'").fetchone()[0]
                    image_rows = index.execute("SELECT COUNT(*) FROM nodes WHERE kind='MediaImage' AND parent LIKE 'gid://shopify/Product/%'").fetchone()[0]
                    self.update(opid, message="Downloading product images", current=0, total=image_rows)
                    assets_by_url, assets, asset_paths, images_done = {}, [], set(), 0
                    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as z:
                        cursor = index.execute("SELECT payload FROM nodes WHERE kind='MediaImage' AND parent LIKE 'gid://shopify/Product/%' ORDER BY seq")
                        for (payload,) in cursor:
                            m = json.loads(payload)
                            url = (m.get("image") or {}).get("url")
                            if not url:
                                raise ValueError(f"Source image {m.get('id')} is not ready or has no downloadable URL. Wait for Shopify to finish processing it and retry.")
                            if url not in assets_by_url:
                                image_temp = temp / "image.tmp"
                                info = await client.download_image(url, image_temp, m.get("mimeType"))
                                path = f"images/{info['sha256']}.{info['extension']}"
                                if path not in asset_paths:
                                    z.write(image_temp, path, compress_type=zipfile.ZIP_STORED)
                                    asset_paths.add(path)
                                    assets.append({"path":path, "sha256":info["sha256"], "bytes":info["bytes"], "content_type":info["content_type"]})
                                assets_by_url[url] = path
                                image_temp.unlink(missing_ok=True)
                            images_done += 1
                            if images_done % 5 == 0 or images_done == image_rows:
                                self.update(opid, message="Downloading product images", current=images_done, total=image_rows)
                        stats = Counter()
                        self.update(opid, message="Writing the product catalog", current=0, total=product_total)
                        with z.open("products.jsonl", "w", force_zip64=True) as out:
                            cursor = index.execute("SELECT node_id,payload FROM nodes WHERE parent IS NULL AND kind='Product' ORDER BY seq")
                            for pid, payload in cursor:
                                product = json.loads(payload)
                                product.pop("__typename", None)
                                variants, metafields, media = [], [], []
                                for child_id, kind, child in index.execute("SELECT node_id,kind,payload FROM nodes WHERE parent=? ORDER BY seq", (pid,)):
                                    item = json.loads(child); item.pop("__typename", None)
                                    if kind == "ProductVariant":
                                        item["media_ids"] = [r[0] for r in index.execute("SELECT node_id FROM nodes WHERE parent=? ORDER BY seq", (child_id,))]
                                        variants.append(item)
                                    elif kind == "Metafield":
                                        metafields.append(item)
                                    elif item.get("mediaContentType"):
                                        if item["mediaContentType"] == "IMAGE":
                                            url = (item.get("image") or {}).get("url")
                                            item["asset"] = assets_by_url.get(url)
                                            if not item["asset"]:
                                                raise ValueError(f"A source image was not saved for product {product.get('handle')}.")
                                            stats["images"] += 1
                                        else:
                                            stats["other_media"] += 1
                                        # CDN URLs need not be stored in the portable package.
                                        item.pop("image", None)
                                        media.append(item)
                                product["variants"], product["metafields"], product["media"] = variants, metafields, media
                                out.write((jdump(product)+"\n").encode("utf-8"))
                                stats["products"] += 1; stats["variants"] += len(variants); stats["product_metafields"] += len(metafields)
                                if stats["products"] % 100 == 0:
                                    self.update(opid, message="Writing the product catalog", current=stats["products"], total=product_total)
                        warnings = ["The bundle contains product metafield values, not metafield definitions. Some reference types and app-owned namespaces need manual migration.",
                                    "Inventory quantities and locations, publications, collections, translations, variant metafields and non-product metafields are not migrated.",
                                    "Video and 3D media files are not included. Multiple media links on one variant may require manual review.",
                                    "Bundles, required selling plans, gift cards and products above 250 variants or images require a separate migration."]
                        manifest = {"format":FORMAT, "version":VERSION, "created_at":timestamp(),
                                    "source":{"kind":"shopify", "name":shop.get("name"), "domain":shop["myshopifyDomain"], "shop_id":shop["id"], "currency":shop.get("currencyCode")},
                                    "counts":{k:stats[k] for k in ("products", "variants", "images", "product_metafields", "other_media")},
                                    "assets":assets, "warnings":warnings}
                        z.writestr("manifest.json", jdump(manifest).encode("utf-8"))
            finally:
                index.close()

    async def import_worker(self, opid, stage, summary, shop, force_draft):
        report = self.root / f"report-{opid}.jsonl"
        self.update(opid, reportpath=str(report), message="Checking destination products", total=summary["counts"].get("products", 0))
        stats = Counter()
        def event(file, kind, **payload):
            file.write(jdump({"time":timestamp(), "kind":kind, **payload})+"\n")
            file.flush()
        try:
            with tempfile.TemporaryDirectory(prefix="porter-import-", dir=self.root) as temp_dir:
                index = sqlite3.connect(Path(temp_dir) / "mapping.sqlite")
                index.execute("CREATE TABLE id_map(old_id TEXT PRIMARY KEY, new_id TEXT NOT NULL, created INTEGER NOT NULL)")
                mapping = DiskIdMap(index)
                try:
                    with open(report, "w", encoding="utf-8") as log, zipfile.ZipFile(stage["filepath"], "r") as z:
                        event(log, "start", source=summary.get("source"), destination=shop.get("myshopifyDomain"), create_as_draft=force_draft,
                              notice="Existing product handles are not modified. Inventory quantities, definitions and channel publication are not migrated.")
                        assets = {a["path"]:a for a in summary["assets"]}
                        async with self.client_factory("destination") as client:
                            # First pass creates products/variants and saves their new IDs. A
                            # lookup immediately before every create makes retries create-only.
                            with z.open("products.jsonl") as catalog:
                                for line in catalog:
                                    product = json.loads(line); handle = product["handle"]
                                    try:
                                        existing = await client.find_product(handle)
                                        if existing:
                                            mapping.put(product["id"], existing["id"], False)
                                            stats["skipped_existing"] += 1
                                            event(log, "skipped_existing", handle=handle, destination_id=existing["id"],
                                                  reason="Matching handle exists; no product, variants, images or metafields were changed.")
                                        else:
                                            # Validate product constraints before uploading images.
                                            placeholder = {m["asset"]:{"resourceUrl":"https://example.invalid/staged", "filename":"x.png"}
                                                           for m in product.get("media", []) if m.get("mediaContentType") == "IMAGE"}
                                            build_product_input(product, placeholder, force_draft)
                                            staged = await client.stage_images(z, product.get("media", []), assets)
                                            input_doc = build_product_input(product, staged, force_draft)
                                            created = await client.create_product(input_doc)
                                            if created.get("handle") != handle:
                                                event(log, "unexpected_handle", handle=handle, returned_handle=created.get("handle"), destination_id=created.get("id"),
                                                      reason="The destination handle changed during creation. Stop and review the store.")
                                                raise RuntimeError(f"Shopify created {created.get('handle')} instead of {handle}; review the destination before continuing.")
                                            mapping.put(product["id"], created["id"], True)
                                            expected_images = sum(1 for m in product.get("media", []) if m.get("mediaContentType") == "IMAGE")
                                            returned_media = [m for m in ((created.get("media") or {}).get("nodes") or []) if m.get("mediaContentType") == "IMAGE"]
                                            if expected_images and (len(returned_media) < expected_images or any(m.get("status") != "READY" for m in returned_media)):
                                                stats["warnings"] += 1
                                                event(log, "warning", handle=handle,
                                                      reason="Shopify processes images asynchronously; not all images were confirmed READY. Inspect the destination product after processing.",
                                                      expected_images=expected_images, returned_media=returned_media)
                                            returned = ((created.get("variants") or {}).get("nodes") or [])
                                            variant_ids = {variant_key(v.get("selectedOptions")):v.get("id") for v in returned}
                                            for variant in product.get("variants", []):
                                                mapped = variant_ids.get(variant_key(variant.get("selectedOptions")))
                                                if mapped:
                                                    mapping.put(variant.get("id"), mapped, False)
                                                else:
                                                    stats["warnings"] += 1
                                                    event(log, "warning", handle=handle, reason="A variant ID could not be mapped for reference metafields.", source_variant=variant.get("id"))
                                                linked = variant.get("media_ids") or []
                                                if len(linked) > 1:
                                                    stats["warnings"] += 1
                                                    event(log, "warning", handle=handle, reason="Only the first image association per variant is restored; review additional media links.", source_variant=variant.get("id"), media_ids=linked)
                                            for media in product.get("media", []):
                                                if media.get("mediaContentType") != "IMAGE":
                                                    stats["warnings"] += 1
                                                    event(log, "warning", handle=handle, reason="Non-image media is not imported.", media_type=media.get("mediaContentType"), media_id=media.get("id"))
                                            for option in product.get("options", []):
                                                if option.get("linkedMetafield"):
                                                    stats["warnings"] += 1
                                                    event(log, "warning", handle=handle, reason="Option-to-metafield link needs manual review.", option=option.get("name"))
                                            stats["created"] += 1
                                            event(log, "created", handle=handle, source_id=product["id"], destination_id=created["id"], variants=len(product.get("variants", [])))
                                    except ValueError as e:
                                        stats["failed"] += 1
                                        event(log, "product_failed", handle=handle, reason=str(e))
                                    index.commit()
                                    stats["processed"] += 1
                                    self.update(opid, message="Creating products in the destination", current=stats["processed"], total=summary["counts"].get("products", 0), stats=dict(stats))
                            # Second pass resolves product/variant reference IDs after all
                            # products exist; skipped existing products are never modified.
                            self.update(opid, message="Writing product metafields", current=0, total=summary["counts"].get("product_metafields", 0), stats=dict(stats))
                            with z.open("products.jsonl") as catalog:
                                for line in catalog:
                                    product = json.loads(line)
                                    dest_id = mapping.created_product(product["id"])
                                    if not dest_id:
                                        continue
                                    pending = []
                                    for field in product.get("metafields", []):
                                        try:
                                            if not field.get("namespace") or not field.get("key"):
                                                raise ValueError("The namespace or key is missing.")
                                            value = metafield_value(field, mapping)
                                            pending.append({"ownerId":dest_id, "namespace":field["namespace"], "key":field["key"], "type":field["type"], "value":value, "compareDigest":None})
                                        except ValueError as e:
                                            stats["metafields_unresolved"] += 1
                                            event(log, "metafield_unresolved", handle=product["handle"], namespace=field.get("namespace"), key=field.get("key"), type=field.get("type"), value=field.get("value"), reason=str(e))
                                    for start in range(0, len(pending), 25):
                                        batch = pending[start:start+25]
                                        try:
                                            errors = await client.set_metafields(batch)
                                        except ValueError as e:
                                            errors = [{"message":str(e)}]
                                        if not errors:
                                            stats["metafields_written"] += len(batch)
                                        else:
                                            # metafieldsSet is atomic, so retry individually to
                                            # preserve valid fields and identify rejected ones.
                                            for field in batch:
                                                try:
                                                    one_error = await client.set_metafields([field])
                                                except ValueError as e:
                                                    one_error = [{"message":str(e)}]
                                                if one_error:
                                                    stats["metafields_unresolved"] += 1
                                                    event(log, "metafield_failed", handle=product["handle"], namespace=field["namespace"], key=field["key"], type=field["type"], value=field["value"], errors=one_error)
                                                else:
                                                    stats["metafields_written"] += 1
                                        self.update(opid, message="Writing product metafields", current=stats["metafields_written"] + stats["metafields_unresolved"], total=summary["counts"].get("product_metafields", 0), stats=dict(stats))
                            event(log, "summary", **dict(stats))
                except BaseException as e:
                    # Unknown-outcome mutation failures stop the entire job, not
                    # an automatic retry. Keep an audit trail even after failure.
                    with open(report, "a", encoding="utf-8") as log:
                        event(log, "fatal", reason=str(e), stats=dict(stats))
                    raise
                finally:
                    index.close()
            warnings = stats["failed"] + stats["metafields_unresolved"] + stats["warnings"]
            status = "completed_with_warnings" if warnings else "completed"
            self.update(opid, status=status, message=("Import finished with items to review. Download the report." if warnings else "Import finished. Review products and the report before publishing."),
                        current=summary["counts"].get("products", 0), total=summary["counts"].get("products", 0), stats=dict(stats))
        finally:
            self.running_live_import = False


def register_migration(app, db_factory, all_records, base_dir, client_factory=ShopifyClient):
    service = MigrationService(app, db_factory, all_records, base_dir, client_factory)
    app[MIGRATION_APP_KEY] = service
    r = app.router
    r.add_get("/api/migration/config", service.config)
    r.add_post("/api/migration/unlock", service.unlock)
    r.add_post("/api/migration/lock", service.lock)
    r.add_post("/api/migration/test", service.test_connection)
    r.add_get("/api/migration/operations", service.operations)
    r.add_post("/api/migration/export", service.start_export)
    r.add_get("/api/migration/download/{opid}", service.download)
    r.add_get("/api/migration/report/{opid}", service.report)
    r.add_post("/api/migration/stage", service.stage)
    r.add_post("/api/migration/import", service.start_import)
    app.on_cleanup.append(service.shutdown)
    return service
