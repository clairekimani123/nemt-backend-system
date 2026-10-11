-- Runs once, as superuser, when the Postgres container is first created.
-- Principle: the app connects with a role that CANNOT bypass Row-Level Security.
CREATE ROLE nemt_owner LOGIN PASSWORD 'owner_dev_pw';
CREATE ROLE nemt_app   LOGIN PASSWORD 'app_dev_pw' NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
ALTER DATABASE nemt OWNER TO nemt_owner;
CREATE EXTENSION IF NOT EXISTS postgis;   -- needs superuser; used from the Trips/Tracking phases
GRANT CONNECT ON DATABASE nemt TO nemt_app;
GRANT USAGE ON SCHEMA public TO nemt_app;

-- Separate database for the test-suite: pytest TRUNCATEs tables, so it must never
-- share a database with real development data.
CREATE DATABASE nemt_test OWNER nemt_owner;
GRANT CONNECT ON DATABASE nemt_test TO nemt_app;
\connect nemt_test
CREATE EXTENSION IF NOT EXISTS postgis;
GRANT USAGE ON SCHEMA public TO nemt_app;
