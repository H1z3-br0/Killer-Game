# Папка deploy

Файлы для запуска сервиса на сервере. В самом приложении они не участвуют —
это то, что вы копируете в систему один раз при развёртывании.

| Файл | Для чего |
|---|---|
| `killer.service` | Юнит **systemd** (Linux): держит сервис поднятым и перезапускает после сбоя |
| `com.killer.plist` | То же для **launchd** (macOS) |
| `nginx-killer.conf` | Конфиг **nginx**: адрес без порта, статика, заголовки безопасности |

## Linux

```bash
sudo cp deploy/killer.service /etc/systemd/system/
sudo systemctl enable --now killer
sudo systemctl status killer

sudo cp deploy/nginx-killer.conf /etc/nginx/sites-available/killer
sudo ln -s /etc/nginx/sites-available/killer /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

В юните и в конфиге прописан путь `/var/www/Killer-Game` и пользователь `aesc` —
подставьте свои, если размещение другое. В конфиге nginx поправьте `server_name`
на адрес сервера.

## Права на файлы

Каталог проекта целиком должен принадлежать тому пользователю, от имени
которого работает сервис:

```bash
sudo chown -R aesc:aesc /var/www/Killer-Game
```

Это же лечит `dubious ownership` и `cannot open '.git/FETCH_HEAD'` у git:
такое бывает, когда репозиторий склонировали под `sudo`. По той же причине
не стоит делать `sudo git pull` — root снова создаст файлы, которые обычный
пользователь не перепишет.

Nginx читает только `app/static/`, ему достаточно прав на чтение и на проход
по каталогам выше. Каталог `data/` должен быть доступен на запись сервису —
там живут база и бэкапы.

## macOS

```bash
cp deploy/com.killer.plist ~/Library/LaunchAgents/     # путь внутри — заменить на свой
launchctl load -w ~/Library/LaunchAgents/com.killer.plist

brew install nginx
cp deploy/nginx-killer.conf /opt/homebrew/etc/nginx/servers/killer.conf
sudo nginx -t && sudo nginx -s reload                  # порт 80 требует sudo
```

В конфиге поправьте `alias` для `/static/` — на macOS это путь вида
`/Users/<вы>/aesc_services/Assassin/app/static/`.

## Без nginx тоже можно

Сервис самодостаточен: `./run.sh` поднимает его на `0.0.0.0:8000`, и с телефонов
он открывается по `http://<адрес>:8000`. Nginx нужен, если хочется адрес без
порта, кеш статики и заголовки безопасности.

## Что важно не забыть

Если nginx стоит, сервис обязан запускаться с `--proxy-headers
--forwarded-allow-ips 127.0.0.1` (в готовых файлах это уже так). Иначе все
запросы придут к нему с адреса `127.0.0.1`, и сервис посчитает всех игроков
одним человеком: три неверных пароля от кого угодно — и вход подвиснет для
всего офиса.
