# Папка deploy

Файлы для запуска сервиса на сервере. В самом приложении они не участвуют —
это то, что вы копируете в систему один раз при развёртывании.

| Файл | Для чего |
|---|---|
| `killer.service` | Юнит **systemd** (Linux): держит сервис поднятым и перезапускает после сбоя |
| `com.killer.plist` | То же для **launchd** (macOS) |
| `nginx-killer.conf` | Конфиг **nginx**: адрес без порта, статика, заголовки безопасности |

## Что нужно на сервере

На чистой Ubuntu/Debian не хватает двух системных пакетов: `venv` там идёт
отдельно от Python, а `make` не ставится по умолчанию.

```bash
sudo apt update
sudo apt install -y python3-venv python3-pip make
```

Если `apt` ругается на `python3-venv`, поставьте версию под свой Python —
например `python3.12-venv`. Проверить версию: `python3 -V`.

Дальше — окружение и первый запуск:

```bash
cd /var/www/Killer-Game
rm -rf .venv            # если venv уже пытался создаться и не смог
make install
./run.sh                # Ctrl+C после проверки, дальше запускает systemd
```

`make` — только ради удобства. Без него то же самое:

```bash
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1
```

Если `pip install` попробует что-то собирать из исходников (бывает на редких
архитектурах), доставьте `sudo apt install -y build-essential libffi-dev` —
обычно на x86_64 это не нужно, всё ставится готовыми пакетами.

## Если сервер не достучится до PyPI

Сначала проверьте, в чём дело:

```bash
curl -4 -sS -o /dev/null -w "IPv4: %{http_code}\n" https://pypi.org/simple/
curl -6 -sS -o /dev/null -w "IPv6: %{http_code}\n" https://pypi.org/simple/
env | grep -i proxy
```

- **IPv4 отвечает, IPv6 висит** — pip уходит по IPv6, которого нет. Дайте системе
  предпочитать IPv4: раскомментируйте в `/etc/gai.conf` строку
  `precedence ::ffff:0:0/96  100`, либо поднимите таймауты:
  `pip install --timeout 60 --retries 10 -r requirements.txt`.
- **Задан прокси** — передайте его pip: `pip install --proxy http://адрес:порт ...`
  или пропишите в `/etc/pip.conf`.
- **Не отвечает ничего** — переносим пакеты руками, см. ниже.

### Установка без интернета на сервере

На машине с интернетом (подойдёт и macOS — платформа задаётся флагами):

```bash
pip download --dest wheels --platform manylinux_2_17_x86_64 \
    --python-version 3.12 --only-binary=:all: -r requirements.txt
scp -r wheels aesc@srv:/var/www/Killer-Game/
```

Набор получается около 20 МБ, 26 файлов. На сервере:

```bash
python3 -m venv .venv
.venv/bin/pip install --no-index --find-links=wheels -r requirements.txt
```

Флаг `--no-index` запрещает pip ходить в сеть вообще: ставится только то, что
лежит в каталоге. Каталог `wheels/` в репозиторий не кладём — он в `.gitignore`.

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
