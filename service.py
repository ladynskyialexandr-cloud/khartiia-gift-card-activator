import threading
from flask import Flask, jsonify
from gift_card_worker import OdooClient, ShopifyClient, configure_products, import_cards, activate_pending_cards, log

app = Flask(__name__)
lock = threading.Lock()


def cycle(setup=False):
    if not lock.acquire(blocking=False):
        return 0
    try:
        odoo = OdooClient()
        shopify = ShopifyClient()
        if setup:
            configure_products(odoo)
            import_cards(odoo)
        return activate_pending_cards(odoo, shopify)
    finally:
        lock.release()


def bootstrap():
    try:
        activated = cycle(setup=True)
        log("Startup bootstrap complete; activated=%s" % activated)
    except Exception as exc:
        log("Startup bootstrap failed: %s" % exc)


threading.Thread(target=bootstrap, daemon=True).start()


@app.get("/")
def root():
    return jsonify({"ok": True, "service": "khartiia-gift-card-activator"})


@app.get("/health")
def health():
    return jsonify({"ok": True})


@app.get("/tick")
def tick():
    try:
        count = cycle(setup=True)
        return jsonify({"ok": True, "activated": count})
    except Exception as exc:
        log("tick failed: %s" % exc)
        return jsonify({"ok": False, "error": str(exc)[:1000]}), 500
