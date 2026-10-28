"""Deterministic Acme Shop seed (random seed 42). Idempotent: does nothing when customers already exist.

20k customers, 2k products, 200k orders, 600k order_items, 1M events spread over the last 90 days.
"""
import os
import random
import sys
import time
from datetime import datetime, timedelta, timezone

import psycopg

N_CUST, N_PROD, N_ORD, ITEMS_PER_ORDER, N_EVT = 20_000, 2_000, 200_000, 3, 1_000_000
DAYS = 90
COUNTRIES = ["US", "GB", "DE", "FR", "BD", "IN", "BR", "CA", "AU", "JP"]
CATEGORIES = ["books", "toys", "garden", "kitchen", "sports", "office", "audio", "apparel"]
STATUSES = ["delivered"] * 70 + ["shipped"] * 10 + ["paid"] * 8 + ["pending"] * 5 + ["cancelled"] * 5 + ["refunded"] * 2
EVENT_TYPES = ["page_view", "page_view", "page_view", "search", "add_to_cart", "checkout_start", "login"]


def main() -> int:
    rnd = random.Random(42)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    start = now - timedelta(days=DAYS)
    span = DAYS * 86400

    def ts(frac: float) -> datetime:
        return start + timedelta(seconds=int(frac * span))

    creds = {"password": os.environ["DEMO_SRC_PASSWORD"]}
    conn = psycopg.connect(host=os.environ.get("SRC_HOST", "source-db"), dbname="shop", user="shop_owner",
                           autocommit=True, **creds)
    for _ in range(30):
        try:
            conn.execute("select 1")
            break
        except Exception:
            time.sleep(1)
    with conn.cursor() as cur:
        cur.execute("select count(*) from customers")
        if cur.fetchone()[0] > 0:
            print("seed: already loaded, nothing to do")
            return 0
        t0 = time.time()
        with cur.copy("COPY customers FROM STDIN") as cp:
            for i in range(1, N_CUST + 1):
                c = ts(rnd.random())
                cp.write_row((i, f"user{i}@example.invalid", f"Customer {i}", rnd.choice(COUNTRIES), c, c))
        prices = {}
        with cur.copy("COPY products FROM STDIN") as cp:
            for i in range(1, N_PROD + 1):
                c = ts(rnd.random() * 0.2)
                prices[i] = round(rnd.uniform(3, 250), 2)
                cp.write_row((i, f"SKU-{i:05d}", f"Product {i}", rnd.choice(CATEGORIES), prices[i], c, c))
        order_rows, item_rows = [], []
        item_id = 0
        for oid in range(1, N_ORD + 1):
            created = ts(rnd.random())
            status = rnd.choice(STATUSES)
            updated = created if status == "pending" else min(now, created + timedelta(hours=rnd.randint(1, 120)))
            total = 0.0
            for _ in range(ITEMS_PER_ORDER):
                item_id += 1
                pid = rnd.randint(1, N_PROD)
                qty = rnd.randint(1, 4)
                total += qty * prices[pid]
                item_rows.append((item_id, oid, pid, qty, prices[pid], created, updated))
            order_rows.append((oid, rnd.randint(1, N_CUST), status, round(total, 2), created, updated))
        with cur.copy("COPY orders FROM STDIN") as cp:
            for r in order_rows:
                cp.write_row(r)
        with cur.copy("COPY order_items FROM STDIN") as cp:
            for r in item_rows:
                cp.write_row(r)
        with cur.copy("COPY events FROM STDIN") as cp:
            for i in range(1, N_EVT + 1):
                c = ts(rnd.random())
                cp.write_row((i, rnd.randint(1, N_CUST), rnd.choice(EVENT_TYPES),
                              f'{{"page": "/p/{rnd.randint(1, N_PROD)}", "ms": {rnd.randint(5, 900)}}}', c, c))
        cur.execute("analyze")
        print(f"seed: loaded {N_CUST} customers, {N_PROD} products, {N_ORD} orders, {N_ORD * ITEMS_PER_ORDER} items, "
              f"{N_EVT} events in {time.time() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
