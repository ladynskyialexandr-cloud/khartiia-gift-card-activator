from __future__ import annotations
from gift_card_worker import OdooClient, log

SRC = 5
DST = 100
PICKING_TYPE = 7

KEEP_IDS = {136,137,138,144,151,153,65}
# old/archived RC product -> correct IM product
REMAP = {
    206: 399,
    641: 640,
    643: 642,
    655: 654,
    657: 656,
    659: 658,
    661: 660,
}
SPECIAL_CASES = {226,231}


def _m2o(v):
    return v[0] if isinstance(v, (list, tuple)) and v else v


def _cancel_partial(odoo):
    rows = odoo.search_read("stock.picking", [["id","=",578],["state","not in",["done","cancel"]]], ["id","state"], limit=1)
    if rows:
        odoo.call("stock.picking","action_cancel",ids=[578])
        log("Cancelled partial accidental picking 578")


def _move_special_cases(odoo):
    p = odoo.search_read("stock.picking", [["id","=",373]], ["id","state","move_ids"], limit=1)
    if not p:
        return None
    if p[0]["state"] == "done":
        return 373
    # remove unrelated line from old draft, keep only the two Sila i Volia phone cases
    for mid in list(p[0].get("move_ids") or []):
        mv = odoo.search_read("stock.move", [["id","=",mid]], ["id","product_id","state"], limit=1)
        if mv and _m2o(mv[0]["product_id"]) not in SPECIAL_CASES and mv[0]["state"] == "draft":
            odoo.call("stock.move","unlink",ids=[mid])
    # confirm/assign and validate
    odoo.call("stock.picking","action_confirm",ids=[373])
    odoo.call("stock.picking","action_assign",ids=[373])
    moves = odoo.search_read("stock.move", [["picking_id","=",373],["state","not in",["done","cancel"]]], ["id","product_uom_qty"], limit=20)
    for m in moves:
        odoo.call("stock.move","write",ids=[m["id"]],vals={"quantity": float(m["product_uom_qty"] or 0), "picked": True})
    odoo.call("stock.picking","button_validate",ids=[373])
    log("Validated special-case picking 373")
    return 373


def _current_rc(odoo):
    qs = odoo.search_read("stock.quant", [["location_id","=",SRC],["quantity",">",0]], ["id","product_id","quantity","reserved_quantity"], limit=2000)
    agg = {}
    for q in qs:
        pid = _m2o(q["product_id"])
        rec = agg.setdefault(pid, {"qty":0.0,"reserved":0.0})
        rec["qty"] += float(q.get("quantity") or 0)
        rec["reserved"] += float(q.get("reserved_quantity") or 0)
    return agg


def _create_main_transfer(odoo, agg):
    normal = []
    for pid, vals in agg.items():
        if pid in KEEP_IDS or pid in REMAP or pid in SPECIAL_CASES:
            continue
        normal.append((pid, vals["qty"]))
    if not normal:
        return None
    picking_id = odoo.call("stock.picking","create",vals_list={
        "picking_type_id": PICKING_TYPE,
        "location_id": SRC,
        "location_dest_id": DST,
        "origin": "RC→IM cleanup 2026-09-26",
    })
    if isinstance(picking_id, list):
        picking_id = picking_id[0]
    products = odoo.search_read("product.product", [["id","in",[x[0] for x in normal]]], ["id","display_name"], limit=1000)
    names = {x["id"]: x["display_name"] for x in products}
    for pid, qty in normal:
        odoo.call("stock.move","create",vals_list={
            "name": names.get(pid, str(pid)),
            "product_id": pid,
            "product_uom_qty": qty,
            "location_id": SRC,
            "location_dest_id": DST,
            "picking_id": picking_id,
        })
    odoo.call("stock.picking","action_confirm",ids=[picking_id])
    odoo.call("stock.picking","action_assign",ids=[picking_id])
    moves = odoo.search_read("stock.move", [["picking_id","=",picking_id],["state","not in",["done","cancel"]]], ["id","product_uom_qty","quantity"], limit=1000)
    short = [m for m in moves if float(m.get("quantity") or 0) + 1e-9 < float(m.get("product_uom_qty") or 0)]
    if short:
        raise RuntimeError("Not all quantities reserved for main transfer: %s" % short[:10])
    for m in moves:
        odoo.call("stock.move","write",ids=[m["id"]],vals={"quantity": float(m["product_uom_qty"] or 0), "picked": True})
    result = odoo.call("stock.picking","button_validate",ids=[picking_id])
    log("Validated main RC→IM picking %s" % picking_id)
    return picking_id


def _set_quant_inventory(odoo, product_id, location_id, new_qty):
    qs = odoo.search_read("stock.quant", [["product_id","=",product_id],["location_id","=",location_id],["lot_id","=",False],["package_id","=",False],["owner_id","=",False]], ["id","quantity"], limit=5)
    if qs:
        qid = qs[0]["id"]
        odoo.call("stock.quant","write",ids=[qid],vals={"inventory_quantity": new_qty})
    else:
        qid = odoo.call("stock.quant","create",vals_list={"product_id":product_id,"location_id":location_id,"inventory_quantity":new_qty})
        if isinstance(qid, list):
            qid = qid[0]
    odoo.call("stock.quant","action_apply_inventory",ids=[qid])


def _reclass_legacy(odoo):
    done = []
    for old_id, new_id in REMAP.items():
        src = odoo.search_read("stock.quant", [["product_id","=",old_id],["location_id","=",SRC],["quantity",">",0]], ["id","quantity"], limit=20)
        qty = sum(float(x.get("quantity") or 0) for x in src)
        if qty <= 0:
            continue
        # zero old product at RC
        for q in src:
            odoo.call("stock.quant","write",ids=[q["id"]],vals={"inventory_quantity":0.0})
            odoo.call("stock.quant","action_apply_inventory",ids=[q["id"]])
        # add to correct IM article
        dst = odoo.search_read("stock.quant", [["product_id","=",new_id],["location_id","=",DST],["lot_id","=",False],["package_id","=",False],["owner_id","=",False]], ["id","quantity"], limit=5)
        current = sum(float(x.get("quantity") or 0) for x in dst)
        _set_quant_inventory(odoo,new_id,DST,current+qty)
        done.append({"from":old_id,"to":new_id,"qty":qty})
        log("Reclassified old product %s -> %s qty=%s" % (old_id,new_id,qty))
    return done


def execute():
    odoo = OdooClient()
    _cancel_partial(odoo)
    _move_special_cases(odoo)
    agg = _current_rc(odoo)
    main = _create_main_transfer(odoo, agg)
    remapped = _reclass_legacy(odoo)
    final = _current_rc(odoo)
    return {"main_picking":main,"special_picking":373,"remapped":remapped,"remaining_rc":final}
