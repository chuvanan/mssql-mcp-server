-- Demo dataset for the MSSQL MCP server tutorial.
--
-- A small storefront: customers place orders, orders contain line items,
-- line items reference products. Small enough to read, rich enough to show
-- joins, aggregation, dates, money and NULLs.
--
-- Load with:
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
    id            INT IDENTITY(1,1) PRIMARY KEY,
    name          NVARCHAR(100) NOT NULL,
    email         NVARCHAR(200) NOT NULL,
    country       NVARCHAR(2)   NOT NULL,
    signed_up_at  DATE          NOT NULL,
    -- Deliberately nullable: shows how NULL surfaces through the tools.
    referred_by   NVARCHAR(100) NULL
);

CREATE TABLE products (
    id          INT IDENTITY(1,1) PRIMARY KEY,
    sku         NVARCHAR(20)   NOT NULL UNIQUE,
    name        NVARCHAR(100)  NOT NULL,
    category    NVARCHAR(50)   NOT NULL,
    unit_price  DECIMAL(10, 2) NOT NULL,
    discontinued BIT           NOT NULL DEFAULT 0
);

CREATE TABLE orders (
    id          INT IDENTITY(1,1) PRIMARY KEY,
    customer_id INT NOT NULL REFERENCES customers(id),
    placed_at   DATETIME2    NOT NULL,
    status      NVARCHAR(20) NOT NULL
);

CREATE TABLE order_items (
    id         INT IDENTITY(1,1) PRIMARY KEY,
    order_id   INT NOT NULL REFERENCES orders(id),
    product_id INT NOT NULL REFERENCES products(id),
    quantity   INT NOT NULL,
    unit_price DECIMAL(10, 2) NOT NULL   -- price captured at time of sale
);
GO

INSERT INTO customers (name, email, country, signed_up_at, referred_by) VALUES
    (N'Alice Nguyen',   N'alice@example.com',   N'VN', '2024-01-15', NULL),
    (N'Bao Tran',       N'bao@example.com',     N'VN', '2024-02-03', N'Alice Nguyen'),
    (N'Chidi Okafor',   N'chidi@example.com',   N'NG', '2024-02-20', NULL),
    (N'Dana Williams',  N'dana@example.com',    N'US', '2024-03-11', N'Alice Nguyen'),
    (N'Elif Demir',     N'elif@example.com',    N'TR', '2024-05-02', NULL),
    (N'Farid Rahman',   N'farid@example.com',   N'MY', '2024-06-18', N'Bao Tran'),
    (N'Grace Kim',      N'grace@example.com',   N'KR', '2024-08-27', NULL),
    (N'Hugo Martins',   N'hugo@example.com',    N'PT', '2025-01-09', N'Dana Williams');

INSERT INTO products (sku, name, category, unit_price, discontinued) VALUES
    (N'KB-001', N'Mechanical Keyboard',   N'Peripherals', 129.00, 0),
    (N'MS-002', N'Wireless Mouse',        N'Peripherals',  45.50, 0),
    (N'MN-003', N'27-inch Monitor',       N'Displays',    319.99, 0),
    (N'MN-004', N'34-inch Ultrawide',     N'Displays',    649.00, 0),
    (N'DK-005', N'Standing Desk',         N'Furniture',   540.00, 0),
    (N'CH-006', N'Ergonomic Chair',       N'Furniture',   410.75, 0),
    (N'HP-007', N'Noise-cancelling Headphones', N'Audio', 275.00, 0),
    (N'WC-008', N'1080p Webcam',          N'Peripherals',  89.99, 1),
    (N'CB-009', N'USB-C Hub',             N'Peripherals',  62.25, 0),
    (N'LP-010', N'Laptop Stand',          N'Furniture',    75.00, 0);

INSERT INTO orders (customer_id, placed_at, status) VALUES
    (1, '2024-03-02T10:15:00', N'shipped'),
    (1, '2024-07-19T14:02:00', N'shipped'),
    (2, '2024-04-11T09:30:00', N'shipped'),
    (2, '2025-02-14T16:45:00', N'processing'),
    (3, '2024-05-23T11:05:00', N'cancelled'),
    (4, '2024-06-01T08:20:00', N'shipped'),
    (4, '2024-11-30T19:55:00', N'shipped'),
    (5, '2024-09-08T13:40:00', N'shipped'),
    (6, '2024-10-17T07:10:00', N'processing'),
    (7, '2025-01-22T12:00:00', N'shipped'),
    (8, '2025-03-05T15:30:00', N'processing'),
    (1, '2025-04-01T09:00:00', N'processing');

INSERT INTO order_items (order_id, product_id, quantity, unit_price) VALUES
    (1,  1, 1, 129.00), (1,  2, 2,  45.50),
    (2,  3, 2, 319.99),
    (3,  5, 1, 540.00), (3,  6, 1, 410.75),
    (4,  7, 1, 275.00), (4,  9, 3,  62.25),
    (5,  4, 1, 649.00),
    (6,  1, 1, 129.00), (6,  3, 1, 319.99), (6, 10, 2, 75.00),
    (7,  6, 2, 410.75),
    (8,  4, 1, 649.00), (8,  2, 1,  45.50),
    (9,  8, 1,  89.99),
    (10, 7, 2, 275.00), (10, 9, 1,  62.25),
    (11, 5, 1, 540.00), (11, 10, 1, 75.00),
    (12, 3, 1, 319.99), (12, 2, 1,  45.50);
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

SELECT 'customers'   AS table_name, COUNT(*) AS rows FROM customers
UNION ALL SELECT 'products',   COUNT(*) FROM products
UNION ALL SELECT 'orders',     COUNT(*) FROM orders
UNION ALL SELECT 'order_items', COUNT(*) FROM order_items;
GO
