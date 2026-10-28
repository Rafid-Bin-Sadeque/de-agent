insert into analytics.x_summary
select c.country, count(*) from raw.customers c join raw.orders o on o.customer_id = c.id group by 1;
