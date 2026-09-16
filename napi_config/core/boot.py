import re


def boot_values(text):
    values = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        if key.strip() in values:
            raise ValueError(f'Duplicate boot setting: {key.strip()}')
        values[key.strip()] = value.strip()
    return values


def resolve_overlay(name, platform, prefix, files, aliases=None):
    names = [name] + list((aliases or {}).get(name, []))
    # Platform mapping stores logical names; runtime prefix belongs to boot config.
    full_prefix = prefix or platform
    if not re.fullmatch(r'[A-Za-z0-9_.-]+', full_prefix):
        raise ValueError(f'Invalid overlay prefix: {full_prefix}')
    for logical in names:
        full = full_prefix + '-' + logical
        if full in files:
            return logical if prefix else full
    return name if prefix else full_prefix + '-' + name


def patch_boot(text, overlays):
    # Only replace overlays. Preserve user_overlays and overlay_prefix.
    boot_values(text)
    replacements = {'overlays': ' '.join(dict.fromkeys(overlays))}
    result, seen = [], set()
    for line in text.splitlines():
        key = line.split('=', 1)[0].strip() if '=' in line and not line.lstrip().startswith('#') else None
        if key in replacements:
            result.append(key + '=' + replacements[key])
            seen.add(key)
        else:
            result.append(line)
    result.extend(key + '=' + value for key, value in replacements.items() if key not in seen)
    return '\n'.join(result) + '\n'
