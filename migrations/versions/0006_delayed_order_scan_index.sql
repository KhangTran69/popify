CREATE INDEX ix_orders_delay_scan
    ON orders (fulfillment_status, created_at, id);
