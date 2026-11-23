import logging
import mimetypes
import time
import uuid
from pathlib import Path

import requests

from .config import CFG, env

log = logging.getLogger(__name__)

PRODUCT_SET = """
mutation ProductSet($input: ProductSetInput!, $identifier: ProductSetIdentifiers) {
  productSet(input: $input, identifier: $identifier, synchronous: true) {
    product {
      id
      handle
      status
      onlineStoreUrl
      variants(first: 100) { nodes { id sku inventoryItem { id } } }
    }
    userErrors { field message code }
  }
}"""

STAGED_UPLOADS = """
mutation Staged($input: [StagedUploadInput!]!) {
  stagedUploadsCreate(input: $input) {
    stagedTargets { url resourceUrl parameters { name value } }
    userErrors { field message }
  }
}"""

INVENTORY_SET = """
mutation InvSet($input: InventorySetQuantitiesInput!, $key: String!) {
  inventorySetQuantities(input: $input) @idempotent(key: $key) {
    userErrors { field message code }
  }
}"""

PRODUCT_STATUS = """
mutation Status($product: ProductUpdateInput!) {
  productUpdate(product: $product) {
    product { id status }
    userErrors { field message }
  }
}"""

PUBLISH = """
mutation Publish($id: ID!, $input: [PublicationInput!]!) {
  publishablePublish(id: $id, input: $input) {
    userErrors { field message }
  }
}"""

COLLECTIONS = """
query Cols($after: String) {
  collections(first: 100, after: $after, sortKey: TITLE) {
    nodes { id title ruleSet { appliedDisjunctively } }
    pageInfo { hasNextPage endCursor }
  }
}"""

COLLECTION_CREATE = """
mutation ColCreate($input: CollectionInput!) {
  collectionCreate(input: $input) {
    collection { id title }
    userErrors { field message }
  }
}"""

COLLECTION_ADD = """
mutation ColAdd($id: ID!, $productIds: [ID!]!) {
  collectionAddProducts(id: $id, productIds: $productIds) {
    userErrors { field message }
  }
}"""

COLLECTION_REMOVE = """
mutation ColRemove($id: ID!, $productIds: [ID!]!) {
  collectionRemoveProducts(id: $id, productIds: $productIds) {
    job { id }
    userErrors { field message }
  }
}"""

PRODUCT_INFO = "query PC($id: ID!) { product(id: $id) { status onlineStoreUrl collections(first: 50) { nodes { id title } } } }"

PRODUCT_EXISTS = "query P($id: ID!) { product(id: $id) { id status } }"
FILE_CREATE = """
mutation FileCreate($files: [FileCreateInput!]!) {
  fileCreate(files: $files) { files { id } userErrors { field message } }
}"""
FILE_STATUS = "query F($id: ID!) { node(id: $id) { ... on MediaImage { fileStatus image { url } } } }"
VIDEO_STATUS = """query V($id: ID!) { node(id: $id) { ... on Video {
  fileStatus originalSource { url } sources { url mimeType height } } } }"""
LOCATIONS = "query Locations { locations(first: 20) { nodes { id name isActive } } }"
SHOP = "query Shop { shop { name myshopifyDomain } }"


class ShopifyError(Exception):
    pass


class Shopify:
    def __init__(self):
        c = CFG["shopify"]
        self.shop = c["shop"]
        self.endpoint = f"https://{self.shop}/admin/api/{c['api_version']}/graphql.json"
        self._static_token = env("SHOPIFY_ACCESS_TOKEN", required=False)
        self._token = None
        self._token_expires = 0.0
        self._location = c.get("location_id")

    # --- auth ----------------------------------------------------------------
    def _access_token(self) -> str:
        if self._static_token:
            return self._static_token
        if self._token and time.time() < self._token_expires - 300:
            return self._token
        r = requests.post(
            f"https://{self.shop}/admin/oauth/access_token",
            data={"grant_type": "client_credentials",
                  "client_id": env("SHOPIFY_CLIENT_ID"),
                  "client_secret": env("SHOPIFY_CLIENT_SECRET")},
            timeout=30,
        )
        if r.status_code != 200:
            raise ShopifyError(f"Could not get Shopify token ({r.status_code}): {r.text[:300]}")
        body = r.json()
        self._token = body["access_token"]
        self._token_expires = time.time() + int(body.get("expires_in", 86399))
        return self._token

    def gql(self, query: str, variables: dict | None = None) -> dict:
        for attempt in range(5):
            r = requests.post(
                self.endpoint,
                json={"query": query, "variables": variables or {}},
                headers={"X-Shopify-Access-Token": self._access_token()},
                timeout=120,
            )
            if r.status_code == 429 or (r.ok and "THROTTLED" in r.text[:500]):
                time.sleep(2 * (attempt + 1))
                continue
            if r.status_code == 401 and not self._static_token:
                self._token = None
                continue
            if not r.ok:
                raise ShopifyError(f"Shopify HTTP {r.status_code}: {r.text[:500]}")
            body = r.json()
            if body.get("errors"):
                raise ShopifyError(f"Shopify GraphQL errors: {body['errors']}")
            return body["data"]
        raise ShopifyError("Shopify kept throttling the request")

    @staticmethod
    def _check(payload: dict, what: str):
        errors = payload.get("userErrors") or []
        if errors:
            raise ShopifyError(f"{what}: " + "; ".join(f"{e.get('field')}: {e['message']}" for e in errors))

    # --- helpers -------------------------------------------------------------
    def shop_name(self) -> str:
        return self.gql(SHOP)["shop"]["name"]

    def location_id(self) -> str:
        if not self._location:
            nodes = self.gql(LOCATIONS)["locations"]["nodes"]
            active = [n for n in nodes if n["isActive"]]
            if not active:
                raise ShopifyError("No active Shopify location found")
            self._location = active[0]["id"]
            log.info("Using Shopify location %s (%s)", active[0]["name"], self._location)
        return self._location

    def upload_image(self, path: Path, resource: str = "IMAGE") -> str:
        """Uploads a local file to Shopify's staging bucket; returns a URL usable as originalSource."""
        mime = mimetypes.guess_type(path.name)[0] or ("video/mp4" if resource == "VIDEO" else "image/jpeg")
        data = self.gql(STAGED_UPLOADS, {"input": [{
            "resource": resource, "filename": path.name, "mimeType": mime,
            "httpMethod": "POST", "fileSize": str(path.stat().st_size),
        }]})["stagedUploadsCreate"]
        self._check(data, "stagedUploadsCreate")
        target = data["stagedTargets"][0]
        form = {p["name"]: p["value"] for p in target["parameters"]}
        with open(path, "rb") as f:
            r = requests.post(target["url"], data=form, files={"file": (path.name, f, mime)}, timeout=600)
        if r.status_code not in (200, 201, 204):
            raise ShopifyError(f"Image upload failed ({r.status_code}): {r.text[:300]}")
        return target["resourceUrl"]

    def host_image(self, path: Path, alt: str = "", timeout: int = 120) -> str:
        """Uploads a local image to Shopify Files and returns its public CDN URL (for Instagram)."""
        data = self.gql(FILE_CREATE, {"files": [{"originalSource": self.upload_image(path),
                                                  "contentType": "IMAGE", "alt": alt[:500]}]})["fileCreate"]
        self._check(data, "fileCreate")
        fid = data["files"][0]["id"]
        deadline = time.time() + timeout
        while time.time() < deadline:
            node = self.gql(FILE_STATUS, {"id": fid})["node"] or {}
            if node.get("fileStatus") == "FAILED":
                raise ShopifyError(f"Shopify could not process {path.name}")
            if node.get("fileStatus") == "READY" and (node.get("image") or {}).get("url"):
                return node["image"]["url"]
            time.sleep(3)
        raise ShopifyError(f"Timed out waiting for Shopify to process {path.name}")

    def host_video(self, path: Path, alt: str = "", timeout: int = 600) -> str:
        """Uploads a local mp4 to Shopify Files and returns a public URL of the original file (for
        Instagram/Facebook Reels, which fetch the video from a URL)."""
        data = self.gql(FILE_CREATE, {"files": [{"originalSource": self.upload_image(path, "VIDEO"),
                                                  "contentType": "VIDEO", "alt": alt[:500]}]})["fileCreate"]
        self._check(data, "fileCreate")
        fid = data["files"][0]["id"]
        deadline = time.time() + timeout
        while time.time() < deadline:
            node = self.gql(VIDEO_STATUS, {"id": fid})["node"] or {}
            if node.get("fileStatus") == "FAILED":
                raise ShopifyError(f"Shopify could not process {path.name}")
            if node.get("fileStatus") == "READY":
                mp4 = [s for s in node.get("sources") or [] if s.get("mimeType") == "video/mp4"]
                url = (node.get("originalSource") or {}).get("url") or                     (max(mp4, key=lambda s: s.get("height") or 0)["url"] if mp4 else "")
                if url:
                    return url
            time.sleep(5)
        raise ShopifyError(f"Timed out waiting for Shopify to process {path.name}")

    def product_set(self, product_input: dict, product_id: str | None = None) -> dict:
        variables = {"input": product_input}
        if product_id:
            variables["identifier"] = {"id": product_id}
        data = self.gql(PRODUCT_SET, variables)["productSet"]
        self._check(data, "productSet")
        return data["product"]

    def set_inventory(self, quantities: list[tuple[str, int]]) -> None:
        """quantities: [(inventory_item_id, available_qty)] at the configured location."""
        if not quantities:
            return
        loc = self.location_id()
        data = self.gql(INVENTORY_SET, {"input": {
            "name": "available", "reason": "correction",
            "quantities": [{"inventoryItemId": i, "locationId": loc, "quantity": q, "changeFromQuantity": None}
                           for i, q in quantities],
        }, "key": str(uuid.uuid4())})["inventorySetQuantities"]
        self._check(data, "inventorySetQuantities")

    def set_status(self, product_id: str, status: str) -> None:
        data = self.gql(PRODUCT_STATUS, {"product": {"id": product_id, "status": status}})["productUpdate"]
        self._check(data, "productUpdate")

    def publish(self, resource_id: str) -> None:
        """Publishes a product or collection to the configured sales channels (Online Store)."""
        pubs = CFG["shopify"].get("publications") or []
        if not pubs:
            return
        data = self.gql(PUBLISH, {"id": resource_id, "input": [{"publicationId": p} for p in pubs]})
        self._check(data["publishablePublish"], "publishablePublish")

    def product_info(self, product_id: str) -> dict | None:
        p = self.gql(PRODUCT_INFO, {"id": product_id})["product"]
        if p:
            p["collections"] = p["collections"]["nodes"]
        return p

    def manual_collections(self) -> list[dict]:
        """Collections products can be added to by hand (smart/automated ones are excluded)."""
        out, after = [], None
        while True:
            page = self.gql(COLLECTIONS, {"after": after})["collections"]
            out += [{"id": c["id"], "title": c["title"]} for c in page["nodes"] if not c["ruleSet"]]
            if not page["pageInfo"]["hasNextPage"]:
                return out
            after = page["pageInfo"]["endCursor"]

    def collection_by_title(self, title: str, create: bool = True) -> dict | None:
        for c in self.manual_collections():
            if c["title"].strip().lower() == title.strip().lower():
                return c
        if not create:
            return None
        data = self.gql(COLLECTION_CREATE, {"input": {"title": title.strip()}})["collectionCreate"]
        self._check(data, "collectionCreate")
        self.publish(data["collection"]["id"])
        log.info("Created collection %s", title)
        return data["collection"]

    def add_to_collection(self, collection_id: str, product_id: str) -> None:
        data = self.gql(COLLECTION_ADD, {"id": collection_id, "productIds": [product_id]})["collectionAddProducts"]
        self._check(data, "collectionAddProducts")

    def remove_from_collection(self, collection_id: str, product_id: str) -> None:
        data = self.gql(COLLECTION_REMOVE, {"id": collection_id, "productIds": [product_id]})["collectionRemoveProducts"]
        self._check(data, "collectionRemoveProducts")

    def product_exists(self, product_id: str) -> bool:
        return self.gql(PRODUCT_EXISTS, {"id": product_id})["product"] is not None

    def admin_url(self, product_id: str) -> str:
        return f"https://admin.shopify.com/store/{self.shop.split('.')[0]}/products/{product_id.rsplit('/', 1)[-1]}"
