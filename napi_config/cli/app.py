import argparse
import json
import sys


def parser():
    from ..bootstrap import DEFAULT_DB, DEFAULT_EEPROM, DEFAULT_PLATFORMS, DEFAULT_SETTINGS, DEFAULT_OTP
    result = argparse.ArgumentParser(prog='napi-config')
    result.add_argument('--version', action='version', version='napi-config 21')
    groups = result.add_subparsers(dest='group', required=True)
    actions = {'eeprom': ['show', 'preview', 'write', 'reset', 'migrate', 'overlays', 'enable'],
               'processor': ['status', 'write', 'bind', 'rebind', 'reset'],
               'mac': ['generate', 'preview', 'write', 'assignments'], 'board': ['info'], 'overlay': ['list'],
               'boot': ['show', 'preview', 'write'], 'hardware': ['detect'], 'tui': []}
    for group, names in actions.items():
        group_parser = groups.add_parser(group)
        commands = group_parser.add_subparsers(dest='action', required=True) if names else None
        for name in names or [None]:
            command = commands.add_parser(name) if name else group_parser
            command.add_argument('--eeprom', default=DEFAULT_EEPROM)
            command.add_argument('--db', default=DEFAULT_DB)
            command.add_argument('--platforms', default=DEFAULT_PLATFORMS)
            command.add_argument('--settings', default=DEFAULT_SETTINGS, help='MAC assignment settings YAML')
            command.add_argument('--boot-dir', default='/boot', help='Boot directory (auto-detect env file within it)')
            if group != 'tui':
                command.add_argument('--json', action='store_true', help='Machine-readable JSON output')
            if group in ('board', 'overlay', 'mac') or (group == 'boot' and name != 'show'):
                inputs = command.add_mutually_exclusive_group()
                inputs.add_argument('--config', help='Instance JSON configuration')
                inputs.add_argument('--profile', nargs=2, type=int, metavar=('ID', 'REV'), help='Reusable profile; requires --yes')
                command.add_argument('--yes', action='store_true', help='Explicitly confirm changes')
            if group in ('tui', 'processor', 'mac') or (group == 'eeprom' and name in ('write', 'preview')):
                command.add_argument('--otp', default=DEFAULT_OTP, help='RK3308 NVMEM path; offset 20, length 5')
            if (group == 'processor' and name != 'status') or (group == 'eeprom' and name in ('migrate', 'reset')):
                command.add_argument('--preview', action='store_true', help='Show proposed changes without writing')
                command.add_argument('--yes', action='store_true', help='Explicitly confirm EEPROM change')
            if group == 'processor' and name == 'write':
                command.add_argument('--config', help='Full instance JSON for initializing empty EEPROM; otherwise defaults')
            if group == 'mac' and name == 'generate':
                command.add_argument('--output', help='Save generated instance configuration as JSON')
            if group == 'eeprom' and name in ('overlays', 'enable'):
                command.add_argument('--platform', default='rk3308')
            if group == 'eeprom' and name == 'enable':
                command.add_argument('--overlay', required=True, help='Exact overlay name returned by eeprom overlays')
                command.add_argument('--yes', action='store_true', help='Confirm chip, bus, address and boot write')
                command.add_argument('--reboot', action='store_true', help='Reboot after confirmed setup')
            if group == 'eeprom' and name in ('preview', 'write'):
                command.add_argument('--config', required=True, help='Instance JSON configuration to write')
                if name == 'write':
                    command.add_argument('--yes', action='store_true', help='Confirm EEPROM write')
    return result


def execute(args, service):
    if args.group == 'processor' or (args.group == 'eeprom' and args.action in ('migrate', 'reset')):
        if args.group == 'processor' and args.action == 'status':
            return service.processor_status()
        plan = ((service.reset_eeprom_plan() if args.action == 'reset' else service.migration_plan())
                if args.group == 'eeprom' else service.processor_write_plan(
                    service.load_configuration(args.config) if args.config else None) if args.action == 'write'
                else service.reset_processor_plan() if args.action == 'reset'
                else service.processor_plan(rebind=args.action == 'rebind'))
        if args.preview:
            return {'preview': plan['comparison'], 'changed': plan['changed'], 'writable': plan.get('writable', True)}
        if plan.get('writable') is False:
            raise ValueError(plan['reason'])
        written = (service.apply_reset_eeprom_plan(plan, confirmed=args.yes) if args.group == 'eeprom' and args.action == 'reset'
                   else service.apply_reset_processor_plan(plan, confirmed=args.yes) if args.action == 'reset'
                   else service.apply_processor_write_plan(plan, confirmed=args.yes) if args.action == 'write'
                   else service.apply_instance_eeprom_plan(plan, confirmed=args.yes))
        return {'written': written, 'verified': True,
                'backup': getattr(service, 'last_eeprom_backup', None)}
    if args.group == 'hardware':
        return service.detect()
    if args.group == 'eeprom':
        if args.action == 'overlays':
            return service.eeprom_setup(args.platform)
        if args.action == 'enable':
            cfg = service.update(service.defaults(), 'platform', args.platform)
            plan = service.boot_plan(cfg, eeprom_overlay=args.overlay)
            backup = service.apply_boot_plan(plan, confirmed=args.yes)
            if args.reboot:
                service.reboot(confirmed=args.yes)
            return {'path': plan['target'], 'changed': plan['changed'], 'backup': backup,
                    'warnings': plan['warnings'], 'reboot': args.reboot}
        if args.action == 'show':
            return service.document(service.read_eeprom())
        if args.action == 'preview':
            return {'preview': service.eeprom_preview(service.load_configuration(args.config))}
        written = service.write_eeprom(service.load_configuration(args.config), confirmed=args.yes)
        return {'written': written, 'verified': True}
    if args.group == 'boot' and args.action == 'show':
        info = service.boot_info()
        return {'path': info['path'], 'overlay_prefix': info['overlay_prefix'], 'content': info['content']}
    if args.config:
        cfg = service.load_configuration(args.config)
    elif args.profile:
        identity = tuple(args.profile)
        profile = next((p for p in service.list_profiles() if (p['id'], p['rev']) == identity), None)
        if profile is None:
            raise ValueError('Board profile not found')
        cfg = service.apply_profile(service.defaults(), profile, confirmed=args.yes)
    elif args.group == 'mac' and args.action in ('generate', 'preview'):
        cfg, _ = service.initial_configuration()
    else:
        cfg = service.read_eeprom()
    if args.group == 'mac':
        if args.action == 'assignments':
            return service.mac_assignments(cfg)
        if args.action == 'write':
            plan = service.mac_eeprom_plan(cfg.macs)
            written = service.apply_mac_eeprom_plan(plan, confirmed=args.yes)
            return {'written': written, 'verified': True, 'configuration': plan['proposed']}
        plan = service.mac_plan(cfg)
        if args.action == 'preview':
            return plan
        cfg = service.apply_mac_plan(cfg, plan, confirmed=args.yes)
        if args.output:
            service.save_configuration(args.output, cfg, confirmed=args.yes)
        return {'configuration': service.document(cfg), 'mac_generation': plan}
    if args.group == 'overlay':
        return service.overlays(cfg)
    if args.group == 'boot':
        plan = service.boot_plan(cfg)
        if args.action == 'write':
            backup = service.apply_boot_plan(plan, confirmed=args.yes)
            return {'path': plan['target'], 'changed': plan['changed'], 'backup': backup,
                    'warnings': plan['warnings'], 'reboot': False}
        return plan
    return service.document(cfg)


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        from ..bootstrap import create_service
        from ..bootstrap import DEFAULT_OTP
        service = create_service(args.eeprom, args.db, args.platforms, args.boot_dir,
                                 getattr(args, 'otp', DEFAULT_OTP), args.settings)
        if args.group == 'tui':
            from ..tui.app import run
            return run(service)
        result = execute(args, service)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        elif (args.group == 'processor' and args.action != 'status') or (args.group == 'eeprom' and args.action in ('migrate', 'reset')):
            if args.preview:
                comparison = result['preview']
                print(comparison['title'] + '\n' + comparison['note'])
                print(f'{"Field":<28} {"Proposed / new":<35} Current / old')
                for row in comparison['rows']:
                    print(f'{row["field"]:<28} {row["proposed"]:<35} {row["current"]}')
            else:
                print(json.dumps(result, ensure_ascii=False))
        elif args.group == 'mac' and args.action in ('preview', 'generate'):
            plan = result if args.action == 'preview' else result['mac_generation']
            print('RK3308 OTP ID: ' + plan['otp_id'])
            print('MAC source: ' + plan['source'])
            for heading, key in [('Current configuration', 'current'), ('Current EEPROM', 'eeprom_current'),
                                 ('Generated', 'generated')]:
                print('\n' + heading + ':')
                if plan[key] is not None:
                    cfg = service.configuration_from_document(plan[key])
                    print('\n'.join(service.mac_rows(cfg)) or 'No MAC addresses')
                else:
                    print(plan['eeprom_error'])
            if plan['already_matches']:
                print('\nMAC addresses already match this SoC.')
            if args.action == 'generate' and args.output:
                print('\nInstance configuration saved: ' + args.output)
        else:
            for key, value in result.items():
                print(f'{key}: {", ".join(map(str, value)) if isinstance(value, list) else value}')
        return 0
    except (ValueError, TypeError, OSError, KeyError, ImportError) as exc:
        print(f'napi-config: {exc}', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
