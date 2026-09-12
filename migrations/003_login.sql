-- Логин как имя для входа. До этого логином служило ФИО, из-за чего тёзки
-- конфликтовали на ровном месте; теперь ФИО — просто отображаемое имя.

ALTER TABLE user ADD COLUMN login TEXT NOT NULL DEFAULT '';

-- Существующим записям выдаём логин из транслита; при пустой базе не делает ничего.
UPDATE user SET login = 'user' || id WHERE login = '';

CREATE UNIQUE INDEX idx_user_login ON user(login);
