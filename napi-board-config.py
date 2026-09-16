#!/usr/bin/env python3
"""Compatible launcher for the original TUI and --dump option."""
import argparse
import sys
from napi_config.bootstrap import DEFAULT_DB, DEFAULT_EEPROM, DEFAULT_PLATFORMS, create_service


def main():
    parser = argparse.ArgumentParser(description='NAPI board EEPROM/profile configurator')
    parser.add_argument('--eeprom', default=DEFAULT_EEPROM)
    parser.add_argument('--db', default=DEFAULT_DB)
    parser.add_argument('--platforms', default=DEFAULT_PLATFORMS)
    parser.add_argument('--dump', action='store_true')
    args = parser.parse_args()
    try:
        service = create_service(args.eeprom, args.db, args.platforms)
        if args.dump:
            cfg = service.read_eeprom()
            for key, value in service.document(cfg).items():
                print(f'{key}: {value}')
            print('overlays=' + ' '.join(service.overlays(cfg)['overlays']))
            return 0
        from napi_config.tui.app import run
        return run(service)
    except (ValueError, TypeError, OSError, KeyError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
