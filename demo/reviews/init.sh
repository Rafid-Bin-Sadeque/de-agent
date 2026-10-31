#!/bin/bash
# Demo reviews database (fictional): table `reviews` with 50 rows and a read-only `monitor` user.
set -euo pipefail
mysql -uroot -p"${MYSQL_ROOT_PASSWORD}" <<SQL
CREATE TABLE reviews.reviews (
  id         INT AUTO_INCREMENT PRIMARY KEY,
  order_id   INT NOT NULL,
  rating     TINYINT NOT NULL,
  body       VARCHAR(255) NOT NULL,
  created_at DATETIME NOT NULL
);
INSERT INTO reviews.reviews (order_id, rating, body, created_at)
WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 50)
SELECT 1000 + i * 7,
       1 + (i * 3) % 5,
       ELT(1 + i % 5, 'Arrived on time, nicely packed.', 'Colour differs from the photo.', 'Great value for the price.',
           'Box was dented but the item is fine.', 'Would order again.'),
       TIMESTAMP('2026-09-01 08:00:00') + INTERVAL (i * 7) HOUR
FROM n;
CREATE USER 'monitor'@'%' IDENTIFIED BY '${DEMO_REVIEWS_PASSWORD}';
GRANT SELECT ON reviews.* TO 'monitor'@'%';
GRANT PROCESS, REPLICATION CLIENT ON *.* TO 'monitor'@'%';
GRANT SELECT ON performance_schema.* TO 'monitor'@'%';
GRANT SELECT, EXECUTE ON sys.* TO 'monitor'@'%';
SQL
