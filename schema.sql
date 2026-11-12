-- Triper schema (SQLite, WAL)
-- No data is versioned here: the DB file itself is gitignored.
-- Apply with:  sqlite3 data/trips.db < schema.sql
-- or automatically on server start (server.py runs this file).

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------- accounts
CREATE TABLE IF NOT EXISTS users (
  id         TEXT PRIMARY KEY,
  email      TEXT NOT NULL UNIQUE,
  name       TEXT,
  pw_hash    TEXT NOT NULL,
  pw_salt    TEXT NOT NULL,
  created    REAL NOT NULL,
  last_login REAL
);

CREATE TABLE IF NOT EXISTS sessions (
  token   TEXT PRIMARY KEY,
  user    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created REAL NOT NULL,
  expires REAL NOT NULL,
  ua      TEXT
);

-- ---------------------------------------------------------------- trips
CREATE TABLE IF NOT EXISTS trips (
  id          TEXT PRIMARY KEY,
  title       TEXT NOT NULL,
  owner       TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  start_date  TEXT,
  end_date    TEXT,
  days        INTEGER NOT NULL DEFAULT 3,
  share_token TEXT UNIQUE,
  share_role  TEXT NOT NULL DEFAULT 'viewer',
  rev         INTEGER NOT NULL DEFAULT 0,
  created     REAL NOT NULL,
  updated     REAL NOT NULL
);

-- role: owner | editor | viewer   (owner is also stored here for uniformity)
CREATE TABLE IF NOT EXISTS members (
  trip    TEXT NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
  user    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  role    TEXT NOT NULL DEFAULT 'editor',
  created REAL NOT NULL,
  PRIMARY KEY (trip, user)
);

-- pending invites for emails that have not registered yet
CREATE TABLE IF NOT EXISTS invites (
  id      TEXT PRIMARY KEY,
  trip    TEXT NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
  email   TEXT NOT NULL,
  role    TEXT NOT NULL DEFAULT 'editor',
  created REAL NOT NULL,
  UNIQUE (trip, email)
);

-- ---------------------------------------------------------------- itinerary
CREATE TABLE IF NOT EXISTS items (
  trip     TEXT NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
  id       TEXT NOT NULL,
  day      TEXT NOT NULL,
  sort     REAL NOT NULL DEFAULT 0,
  time     TEXT DEFAULT '',
  title    TEXT NOT NULL,
  sub      TEXT DEFAULT '',
  kind     TEXT DEFAULT '',
  place_id TEXT,
  photo    TEXT,
  created  REAL NOT NULL,
  updated  REAL NOT NULL,
  PRIMARY KEY (trip, id)
);

-- checkbox state: '' | done | partial | skip
CREATE TABLE IF NOT EXISTS marks (
  trip    TEXT NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
  item    TEXT NOT NULL,
  status  TEXT NOT NULL DEFAULT '',
  updated REAL NOT NULL,
  PRIMARY KEY (trip, item)
);

-- map points (trip NULL = shared/global POI library)
CREATE TABLE IF NOT EXISTS pois (
  id        TEXT PRIMARY KEY,
  trip      TEXT REFERENCES trips(id) ON DELETE CASCADE,
  name      TEXT NOT NULL,
  name_en   TEXT,
  addr      TEXT,
  lat       REAL,
  lng       REAL,
  kind      TEXT,
  hours     TEXT,
  note      TEXT,
  group_key TEXT,
  updated   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS photos (
  id      TEXT PRIMARY KEY,
  trip    TEXT NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
  file    TEXT NOT NULL,
  caption TEXT,
  vision  TEXT,
  day     TEXT,
  item_id TEXT,
  created REAL NOT NULL
);

-- ---------------------------------------------------------------- indexes
CREATE INDEX IF NOT EXISTS ix_items_trip     ON items(trip, day, sort);
CREATE INDEX IF NOT EXISTS ix_marks_trip     ON marks(trip);
CREATE INDEX IF NOT EXISTS ix_pois_trip      ON pois(trip);
CREATE INDEX IF NOT EXISTS ix_photos_trip    ON photos(trip, created);
CREATE INDEX IF NOT EXISTS ix_members_user   ON members(user);
CREATE INDEX IF NOT EXISTS ix_members_trip   ON members(trip);
CREATE INDEX IF NOT EXISTS ix_sessions_user  ON sessions(user);
CREATE INDEX IF NOT EXISTS ix_trips_owner    ON trips(owner);
CREATE UNIQUE INDEX IF NOT EXISTS ux_trips_share ON trips(share_token);
