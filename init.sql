-- init.sql: Docker entrypoint bootstrap for MySQL.
--
-- Runs once, on first container start. The application user itself is created by
-- the MySQL entrypoint from MYSQL_USER / MYSQL_PASSWORD; this script only creates
-- the two databases and grants the application user access to the data warehouse
-- (the entrypoint grants the metadata database automatically).

CREATE DATABASE IF NOT EXISTS `opendata` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE DATABASE IF NOT EXISTS `opendata_data` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

-- IMPORTANT (Production): replace '%' with 'localhost' or a specific IP to restrict
-- remote access:
--   GRANT ALL PRIVILEGES ON `opendata_data`.* TO 'opendata_user'@'localhost';
-- Docker's default bridge network requires '%' for cross-container connectivity.
GRANT ALL PRIVILEGES ON `opendata_data`.* TO 'opendata_user'@'%';
FLUSH PRIVILEGES;
