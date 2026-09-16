# NAPI Board Config v14

## Запуск

```bash
sudo ./napi-board-config.py --db ./boards.yaml
```

## Интерфейс

Верхняя секция ACTIONS:
- Load EEPROM
- Write EEPROM
- Write boot file
- Load board from local DB
- Save board to local DB
- Delete board from local DB

На широком терминале ACTIONS показываются в два столбца. На узком — автоматически в один.
Ниже расположены BOARD CONFIG, PROGRAM SETTINGS и SERVICE.

RTC теперь отдельный переключатель `[ ] RTC`. После включения тип RTC выбирается отдельной
строкой: DS1307 / DS1338 / DS3231. I2C1 нельзя отключить, поскольку он нужен EEPROM.

Load board from local DB:
- Enter — загрузить выбранную плату;
- Esc или q — отменить без изменения текущей конфигурации.

Все операции записи/изменения требуют точный ввод `yes` маленькими буквами.
Перед изменением local DB автоматически создаётся timestamp backup `boards.yaml.bak-*`.
Перед изменением boot file также создаётся backup.

Ctrl-C корректно завершает curses-интерфейс без traceback.

User overlay `rk3308-i2c1-eeprom.dtbo` должен находиться в `/boot/overlay-user/`.


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
