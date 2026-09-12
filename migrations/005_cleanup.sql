-- Отделы убраны: людей различает логин, а не место работы.
ALTER TABLE user DROP COLUMN department;
ALTER TABLE user DROP COLUMN qualifier;
