from .models import BoardConfig, IF_BY_KEY, RTC_CHOICES, INTERFACES

def profile_to_config(b):
    enabled = set()

    interfaces = b.get("interfaces", {})
    if isinstance(interfaces, dict):
        enabled |= {k for k, v in interfaces.items() if v and k in IF_BY_KEY}
    elif isinstance(interfaces, list):
        enabled |= {k for k in interfaces if k in IF_BY_KEY}

    rtc = "none"
    i2c = b.get("i2c", {})
    if isinstance(i2c, dict):
        for bus in ("i2c0", "i2c1", "i2c3"):
            bus_cfg = i2c.get(bus, {})
            if isinstance(bus_cfg, dict) and bus_cfg.get("enabled"):
                enabled.add(bus)
        i2c1 = i2c.get("i2c1", {})
        if isinstance(i2c1, dict):
            rtc = str(i2c1.get("rtc", "none")).lower()

    # Backward compatibility with v5 flat YAML.
    if isinstance(interfaces, dict) and interfaces.get("rtc") and rtc == "none":
        rtc = "ds1338"

    if rtc not in RTC_CHOICES:
        rtc = "none"

    enabled.discard("rtc")
    if rtc != 'none':
        enabled.add('i2c1')
    return BoardConfig(
        platform=str(b.get("platform", "rk3308")),
        product_id=int(b.get("id", 0)),
        product_rev=int(b.get("rev", 1)),
        board_name=str(b.get("name", "NAPI Board")),
        comment=str(b.get("comment", "")),
        enabled=enabled,
        rtc_i2c1=rtc,
    )


def config_to_profile(cfg: BoardConfig):
    slug = cfg.board_name.lower().strip()
    slug = "".join(ch if ch.isalnum() else "-" for ch in slug)
    while "--" in slug:
        slug = slug.replace("--", "-")

    item = {
        "id": cfg.product_id,
        "rev": cfg.product_rev,
        "slug": slug.strip("-") or f"board-{cfg.product_id}",
        "name": cfg.board_name,
        "comment": cfg.comment,
        "platform": cfg.platform,
        "interfaces": {
            i.key: (i.key in cfg.enabled)
            for i in INTERFACES
            if i.key not in {"i2c0", "i2c1", "i2c3"}
        },
        "i2c": {
            "i2c0": {"enabled": "i2c0" in cfg.enabled},
            "i2c1": {
                "enabled": "i2c1" in cfg.enabled,
                "rtc": cfg.rtc_i2c1,
            },
            "i2c3": {"enabled": "i2c3" in cfg.enabled},
        },
    }

    return item
