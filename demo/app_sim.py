"""Acme Shop traffic trickle: every 5 s insert orders/items/events and move some orders along their lifecycle."""
import os
import random
import time

import psycopg

NEXT = {"pending": "paid", "paid": "shipped", "shipped": "delivered"}


def tick(conn: psycopg.Connection, rnd: random.Random) -> None:
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("select coalesce(max(id), 0) from orders")
        oid = cur.fetchone()[0]
        cur.execute("select coalesce(max(id), 0) from order_items")
        iid = cur.fetchone()[0]
        cur.execute("select coalesce(max(id), 0) from events")
        eid = cur.fetchone()[0]
        for _ in range(rnd.randint(1, 3)):
            oid += 1
            total = 0.0
            cur.execute("insert into orders values (%s, %s, 'pending', 0, now(), now())", (oid, rnd.randint(1, 20000)))
            for _ in range(rnd.randint(1, 4)):
                iid += 1
                pid, qty, price = rnd.randint(1, 2000), rnd.randint(1, 3), round(rnd.uniform(3, 250), 2)
                total += qty * price
                cur.execute("insert into order_items values (%s, %s, %s, %s, %s, now(), now())",
                            (iid, oid, pid, qty, price))
            cur.execute("update orders set total_amount = %s where id = %s", (round(total, 2), oid))
        for _ in range(rnd.randint(3, 8)):
            eid += 1
            cur.execute("insert into events values (%s, %s, %s, %s, now(), now())",
                        (eid, rnd.randint(1, 20000), rnd.choice(["page_view", "search", "add_to_cart", "login"]),
                         f'{{"page": "/p/{rnd.randint(1, 2000)}"}}'))
        for status, nxt in NEXT.items():
            cur.execute(
                "update orders set status = %s, updated_at = now() where id in "
                "(select id from orders where status = %s and created_at > now() - interval '3 days' "
                "order by random() limit 2)", (nxt, status))


def main() -> None:
    rnd = random.Random()
    while True:
        try:
            creds = {"password": os.environ["DEMO_SRC_PASSWORD"]}
            with psycopg.connect(host=os.environ.get("SRC_HOST", "source-db"), dbname="shop", user="shop_owner",
                                 autocommit=True, **creds) as conn:
                while True:
                    tick(conn, rnd)
                    time.sleep(5)
        except Exception as ex:  # keep trickling through restarts / injected faults
            print("app-sim:", type(ex).__name__, str(ex)[:120], flush=True)
            time.sleep(5)


if __name__ == "__main__":
    main()
