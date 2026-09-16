import argparse
import json
import sys


def parser():
    from ..bootstrap import DEFAULT_DB, DEFAULT_EEPROM, DEFAULT_PLATFORMS
    result = argparse.ArgumentParser(prog='napi-config')
    result.add_argument('--version', action='version', version='napi-config 16')
    groups = result.add_subparsers(dest='group', required=True)
    actions = {'eeprom': ['show', 'write'], 'mac': ['generate'], 'board': ['info'], 'overlay': ['list'], 'hardware': ['detect'], 'tui': []}
    for group, names in actions.items():
        group_parser = groups.add_parser(group)
        commands = group_parser.add_subparsers(dest='action', required=True) if names else None
        for name in names or [None]:
            command = commands.add_parser(name) if name else group_parser
            command.add_argument('--eeprom', default=DEFAULT_EEPROM)
            command.add_argument('--db', default=DEFAULT_DB)
            command.add_argument('--platforms', default=DEFAULT_PLATFORMS)
            if group != 'tui':
                command.add_argument('--json', action='store_true', help='Machine-readable JSON output')
            if group in ('board', 'overlay', 'mac'):
                inputs = command.add_mutually_exclusive_group()
                inputs.add_argument('--config', help='Instance JSON configuration')
                inputs.add_argument('--profile', nargs=2, type=int, metavar=('ID', 'REV'), help='Reusable profile; requires --yes')
                command.add_argument('--yes', action='store_true', help='Explicitly confirm changes')
            if group == 'mac':
                command.add_argument('--output', help='Save generated instance configuration as JSON')
            if group == 'eeprom' and name == 'write':
                command.add_argument('--config', required=True, help='Instance JSON configuration to write')
                command.add_argument('--yes', action='store_true', help='Confirm EEPROM write')
    return result


def execute(args, service):
    if args.group == 'hardware':
        return service.detect()
    if args.group == 'eeprom':
        if args.action == 'show':
            return service.document(service.read_eeprom())
        service.write_eeprom(service.load_configuration(args.config), confirmed=args.yes)
        return {'written': True, 'verified': True}
    if args.config:
        cfg = service.load_configuration(args.config)
    elif args.profile:
        identity = tuple(args.profile)
        profile = next((p for p in service.list_profiles() if (p['id'], p['rev']) == identity), None)
        if profile is None:
            raise ValueError('Board profile not found')
        cfg = service.apply_profile(service.defaults(), profile, confirmed=args.yes)
    elif args.group == 'mac':
        cfg = service.defaults()
    else:
        cfg = service.read_eeprom()
    if args.group == 'mac':
        cfg = service.generate_macs(cfg, confirmed=args.yes)
        if args.output:
            service.save_configuration(args.output, cfg, confirmed=args.yes)
        return service.document(cfg)
    if args.group == 'overlay':
        return service.overlays(cfg)
    return service.document(cfg)


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        from ..bootstrap import create_service
        service = create_service(args.eeprom, args.db, args.platforms)
        if args.group == 'tui':
            from ..tui.app import run
            return run(service)
        result = execute(args, service)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        else:
            for key, value in result.items():
                print(f'{key}: {", ".join(map(str, value)) if isinstance(value, list) else value}')
        return 0
    except (ValueError, TypeError, OSError, KeyError, ImportError) as exc:
        print(f'napi-config: {exc}', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
