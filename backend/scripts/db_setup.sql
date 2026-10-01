-- ---------------------------------------------------------------------------
-- Niche Podcast Clipper - one-time database bootstrap
-- Run:  sudo -u postgres psql -f scripts/db_setup.sql
-- (or paste the statements into a psql session as a superuser)
-- ---------------------------------------------------------------------------

-- 1. Role + password matching backend/.env (DATABASE_URL).
ALTER USER postgres WITH PASSWORD 'postgres';

-- 2. Application database.
SELECT 'CREATE DATABASE podcast_clipper OWNER postgres'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'podcast_clipper')\gexec

-- 3. Optional: dedicated least-privilege role instead of using `postgres`.
--    If you use it, change DATABASE_URL to:
--    postgresql://podcast_clipper:podcast_clipper@localhost:5432/podcast_clipper
-- CREATE ROLE podcast_clipper LOGIN PASSWORD 'podcast_clipper';
-- GRANT ALL PRIVILEGES ON DATABASE podcast_clipper TO podcast_clipper;
