def patch_boot(text, prefix, overlays, user_overlay=None, ensure_only=False):
    lines = text.splitlines()
    values = {}
    for line in lines:
        if '=' in line and line.split('=', 1)[0] in ('overlay_prefix', 'overlays', 'user_overlays'):
            key, value = line.split('=', 1)
            values.setdefault(key, value)
    if ensure_only:
        existing = values.get('overlays', '').split()
        if prefix:
            existing = [v.removeprefix(prefix + '-') for v in existing]
        variants = set(overlays)
        if not any(v in variants for v in existing) and overlays:
            existing.insert(0, overlays[0])
        values['overlays'] = ' '.join(dict.fromkeys(existing))
    else:
        values['overlays'] = ' '.join(overlays)
    if prefix:
        values['overlay_prefix'] = prefix
    if user_overlay:
        values['user_overlays'] = ' '.join(dict.fromkeys(values.get('user_overlays', '').split() + [user_overlay]))
    result, seen = [], set()
    for line in lines:
        key = line.split('=', 1)[0]
        if key in values:
            if key not in seen:
                result.append(key + '=' + values[key])
                seen.add(key)
        else:
            result.append(line)
    result.extend(key + '=' + value for key, value in values.items() if key not in seen)
    return '\n'.join(result).rstrip() + '\n'
