# Khartiia Gift Card Activator

Cloud worker for physical gift cards sold from Odoo and activated in Shopify.

## What it does
- Configures GIFT-500 / 1000 / 2000 / 3000 / 5000 as stockable serial-tracked products.
- Imports 250 physical cards from cards.csv into Odoo as serial numbers.
- Stores card number (#1..#250) and normalized Shopify code on each Odoo serial.
- Adds initial stock only when a serial has no existing internal stock.
- Scans completed customer deliveries.
- Verifies payment (Shopify PAID for Shopify orders; fully-paid posted Odoo invoice as fallback).
- Calls Shopify giftCardCreate with the exact preprinted code and nominal value.
- Writes Shopify Gift Card ID and activation status back to Odoo.
- Does not intentionally activate the same serial twice.

## Required Render environment variables

Secrets:
- SHOPIFY_CLIENT_ID
- SHOPIFY_CLIENT_SECRET
- ODOO_API_KEY

Configuration:
- SHOPIFY_SHOP_DOMAIN=89di0g-mx.myshopify.com
- SHOPIFY_API_VERSION=2026-07
- ODOO_URL=https://<your-odoo-host>
- ODOO_DB=<database-name> (optional when host uniquely identifies DB)
- INITIAL_STOCK_LOCATION=IM/Основний

## Shopify scopes
- read_orders
- read_gift_cards
- write_gift_cards

## Render Cron Job
Build: pip install -r requirements.txt
Start: python gift_card_worker.py --once
Schedule: * * * * *

No secrets are stored in this repository.
