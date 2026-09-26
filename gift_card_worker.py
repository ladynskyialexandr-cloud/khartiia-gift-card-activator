from __future__ import annotations

import argparse
import csv
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

SHOPIFY_SHOP_DOMAIN = os.environ.get("SHOPIFY_SHOP_DOMAIN", "").strip()
SHOPIFY_CLIENT_ID = os.environ.get("SHOPIFY_CLIENT_ID", "").strip()
SHOPIFY_CLIENT_SECRET = os.environ.get("SHOPIFY_CLIENT_SECRET", "").strip()
SHOPIFY_API_VERSION = os.environ.get("SHOPIFY_API_VERSION", "2026-07").strip()

ODOO_URL = os.environ.get("ODOO_URL", "").strip().rstrip("/")
ODOO_DB = os.environ.get("ODOO_DB", "").strip()
ODOO_API_KEY = os.environ.get("ODOO_API_KEY", "").strip()
INITIAL_STOCK_LOCATION = os.environ.get("INITIAL_STOCK_LOCATION", "IM/Основний").strip()

CARDS_FILE = Path(__file__).with_name("cards.csv")
SKU_FACE_VALUES = {
    "GIFT-500": 500.0,
    "GIFT-1000": 1000.0,
    "GIFT-2000": 2000.0,
    "GIFT-3000": 3000.0,
    "GIFT-5000": 5000.0,
    "GIFT-TEST-10": 10.0,
}
CODE_RE = re.compile(r"^[A-Z0-9]{8,20}$")


def log(msg: str):
    print(f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] {msg}", flush=True)


def normalize_code(code: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", code or "").upper()


class OdooClient:
    def __init__(self):
        if not ODOO_URL or not ODOO_API_KEY:
            raise RuntimeError("ODOO_URL and ODOO_API_KEY are required")
        self.base = ODOO_URL.rstrip("/") + "/json/2"
        self.headers = {
            "Authorization": f"bearer {ODOO_API_KEY}",
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "Khartiia-Gift-Card-Activator/1.0",
        }
        if ODOO_DB:
            self.headers["X-Odoo-Database"] = ODOO_DB
        self.http = httpx.Client(timeout=45.0, headers=self.headers)

    def call(self, model: str, method: str, *, ids=None, **params):
        body = dict(params)
        if ids is not None:
            body["ids"] = ids
        r = self.http.post(f"{self.base}/{model}/{method}", json=body)
        if r.status_code >= 400:
            raise RuntimeError(f"Odoo {model}.{method} HTTP {r.status_code}: {r.text[:1200]}")
        return r.json()

    def search_read(self, model: str, domain=None, fields=None, limit=100, order=None):
        params = {"domain": domain or [], "limit": limit}
        if fields is not None:
            params["fields"] = fields
        if order:
            params["order"] = order
        return self.call(model, "search_read", **params)


class ShopifyClient:
    def __init__(self):
        if not SHOPIFY_SHOP_DOMAIN or not SHOPIFY_CLIENT_ID or not SHOPIFY_CLIENT_SECRET:
            raise RuntimeError("SHOPIFY_SHOP_DOMAIN, SHOPIFY_CLIENT_ID and SHOPIFY_CLIENT_SECRET are required")
        self.domain = SHOPIFY_SHOP_DOMAIN.replace("https://", "").replace("http://", "").rstrip("/")
        self.http = httpx.Client(timeout=45.0)
        self.token = None
        self.expires_at = 0.0

    def access_token(self) -> str:
        if self.token and time.time() < self.expires_at - 300:
            return self.token
        r = self.http.post(
            f"https://{self.domain}/admin/oauth/access_token",
            data={
                "grant_type": "client_credentials",
                "client_id": SHOPIFY_CLIENT_ID,
                "client_secret": SHOPIFY_CLIENT_SECRET,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if r.status_code >= 400:
            raise RuntimeError(f"Shopify OAuth HTTP {r.status_code}: {r.text[:1200]}")
        data = r.json()
        self.token = data.get("access_token")
        if not self.token:
            raise RuntimeError("Shopify OAuth response did not include access_token")
        self.expires_at = time.time() + int(data.get("expires_in") or 86399)
        return self.token

    def graphql(self, query: str, variables=None):
        r = self.http.post(
            f"https://{self.domain}/admin/api/{SHOPIFY_API_VERSION}/graphql.json",
            json={"query": query, "variables": variables or {}},
            headers={
                "X-Shopify-Access-Token": self.access_token(),
                "Content-Type": "application/json",
            },
        )
        if r.status_code >= 400:
            raise RuntimeError(f"Shopify GraphQL HTTP {r.status_code}: {r.text[:1200]}")
        data = r.json()
        if data.get("errors"):
            raise RuntimeError("Shopify GraphQL: " + json.dumps(data["errors"], ensure_ascii=False))
        return data["data"]

    def order_is_paid_for_sku(self, order_name: str, sku: str) -> bool:
        query = """
        query($q: String!) {
          orders(first: 10, query: $q) {
            nodes {
              name
              displayFinancialStatus
              lineItems(first: 100) { nodes { sku quantity } }
            }
          }
        }
        """
        data = self.graphql(query, {"q": f"name:{order_name}"})
        exact = [x for x in data["orders"]["nodes"] if x.get("name") == order_name]
        if len(exact) != 1:
            return False
        order = exact[0]
        if order.get("displayFinancialStatus") != "PAID":
            return False
        return any(
            li.get("sku") == sku and int(li.get("quantity") or 0) > 0
            for li in order["lineItems"]["nodes"]
        )

    def create_gift_card(self, code: str, amount: float, note: str) -> str:
        mutation = """
        mutation($input: GiftCardCreateInput!) {
          giftCardCreate(input: $input) {
            giftCard { id lastCharacters }
            giftCardCode
            userErrors { field message }
          }
        }
        """
        payload = self.graphql(
            mutation,
            {"input": {
                "initialValue": f"{amount:.2f}",
                "code": code,
                "note": note[:500],
            }},
        )["giftCardCreate"]
        errors = payload.get("userErrors") or []
        if errors:
            raise RuntimeError(" | ".join(x.get("message", "Unknown Shopify error") for x in errors))
        card = payload.get("giftCard") or {}
        if not card.get("id"):
            raise RuntimeError("Shopify returned no Gift Card ID")
        return card["id"]


def m2o_id(value):
    if isinstance(value, (list, tuple)) and value:
        return int(value[0])
    if isinstance(value, int):
        return value
    return None


def load_cards():
    with CARDS_FILE.open("r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def ensure_custom_fields(odoo: OdooClient):
    fields = odoo.call("stock.lot", "fields_get")
    wanted = {
        "x_gc_card_number": ("Порядковий номер картки", "char"),
        "x_gc_shopify_code": ("Shopify code", "char"),
        "x_gc_shopify_id": ("Shopify Gift Card ID", "char"),
        "x_gc_activated": ("Сертифікат активовано", "boolean"),
        "x_gc_activated_at": ("Дата активації сертифіката", "datetime"),
        "x_gc_activation_order": ("Замовлення активації", "char"),
        "x_gc_activation_error": ("Помилка активації сертифіката", "text"),
    }
    missing = [k for k in wanted if k not in fields]
    if not missing:
        return
    model = odoo.search_read("ir.model", [["model", "=", "stock.lot"]], ["id"], limit=1)
    if not model:
        raise RuntimeError("Could not resolve ir.model for stock.lot")
    model_id = model[0]["id"]
    for name in missing:
        label, ttype = wanted[name]
        odoo.call("ir.model.fields", "create", vals_list={
            "model_id": model_id,
            "model": "stock.lot",
            "name": name,
            "field_description": label,
            "ttype": ttype,
            "state": "manual",
            "store": True,
            "readonly": False,
        })
        log(f"Created Odoo field stock.lot.{name}")


def product_map(odoo: OdooClient):
    rows = odoo.search_read(
        "product.product",
        [["default_code", "in", list(SKU_FACE_VALUES)]],
        ["id", "name", "default_code", "product_tmpl_id", "active"],
        limit=50,
    )
    return {r["default_code"]: r for r in rows}


def configure_products(odoo: OdooClient):
    products = product_map(odoo)
    for sku, amount in SKU_FACE_VALUES.items():
        if sku == "GIFT-TEST-10":
            continue
        product = products.get(sku)
        if not product:
            raise RuntimeError(f"Missing Odoo product {sku}")
        tmpl_id = m2o_id(product["product_tmpl_id"])
        history = odoo.search_read("stock.move", [["product_id", "=", product["id"]]], ["id"], limit=1)
        sales = odoo.search_read("sale.order.line", [["product_id", "=", product["id"]]], ["id"], limit=1)
        current = odoo.search_read(
            "product.template",
            [["id", "=", tmpl_id]],
            ["type", "is_storable", "tracking", "list_price"],
            limit=1,
        )[0]
        if history or sales:
            if current.get("type") == "consu" and current.get("is_storable") and current.get("tracking") == "serial":
                continue
            raise RuntimeError(f"{sku} has operational history; refusing product-type migration")
        odoo.call("product.template", "write", ids=[tmpl_id], vals={
            "type": "consu",
            "is_storable": True,
            "tracking": "serial",
            "list_price": amount,
            "sale_ok": True,
        })
        check = odoo.search_read(
            "product.template",
            [["id", "=", tmpl_id]],
            ["type", "is_storable", "tracking", "list_price"],
            limit=1,
        )[0]
        if not (check.get("type") == "consu" and check.get("is_storable") and check.get("tracking") == "serial"):
            raise RuntimeError(f"Odoo did not persist stock/serial config for {sku}: {check}")
        log(f"Configured {sku} as stockable serial-tracked product")


def find_initial_location(odoo: OdooClient):
    if not INITIAL_STOCK_LOCATION:
        return None
    rows = odoo.search_read(
        "stock.location",
        [["complete_name", "=", INITIAL_STOCK_LOCATION], ["usage", "=", "internal"]],
        ["id", "complete_name"],
        limit=2,
    )
    if len(rows) != 1:
        raise RuntimeError(f"Initial stock location not found/ambiguous: {INITIAL_STOCK_LOCATION}")
    return rows[0]["id"]


def import_cards(odoo: OdooClient):
    ensure_custom_fields(odoo)
    products = product_map(odoo)
    location_id = find_initial_location(odoo)
    created_lots = 0
    adjusted = []

    for card in load_cards():
        sku = card["sku"]
        product = products.get(sku)
        if not product:
            raise RuntimeError(f"Missing Odoo product for {sku}")
        printed = card["printed_code"].strip().lower()
        shop_code = card["shopify_code"].strip().upper()
        card_no = card["card_number"].strip()
        if not CODE_RE.fullmatch(shop_code):
            raise RuntimeError(f"Invalid Shopify code for card #{card_no}: {shop_code}")

        lots = odoo.search_read(
            "stock.lot",
            [["product_id", "=", product["id"]], ["name", "=", printed]],
            ["id", "name", "x_gc_card_number", "x_gc_shopify_code", "x_gc_activated"],
            limit=2,
        )
        if len(lots) > 1:
            raise RuntimeError(f"Duplicate Odoo lots for {sku}/{printed}")
        if lots:
            lot_id = lots[0]["id"]
            vals = {}
            if (lots[0].get("x_gc_card_number") or "") != card_no:
                vals["x_gc_card_number"] = card_no
            if (lots[0].get("x_gc_shopify_code") or "") != shop_code:
                vals["x_gc_shopify_code"] = shop_code
            if vals:
                odoo.call("stock.lot", "write", ids=[lot_id], vals=vals)
        else:
            lot_id = odoo.call("stock.lot", "create", vals_list={
                "name": printed,
                "product_id": product["id"],
                "x_gc_card_number": card_no,
                "x_gc_shopify_code": shop_code,
            })
            if isinstance(lot_id, list):
                lot_id = lot_id[0]
            created_lots += 1

        if location_id:
            quants = odoo.search_read(
                "stock.quant",
                [["product_id", "=", product["id"]], ["lot_id", "=", lot_id], ["location_id.usage", "=", "internal"]],
                ["id", "quantity", "location_id"],
                limit=20,
            )
            total = sum(float(q.get("quantity") or 0) for q in quants)
            if total <= 0:
                qid = odoo.call("stock.quant", "create", vals_list={
                    "product_id": product["id"],
                    "location_id": location_id,
                    "lot_id": lot_id,
                    "inventory_quantity": 1.0,
                })
                if isinstance(qid, list):
                    qid = qid[0]
                adjusted.append(qid)

    if adjusted:
        odoo.call("stock.quant", "action_apply_inventory", ids=adjusted)
    log(
        f"Card registry ready: {len(load_cards())} cards; "
        f"new lots={created_lots}; initial-stock adjustments={len(adjusted)}"
    )


def odoo_order_paid(odoo: OdooClient, order_id: int) -> bool:
    order = odoo.search_read("sale.order", [["id", "=", order_id]], ["invoice_ids"], limit=1)
    if not order or not order[0].get("invoice_ids"):
        return False
    invoices = odoo.search_read(
        "account.move",
        [["id", "in", order[0]["invoice_ids"]], ["move_type", "=", "out_invoice"]],
        ["state", "payment_state", "amount_residual"],
        limit=50,
    )
    return bool(invoices) and all(
        x.get("state") == "posted"
        and x.get("payment_state") == "paid"
        and abs(float(x.get("amount_residual") or 0)) < 0.0001
        for x in invoices
    )


def activate_pending_cards(odoo: OdooClient, shopify: ShopifyClient):
    products = product_map(odoo)
    by_id = {p["id"]: p for p in products.values()}
    ids = list(by_id)
    moves = odoo.search_read(
        "stock.move.line",
        [
            ["product_id", "in", ids],
            ["state", "=", "done"],
            ["location_dest_id.usage", "=", "customer"],
            ["lot_id", "!=", False],
            ["quantity", ">", 0],
        ],
        ["id", "move_id", "product_id", "lot_id", "quantity", "date"],
        limit=5000,
        order="date desc, id desc",
    )
    count = 0
    for ml in moves:
        if abs(float(ml.get("quantity") or 0) - 1.0) > 0.0001:
            continue
        lot_id = m2o_id(ml.get("lot_id"))
        product_id = m2o_id(ml.get("product_id"))
        p = by_id.get(product_id)
        if not lot_id or not p:
            continue
        lots = odoo.search_read(
            "stock.lot",
            [["id", "=", lot_id]],
            [
                "id",
                "name",
                "x_gc_card_number",
                "x_gc_shopify_code",
                "x_gc_shopify_id",
                "x_gc_activated",
            ],
            limit=1,
        )
        if not lots:
            continue
        lot = lots[0]
        if lot.get("x_gc_activated") or lot.get("x_gc_shopify_id"):
            continue

        move = odoo.search_read(
            "stock.move",
            [["id", "=", m2o_id(ml["move_id"])]],
            ["sale_line_id"],
            limit=1,
        )
        if not move or not move[0].get("sale_line_id"):
            continue
        line_id = m2o_id(move[0]["sale_line_id"])
        line = odoo.search_read(
            "sale.order.line",
            [["id", "=", line_id]],
            ["order_id"],
            limit=1,
        )
        if not line:
            continue
        order_id = m2o_id(line[0]["order_id"])
        order = odoo.search_read(
            "sale.order",
            [["id", "=", order_id]],
            ["name", "client_order_ref"],
            limit=1,
        )[0]
        order_name = order.get("name") or ""
        sku = p["default_code"]

        paid = False
        if order_name.startswith("#"):
            try:
                paid = shopify.order_is_paid_for_sku(order_name, sku)
            except Exception as exc:
                log(f"{order_name}: Shopify payment check failed: {exc}")
        if not paid:
            paid = odoo_order_paid(odoo, order_id)
        if not paid:
            continue

        code = (lot.get("x_gc_shopify_code") or normalize_code(lot.get("name") or "")).upper()
        if not CODE_RE.fullmatch(code):
            odoo.call(
                "stock.lot",
                "write",
                ids=[lot_id],
                vals={"x_gc_activation_error": f"Invalid Shopify code: {code}"},
            )
            continue

        amount = SKU_FACE_VALUES[sku]
        note = (
            f"Odoo {order_name}; SKU {sku}; card #{lot.get('x_gc_card_number') or '-'}; "
            f"serial {lot.get('name') or ''}"
        )
        try:
            gc_id = shopify.create_gift_card(code, amount, note)
            odoo.call(
                "stock.lot",
                "write",
                ids=[lot_id],
                vals={
                    "x_gc_shopify_id": gc_id,
                    "x_gc_activated": True,
                    "x_gc_activated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                    "x_gc_activation_order": order_name,
                    "x_gc_activation_error": False,
                },
            )
            count += 1
            log(
                f"ACTIVATED {sku} card #{lot.get('x_gc_card_number') or '-'} "
                f"({lot.get('name')}) for {order_name}"
            )
        except Exception as exc:
            msg = str(exc)[:1500]
            odoo.call(
                "stock.lot",
                "write",
                ids=[lot_id],
                vals={"x_gc_activation_error": msg},
            )
            log(f"Activation failed for {order_name}/{lot.get('name')}: {msg}")
    return count


def run_once(setup=True):
    odoo = OdooClient()
    shopify = ShopifyClient()
    shop = shopify.graphql("query { shop { name myshopifyDomain } }")["shop"]
    log(f"Shopify OK: {shop['name']} / {shop['myshopifyDomain']}")
    if setup:
        configure_products(odoo)
        import_cards(odoo)
    count = activate_pending_cards(odoo, shopify)
    log(f"Cycle complete; activated={count}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.parse_args()
    run_once(setup=True)


if __name__ == "__main__":
    main()
