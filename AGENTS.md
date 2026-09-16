# Repository Guidelines

## Project Structure & Module Organization

This repository contains a standalone Python terminal utility for configuring NAPI boards under Armbian/U-Boot.

- `napi-board-config.py`: curses UI, board models, YAML persistence, EEPROM serialization, and boot configuration updates.
- `boards.yaml`: reusable board profiles, currently including FCU3308.
- `platforms.yaml`: platform IDs, overlay names, and interface conflicts; currently defines RK3308.
- `README.md`: usage instructions and version history.

There are no dedicated test or asset directories. Keep platform-specific mappings in `platforms.yaml`. Serial numbers, manufacture dates, and MAC addresses belong to board instances in EEPROM, not reusable YAML profiles.

## Build, Test, and Development Commands

Python 3.9+ and PyYAML are required; curses comes from the Python installation. No build step is needed.

```bash
sudo apt install python3-yaml
python3 napi-board-config.py --help
python3 -m py_compile napi-board-config.py
python3 napi-board-config.py --db ./boards.yaml
python3 napi-board-config.py --dump --eeprom /tmp/sample-eeprom.bin
```

These commands install the dependency, show CLI options, check syntax, launch the UI, and decode an existing EEPROM fixture. Use elevated privileges only when hardware or boot-file access requires them.

## Coding Style & Naming Conventions

Use four-space indentation, `snake_case` for functions and fields, `PascalCase` for classes, and uppercase names for constants. Follow existing dataclass and type-annotation conventions. Prefer readable multiline statements when extending compact legacy code. No formatter or linter configuration is present.

Preserve existing interface keys and EEPROM bit assignments. Platform IDs must be unique, and overlay names must match actual platform overlays.

## Testing Guidelines

No automated test suite, framework, or coverage threshold is configured. For serialization changes, verify encode/decode round trips, CRC corruption rejection, field limits, and invalid dates. For profile changes, verify instance data stays outside YAML and survives profile loading.

Exercise UI changes in wide and narrow terminals. Use temporary files for persistence checks. If adding automated tests, prefer `tests/test_*.py` and document their runner in `README.md`.

## Commit & Pull Request Guidelines

Git history is unavailable in this workspace, so no existing message convention can be confirmed. Use concise imperative subjects such as `Fix MAC actions in terminal menu`.

Describe behavior changes, validation performed, and hardware limitations. Link relevant issues and include terminal screenshots for layout changes. Update documentation when CLI options, YAML schemas, or EEPROM formats change.

## Hardware & Configuration Safety

Writes require the application's `yes` confirmation. Preserve backups and read-back verification. The EEPROM service action can modify boot configuration and reboot the device; run it only during deliberate hardware testing.
