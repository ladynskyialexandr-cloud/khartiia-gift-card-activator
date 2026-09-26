import os
import threading

from flask import Flask, jsonify, request

from gift_card_worker import OdooClient, ShopifyClient, log
from production_worker import activate_pending_real_cards, production_setup

app = Flask(__name__)
lock = threading.Lock()
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")


def cycle(setup=False):
    if not lock.acquire(blocking=False):
        return 0
    try:
        odoo = production_setup() if setup else OdooClient()
        shopify = ShopifyClient()
        return activate_pending_real_cards(odoo, shopify)
    finally:
        lock.release()


def bootstrap():
    try:
        activated = cycle(setup=True)
        log("Production bootstrap complete; activated=%s" % activated)
    except Exception as exc:
        log("Production bootstrap failed: %s" % exc)


threading.Thread(target=bootstrap, daemon=True).start()


@app.get("/")
def root():
    return jsonify({"ok": True, "service": "khartiia-gift-card-activator"})


@app.get("/health")
def health():
    return jsonify({"ok": True})


@app.route("/tick", methods=["GET", "POST"])
def tick():
    if not WEBHOOK_SECRET or request.args.get("token") != WEBHOOK_SECRET:
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    try:
        count = cycle(setup=False)
        return jsonify({"ok": True, "activated": count})
    except Exception as exc:
        log("tick failed: %s" % exc)
        return jsonify({"ok": False, "error": str(exc)[:1000]}), 500


@app.route("/migrate-rc-im", methods=["GET","POST"])
def migrate_rc_im():
    if not WEBHOOK_SECRET or request.args.get("token") != WEBHOOK_SECRET:
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    try:
        from rc_im_migration import execute
        result = execute()
        return jsonify({"ok": True, "result": result})
    except Exception as exc:
        log("RC→IM migration failed: %s" % exc)
        return jsonify({"ok": False, "error": str(exc)[:1500]}), 500
