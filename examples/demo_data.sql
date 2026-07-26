-- Demo dataset for the MSSQL MCP server tutorial.
--
-- A storefront at a useful scale: ~122,000 rows across four tables.
--
--   customers        2,000
--   products           200
--   orders          30,000   spanning 2023-01-01 .. 2024-12-31
--   order_items     90,000   1-5 lines per order
--
-- Generation is set-based (no 100k INSERT statements) and fully
-- DETERMINISTIC: every value derives from the row number via modular
-- arithmetic with prime multipliers, not RAND() or NEWID(). Reload it a
-- hundred times and you get byte-identical data -- which is what lets
-- TUTORIAL.md quote exact query results.
--
-- Loads in a few seconds. Run with:
--   docker compose exec -T mssql /opt/mssql-tools18/bin/sqlcmd \
--     -C -S localhost -U sa -P 'StrongPassword123!' -i /dev/stdin < examples/demo_data.sql

IF DB_ID('storefront') IS NULL
    CREATE DATABASE storefront;
GO

USE storefront;
GO

DROP TABLE IF EXISTS order_items;
DROP TABLE IF EXISTS orders;
DROP TABLE IF EXISTS products;
DROP TABLE IF EXISTS customers;
GO

CREATE TABLE customers (
    id            INT PRIMARY KEY,
    name          NVARCHAR(100) NOT NULL,
    email         NVARCHAR(200) NOT NULL,
    country       NVARCHAR(2)   NOT NULL,
    signed_up_at  DATE          NOT NULL,
    -- Deliberately nullable: roughly a third are NULL, so the tools have
    -- something real to report as null rather than empty string.
    referred_by   INT           NULL REFERENCES customers(id)
);

CREATE TABLE products (
    id           INT PRIMARY KEY,
    sku          NVARCHAR(20)   NOT NULL UNIQUE,
    name         NVARCHAR(100)  NOT NULL,
    category     NVARCHAR(50)   NOT NULL,
    unit_price   DECIMAL(10, 2) NOT NULL,
    discontinued BIT            NOT NULL DEFAULT 0
);

CREATE TABLE orders (
    id          INT PRIMARY KEY,
    customer_id INT          NOT NULL REFERENCES customers(id),
    placed_at   DATETIME2    NOT NULL,
    status      NVARCHAR(20) NOT NULL
);

CREATE TABLE order_items (
    id         INT PRIMARY KEY,
    order_id   INT NOT NULL REFERENCES orders(id),
    product_id INT NOT NULL REFERENCES products(id),
    quantity   INT NOT NULL,
    unit_price DECIMAL(10, 2) NOT NULL   -- price captured at time of sale
);
GO

-- A reusable numbers source. sys.all_objects cross-joined with itself yields
-- millions of rows and works on every supported SQL Server version, unlike
-- GENERATE_SERIES which needs compatibility level 160.
CREATE OR ALTER VIEW numbers AS
    SELECT TOP (200000) ROW_NUMBER() OVER (ORDER BY (SELECT NULL)) AS i
    FROM sys.all_objects a CROSS JOIN sys.all_objects b;
GO

-- ---------------------------------------------------------------- customers
INSERT INTO customers (id, name, email, country, signed_up_at, referred_by)
SELECT
    i,
    CONCAT(
        CHOOSE((i * 13) % 12 + 1, N'Alice', N'Bao', N'Chidi', N'Dana', N'Elif',
               N'Farid', N'Grace', N'Hugo', N'Ines', N'Jae', N'Kenji', N'Lena'),
        N' ',
        CHOOSE((i * 29) % 10 + 1, N'Nguyen', N'Tran', N'Okafor', N'Williams',
               N'Demir', N'Rahman', N'Kim', N'Martins', N'Silva', N'Haddad')),
    CONCAT(N'user', i, N'@example.com'),
    -- Weighted rather than uniform. Ten equal buckets would make every
    -- "which country leads?" query a tie, which teaches nothing.
    CASE
        WHEN (i * 7) % 100 < 22 THEN N'US'
        WHEN (i * 7) % 100 < 40 THEN N'VN'
        WHEN (i * 7) % 100 < 52 THEN N'DE'
        WHEN (i * 7) % 100 < 63 THEN N'IN'
        WHEN (i * 7) % 100 < 72 THEN N'BR'
        WHEN (i * 7) % 100 < 80 THEN N'KR'
        WHEN (i * 7) % 100 < 87 THEN N'TR'
        WHEN (i * 7) % 100 < 93 THEN N'MY'
        WHEN (i * 7) % 100 < 97 THEN N'PT'
        ELSE N'NG'
    END,
    DATEADD(day, (i * 4547) % 730, '2023-01-01'),
    -- Roughly two thirds were referred, always by a lower-numbered customer.
    -- (i * k) % i is identically zero, so the modulus must be i - 1.
    CASE WHEN i % 3 = 0 OR i = 1 THEN NULL
         ELSE (i * 2749) % (i - 1) + 1 END
FROM numbers
WHERE i <= 2000;
GO

-- ----------------------------------------------------------------- products
INSERT INTO products (id, sku, name, category, unit_price, discontinued)
SELECT
    i,
    CONCAT(
        CHOOSE((i - 1) / 40 + 1, N'PR', N'DP', N'FN', N'AU', N'NW'),
        N'-', FORMAT(i, N'0000')),
    CONCAT(
        CHOOSE((i * 11) % 8 + 1, N'Compact', N'Pro', N'Ultra', N'Studio',
               N'Portable', N'Wireless', N'Ergonomic', N'Premium'),
        N' ',
        -- The noun is drawn from the product's own category, so there are no
        -- "Ergonomic Chair" rows filed under Peripherals.
        CASE (i - 1) / 40
            WHEN 0 THEN CHOOSE((i * 17) % 5 + 1, N'Keyboard', N'Mouse',
                               N'Webcam', N'Hub', N'Trackpad')
            WHEN 1 THEN CHOOSE((i * 17) % 4 + 1, N'Monitor', N'Ultrawide',
                               N'Display', N'Projector')
            WHEN 2 THEN CHOOSE((i * 17) % 4 + 1, N'Desk', N'Chair',
                               N'Stand', N'Footrest')
            WHEN 3 THEN CHOOSE((i * 17) % 4 + 1, N'Headphones', N'Speakers',
                               N'Microphone', N'Earbuds')
            ELSE        CHOOSE((i * 17) % 4 + 1, N'Router', N'Switch',
                               N'Access Point', N'Dock')
        END),
    -- 40 products per category, so category is a clean function of id.
    CHOOSE((i - 1) / 40 + 1, N'Peripherals', N'Displays', N'Furniture',
                             N'Audio', N'Networking'),
    CAST(19.99 + ((i * 3571) % 65000) / 100.0 AS DECIMAL(10, 2)),
    CASE WHEN i % 17 = 0 THEN 1 ELSE 0 END   -- ~6% discontinued
FROM numbers
WHERE i <= 200;
GO

-- ------------------------------------------------------------------- orders
INSERT INTO orders (id, customer_id, placed_at, status)
SELECT
    i,
    -- Squaring a uniform draw concentrates orders on low customer ids, so a
    -- few accounts are heavy buyers and the tail orders once. Uniform
    -- customer assignment would give every customer the same basket.
    ((i * 7919) % 2000) * ((i * 7919) % 2000) / 2000 + 1,
    -- 40% of orders in 2023, 60% in 2024: a visible growth trend for
    -- time-series questions.
    DATEADD(minute,
        CASE WHEN (i * 97) % 10 < 4
             THEN (i * 5171) % 525600
             ELSE 525600 + (i * 5171) % 525600
        END, '2023-01-01'),
    -- Weighted: ~70% shipped, 15% processing, 10% delivered, 5% cancelled.
    CASE
        WHEN (i * 31) % 100 < 70 THEN N'shipped'
        WHEN (i * 31) % 100 < 85 THEN N'processing'
        WHEN (i * 31) % 100 < 95 THEN N'delivered'
        ELSE N'cancelled'
    END
FROM numbers
WHERE i <= 30000;
GO

-- -------------------------------------------------------------- order_items
-- 1-5 lines per order, keyed off the order id, giving exactly 90,000 rows.
INSERT INTO order_items (id, order_id, product_id, quantity, unit_price)
SELECT
    ROW_NUMBER() OVER (ORDER BY o.id, n.i),
    o.id,
    p.id,
    -- Skewed toward single units, as real baskets are.
    CASE WHEN (o.id + n.i) % 10 < 6 THEN 1
         WHEN (o.id + n.i) % 10 < 9 THEN 2
         ELSE 3 END,
    -- Mostly list price; every seventh line is discounted 10%, so "price paid
    -- differs from current price" is a real query with real answers.
    CASE WHEN (o.id + n.i) % 7 = 0
         THEN CAST(p.unit_price * 0.90 AS DECIMAL(10, 2))
         ELSE p.unit_price END
FROM orders o
JOIN numbers n ON n.i <= o.id % 5 + 1
-- Squared again, so some products are bestsellers rather than every product
-- selling identical volume.
JOIN products p ON p.id = ((o.id * 6151 + n.i * 3571) % 200)
                        * ((o.id * 6151 + n.i * 3571) % 200) / 200 + 1;
GO

-- Indexes you would actually want at this size; they also keep the tutorial's
-- aggregate queries fast enough to feel interactive.
CREATE INDEX ix_orders_customer  ON orders(customer_id);
CREATE INDEX ix_orders_placed_at ON orders(placed_at) INCLUDE (status);
CREATE INDEX ix_items_order      ON order_items(order_id);
CREATE INDEX ix_items_product    ON order_items(product_id);
GO

-- A least-privilege login for the MCP server. This is the control that
-- actually protects the database; see SECURITY.md.
IF NOT EXISTS (SELECT 1 FROM sys.server_principals WHERE name = 'mcp_demo')
    CREATE LOGIN mcp_demo WITH PASSWORD = 'DemoPassword123!';
GO

USE storefront;
GO

IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name = 'mcp_demo')
    CREATE USER mcp_demo FOR LOGIN mcp_demo;
GO

-- Reader plus writer, so the write-approval walkthrough in TUTORIAL.md can
-- actually execute an approved statement. The final section of the tutorial
-- shows how to drop db_datawriter for a genuinely read-only deployment --
-- which is the control that actually protects the database, not the
-- classifier. See SECURITY.md.
ALTER ROLE db_datareader ADD MEMBER mcp_demo;
ALTER ROLE db_datawriter ADD MEMBER mcp_demo;
GO

SELECT 'customers' AS table_name, COUNT(*) AS [rows] FROM customers
UNION ALL SELECT 'products',    COUNT(*) FROM products
UNION ALL SELECT 'orders',      COUNT(*) FROM orders
UNION ALL SELECT 'order_items', COUNT(*) FROM order_items
UNION ALL SELECT 'TOTAL', (SELECT COUNT(*) FROM customers) + (SELECT COUNT(*) FROM products)
                        + (SELECT COUNT(*) FROM orders) + (SELECT COUNT(*) FROM order_items);
GO
