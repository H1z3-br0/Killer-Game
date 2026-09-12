-- Журнал ошибок приложения для раздела «Обслуживание» и отметки служебных
-- задач планировщика.

CREATE TABLE error_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    path        TEXT    NOT NULL DEFAULT '',
    message     TEXT    NOT NULL,
    traceback   TEXT    NOT NULL DEFAULT '',
    created_at  TEXT    NOT NULL
);
CREATE INDEX idx_error_created ON error_log(created_at);

CREATE TABLE request_stat (
    day         TEXT    PRIMARY KEY,
    hits        INTEGER NOT NULL DEFAULT 0
);
