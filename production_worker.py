from __future__ import annotations

import os
from datetime import datetime, timezone

from gift_card_worker import (
    CODE_RE,
    OdooClient,
    ShopifyClient,
    configure_products,
    import_cards,
    log,
    m2o_id,
    normalize_code,
    odoo_order_paid,
)

REAL_SKUS = {
    "GIFT-500": 500.0,
    "GIFT-1000": 1000.0,
    "GIFT-2000": 2000.0,
    "GIFT-3000": 3000.0,
    "GIFT-5000": 5000.0,
}

SERVICE_URL = os.environ.get("SERVICE_URL", "").rstrip("/")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")


def real_product_map(odoo):
    rows = odoo.search_read(
        "product.product",
        [["default_code", "in", list(REAL_SKUS)]],
        ["id", "name", "default_code"],
        limit=20,
    )
    return {row["id"]: row for row in rows}


def activate_pending_real_cards(odoo, shopify):
    products = real_product_map(odoo)
    moves = odoo.search_read(
        "stock.move.line",
        [
            ["product_id", "in", list(products)],
            ["state", "=", "done"],
            ["location_dest_id.usage", "=", "customer"],
            ["lot_id", "!=", False],
            ["quantity", ">", 0],
        ],
        ["id", "move_id", "product_id", "lot_id", "quantity", "date"],
        limit=5000,
        order="date desc, id desc",
    )

    activated = 0
    for ml in moves:
        if abs(float(ml.get("quantity") or 0) - 1.0) > 0.0001:
            continue
        lot_id = m2o_id(ml.get("lot_id"))
        product_id = m2o_id(ml.get("product_id"))
        product = products.get(product_id)
        if not lot_id or not product:
            continue

        lot_rows = odoo.search_read(
            "stock.lot",
            [["id", "=", lot_id]],
            [
                "id", "name", "x_gc_card_number", "x_gc_shopify_code",
                "x_gc_shopify_id", "x_gc_activated",
            ],
            limit=1,
        )
        if not lot_rows:
            continue
        lot = lot_rows[0]
        if lot.get("x_gc_activated") or lot.get("x_gc_shopify_id"):
            continue

        move_rows = odoo.search_read(
            "stock.move",
            [["id", "=", m2o_id(ml["move_id"])]],
            ["sale_line_id"],
            limit=1,
        )
        if not move_rows or not move_rows[0].get("sale_line_id"):
            continue
        line_id = m2o_id(move_rows[0]["sale_line_id"])
        line_rows = odoo.search_read(
            "sale.order.line",
            [["id", "=", line_id]],
            ["order_id"],
            limit=1,
        )
        if not line_rows:
            continue
        order_id = m2o_id(line_rows[0]["order_id"])
        order = odoo.search_read(
            "sale.order",
            [["id", "=", order_id]],
            ["name", "client_order_ref"],
            limit=1,
        )[0]

        order_name = order.get("name") or ""
        sku = product["default_code"]

        if order_name.startswith("#"):
            paid = shopify.order_is_paid_for_sku(order_name, sku)
        else:
            paid = odoo_order_paid(odoo, order_id)

        if not paid:
            continue

        code = (lot.get("x_gc_shopify_code") or normalize_code(lot.get("name") or "")).upper()
        if not CODE_RE.fullmatch(code):
            odoo.call(
                "stock.lot", "write", ids=[lot_id],
                vals={"x_gc_activation_error": "Invalid Shopify code: %s" % code},
            )
            continue

        note = "Odoo %s; SKU %s; card #%s; serial %s" % (
            order_name,
            sku,
            lot.get("x_gc_card_number") or "-",
            lot.get("name") or "",
        )

        try:
            gift_id = shopify.create_gift_card(code, REAL_SKUS[sku], note)
            odoo.call(
                "stock.lot", "write", ids=[lot_id],
                vals={
                    "x_gc_shopify_id": gift_id,
                    "x_gc_activated": True,
                    "x_gc_activated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                    "x_gc_activation_order": order_name,
                    "x_gc_activation_error": False,
                },
            )
            activated += 1
            log("ACTIVATED %s card #%s (%s) for %s" % (
                sku, lot.get("x_gc_card_number") or "-", lot.get("name"), order_name
            ))
        except Exception as exc:
            odoo.call(
                "stock.lot", "write", ids=[lot_id],
                vals={"x_gc_activation_error": str(exc)[:1500]},
            )
            log("Activation failed for %s/%s: %s" % (order_name, lot.get("name"), exc))

    return activated


def ensure_odoo_webhook(odoo):
    if not SERVICE_URL or not WEBHOOK_SECRET:
        raise RuntimeError("SERVICE_URL and WEBHOOK_SECRET are required")

    action_name = "[GC_PROD] Activate sold gift card"
    rule_name = "[GC_PROD] Gift card delivery webhook"
    target = SERVICE_URL + "/tick?token=" + WEBHOOK_SECRET

    existing_action = odoo.search_read(
        "ir.actions.server", [["name", "=", action_name]],
        ["id", "webhook_url"], limit=2,
    )
    if existing_action:
        action_id = existing_action[0]["id"]
        if existing_action[0].get("webhook_url") != target:
            odoo.call("ir.actions.server", "write", ids=[action_id], vals={"webhook_url": target})
    else:
        fields = odoo.search_read(
            "ir.model.fields",
            [["model", "=", "stock.picking"], ["name", "in", ["id", "state", "sale_id"]]],
            ["id"], limit=10,
        )
        action_id = odoo.call(
            "ir.actions.server", "create",
            vals_list={
                "name": action_name,
                "state": "webhook",
                "model_id": 458,
                "webhook_url": target,
                "webhook_field_ids": [(6, 0, [x["id"] for x in fields])],
            },
        )
        if isinstance(action_id, list):
            action_id = action_id[0]

    existing_rule = odoo.search_read(
        "base.automation", [["name", "=", rule_name]],
        ["id", "active", "action_server_ids"], limit=2,
    )
    domain = "[(\"state\", \"=\", \"done\"), (\"move_ids.product_id.default_code\", \"in\", %s)]" % list(REAL_SKUS)
    if existing_rule:
        rule_id = existing_rule[0]["id"]
        odoo.call(
            "base.automation", "write", ids=[rule_id],
            vals={"active": True, "filter_domain": domain, "action_server_ids": [(6, 0, [action_id])]},
        )
    else:
        state_field = odoo.search_read(
            "ir.model.fields",
            [["model", "=", "stock.picking"], ["name", "=", "state"]],
            ["id"], limit=1,
        )[0]["id"]
        odoo.call(
            "base.automation", "create",
            vals_list={
                "name": rule_name,
                "model_id": 458,
                "trigger": "on_write",
                "trigger_field_ids": [(6, 0, [state_field])],
                "filter_domain": domain,
                "active": True,
                "action_server_ids": [(6, 0, [action_id])],
            },
        )
    log("Odoo production webhook ready")


def production_setup():
    odoo = OdooClient()
    configure_products(odoo)
    import_cards(odoo)
    ensure_odoo_webhook(odoo)
    return odoo
