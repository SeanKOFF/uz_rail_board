-- Канонический справочник расписания.
-- Одна строка = один рейс в одном направлении относительно Ташкента.

CREATE TABLE IF NOT EXISTS trains (
    id            INTEGER PRIMARY KEY,
    number        TEXT NOT NULL,          -- '762Ф'
    direction     TEXT NOT NULL,          -- 'departure' | 'arrival'
    title         TEXT NOT NULL,          -- 'Самарканд — Бухара' / 'из Бухары'
    brand         TEXT,                   -- 'Афросиёб' | 'Шарк' | NULL
    fast          INTEGER NOT NULL DEFAULT 0,
    time_local    TEXT NOT NULL,          -- 'HH:MM' по Ташкенту
    days          TEXT NOT NULL DEFAULT '1234567',  -- дни курсирования, 1=Пн
    via           TEXT,
    station       TEXT NOT NULL DEFAULT 'tashkent-pass',
    through       INTEGER NOT NULL DEFAULT 0,
    cities        TEXT NOT NULL DEFAULT '[]',
    quals         TEXT NOT NULL DEFAULT '[]',
    active        INTEGER NOT NULL DEFAULT 1,
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL,
    UNIQUE (number, direction, station)
);

-- Журнал сверок: что именно изменилось на источнике и когда.
CREATE TABLE IF NOT EXISTS changelog (
    id         INTEGER PRIMARY KEY,
    checked_at TEXT NOT NULL,
    kind       TEXT NOT NULL,   -- 'added' | 'removed' | 'changed'
    number     TEXT NOT NULL,
    direction  TEXT NOT NULL,
    detail     TEXT
);

-- Журнал самих запусков — чтобы видеть, что задача жива.
CREATE TABLE IF NOT EXISTS runs (
    id         INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL,
    status     TEXT NOT NULL,   -- 'ok' | 'aborted' | 'error'
    scraped    INTEGER,
    note       TEXT
);

CREATE INDEX IF NOT EXISTS idx_trains_dir ON trains (direction, active);
