# NAPI Board Config v17

Один Python-пакет с общим Core для curses TUI и неинтерактивного CLI.
Python 3.9+, PyYAML; web-зависимостей и сервера нет.

## Запуск из каталога проекта

```bash
./napi-config tui
python3 napi-board-config.py --db ./boards.yaml
python3 -m napi_config --help
```

Для установки команды `napi-config` в Python-окружение:

```bash
python3 -m pip install .
napi-config --help
```

Повышенные привилегии нужны только для доступа к устройствам и boot-файлам.
При установке YAML помещаются в `share/napi-config` Python-окружения;
`--db` и `--platforms` позволяют указать рабочие файлы явно.

## CLI

```bash
napi-config eeprom show --json
napi-config board info --json
napi-config overlay list --json
napi-config hardware detect --json
napi-config boot show
napi-config boot preview --json
napi-config mac generate --yes --output instance.json --json
napi-config mac generate --profile 6 1 --yes --output instance.json --json
napi-config board info --config instance.json
napi-config overlay list --config instance.json
napi-config boot preview --config instance.json --json
napi-config boot write --config instance.json --yes
napi-config eeprom write --config instance.json --yes
```

Без установки используйте `./napi-config` или `python3 -m napi_config`.
`eeprom show`, `board info` и `overlay list` по умолчанию читают EEPROM.
Для `board info` и `overlay list` можно указать `--config` или
`--profile ID REV --yes`. Генерация без источника использует новую конфигурацию
по умолчанию; чтобы сохранить текущие serial/date, задайте `--config`.
Без `--output` результат генерации выводится в stdout и в файлы не записывается.
Генерация MAC никогда сама не записывает EEPROM.

Команды принимают `--eeprom PATH`, `--db PATH`, `--platforms PATH`, `--boot-dir DIR`
после раздела и действия. Пример безопасного чтения fixture:

```bash
napi-config eeprom show --eeprom /tmp/sample-eeprom.bin --json
```

Генерация, применение профиля и запись требуют явного `--yes`.
Интерактивных вопросов CLI не задаёт. JSON и обычные результаты идут в stdout,
ошибки — в stderr. Коды завершения: 0 — успех, 1 — ошибка операции,
2 — ошибка аргументов, 130 — прерывание.
Instance JSON содержит все поля конфигурации, включая serial/date/MACs.
В reusable `boards.yaml` эти три поля не сохраняются.

## TUI

Сохранены навигация, прокрутка, двухколоночное меню на широком терминале,
загрузка профилей, редактирование полей и действия EEPROM/boot.
`View MACs` и Enter на строке MAC показывают адреса.
`Generate new MACs`, загрузка профиля, сброс и операции записи требуют точное `yes`.
Загрузка профиля сохраняет serial number, manufacturing date и MACs.
Сервисное действие добавления EEPROM overlay может записать boot-файл
и перезагрузить устройство после подтверждения.

## Boot-конфигурация и доступность EEPROM

Boot-файл определяется автоматически: `armbianEnv.txt` либо `uEnv.txt`.
Если присутствуют оба, проверяется используемый `boot.scr` (при его отсутствии
`boot.cmd`). При неоднозначности запись недоступна. В TUI нет переключателя файлов;
показываются обнаруженный путь и текущий префикс.

`View current boot config` открывает полный файл с прокруткой.
`Write overlay settings` показывает изменения, затем требует `yes`, создаёт backup
и записывает автоматически сформированную строку без перезагрузки.
CLI предоставляет `boot show`, `boot preview`, `boot write --yes`.

Текущий `overlay_prefix` сохраняется. При непустом префиксе `overlays=` содержит
короткие имена; без префикса — полные имена (`rk3308-…`) без `.dtbo`.
Префикс из platforms.yaml в boot-файл не добавляется.
Имена проверяются по реальным файлам в каталогах загрузчика. Для USB Host
допускается проверенный вариант `otg-host`, если `usb20-host` отсутствует.
Отсутствующий overlay блокирует запись и указывается в ошибке.
`user_overlays` всегда сохраняет полные имена. Остальные настройки и комментарии
boot-файла сохраняются.

`Load EEPROM` / `Write EEPROM` остаются видимыми с `disabled`, если устройство
недоступно. Причина отображается при наведении и выборе; операция не выполняется.
Отдельный EEPROM overlay не требуется, если устройство объявлено в основном DTB.
`Add EEPROM overlay and reboot` получает `disabled`, если файл EEPROM overlay
не найден, с указанием имени `.dtbo`. Действие требует `yes`, сохраняет backup,
затем перезагружает устройство. Пункт скачивания overlay пока не реализован.

## Архитектура и совместимость

- `core/`: модели, EEPROM codec/CRC, MAC, проверки, профили, overlays,
  расчёт boot-изменений и общий `BoardService`.
- `hardware/`: Linux I2C/sysfs/Device Tree, EEPROM I/O, boot-файлы и reboot.
- `storage/`: YAML/JSON, backups, получение профилей через urllib.
- `tui/` и `cli/`: ввод и представление результатов, вызовы Core.
- `bootstrap.py`: создание адаптеров и сервиса.

Core не читает файлы и не импортирует curses, Hardware или Storage.
Будущий REST адаптер может использовать тот же сервис с отдельной конфигурацией
для каждого запроса. Подробности — в [ARCHITECTURE.md](ARCHITECTURE.md).

EEPROM format v2, interface bits и YAML-схемы сохранены.
Проверки стали строже: недопустимые поля отклоняются до изменения состояния;
имя длиннее 32 UTF-8 байт отклоняется вместо обрезания.
EEPROM read-back сравнивается с записанными bytes, затем проверяется кодеком.
Профили/JSON сохраняются через временный файл с backup; boot-файлы также
сохраняют backup и проверяются чтением. EEPROM backup автоматически не создаётся.

## Проверка

```bash
python3 -B -m unittest discover -s tests -v
```

Тесты используют временные файлы и подставные адаптеры, без доступа к плате.
Проверяются EEPROM round trip/CRC, ограничения полей, подтверждения,
MAC, сохранение данных экземпляра и производственный сценарий CLI.
Реальная запись EEPROM, boot и reboot требуют отдельного аппаратного тестирования.

## История до v16

## Изменения v11

- Без `--db` используется `boards.yaml` из каталога самого `napi-board-config.py`,
  независимо от текущего рабочего каталога.
- Подтверждение всех операций записи показывается в отдельном окне.
  Для подтверждения нужно ввести точное `yes` маленькими буквами.
- Отсутствие EEPROM не блокирует программу. `Load EEPROM` и `Write EEPROM`
  сообщают `EEPROM is not initialized`, после чего возвращают управление в меню.
  Local DB, BOARD CONFIG, Write boot file и SERVICE продолжают работать.
- Убрано повторное `(required by EEPROM)` у I2C1.
- Остальная логика v10 сохранена.

### Запуск

Из каталога программы:

```bash
sudo ./napi-board-config.py
```

Явный файл базы при необходимости:

```bash
sudo ./napi-board-config.py --db /path/to/boards.yaml
```

## v12
- Load defaults resets only BOARD CONFIG after exact `yes`.
- Load EEPROM asks before replacing current BOARD CONFIG.
- Comment is stored in YAML only and never written to EEPROM.
- Loading EEPROM clears Comment.
- Load DB from GitHub downloads and validates the NAPILAB boards.yaml, then asks `yes`,
  backs up the local DB and replaces it.

## v13 — platform architecture

- Fixed Armbian overlay naming: with `overlay_prefix=rk3308`, `overlays=` now contains
  `uart1`, `uart2-m0`, `i2c1-ds1338`, etc., without a second `rk3308-`.
- Board config now contains `platform`.
- Platform ID is stored in EEPROM format v2. Experimental v1 is no longer supported.
- Platform-specific overlay names, overlay prefix, conflicts and EEPROM user overlay are
  stored in `platforms.yaml`, not hard-coded into board YAML.
- `Write boot file` writes/updates `overlay_prefix=` for the selected platform.
- `user_overlays=` keeps the complete user-overlay name (for RK3308:
  `rk3308-i2c1-eeprom`).
- RK3308 uses the established overlay mappings listed in platforms.yaml. RK3568 is intentionally not guessed: add its real mapping to
  `platforms.yaml` when its overlays/pin conflicts are known.

## v14 — board instance data

EEPROM format v2 is now the clean production-oriented format. It contains platform ID,
product/revision, interface mask, board name, serial number, manufacture date, eight MAC
slots and CRC32. Experimental EEPROM v1 compatibility was removed.

`BOARD INSTANCE / EEPROM` data:
- Serial number: editable uint32.
- Manufacture date: editable as YYYY-MM-DD; when empty, editing proposes today's date.
- MAC addresses: eight locally-administered unicast addresses generated as one contiguous block.
- `Generate MACs` replaces existing MACs only after exact `yes`.
- `View MACs` displays the stored/generated addresses.
- Serial/date/MACs are EEPROM-only and are not saved in boards.yaml.
- Loading a board model from local DB preserves current serial/date/MACs.


## v14
- EEPROM v2 contains serial number, manufacture date, 8 MAC slots and CRC32.
- Serial/date/MACs are instance-only: they are not saved to boards.yaml.
- Empty manufacture date proposes today's date when edited.
- Generate MACs creates 8 sequential locally-administered unicast MAC addresses.
- Existing MACs require exact `yes` before replacement.
- View MACs displays the current addresses.
- Loading a board from local DB preserves serial/date/MACs.
