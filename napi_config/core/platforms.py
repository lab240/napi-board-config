class PlatformCatalog:
    def __init__(self, data):
        if not isinstance(data, dict) or not data:
            raise ValueError("Invalid platform catalog")
        self.data = data
        ids = [int(p['id']) for p in data.values()]
        if len(ids) != len(set(ids)) or any(i < 1 or i > 255 for i in ids):
            raise ValueError("Platform IDs must be unique and in 1..255")

    def get(self, name):
        if name not in self.data:
            raise ValueError(f"Unknown platform: {name}")
        return self.data[name]

    def names(self):
        return list(self.data)

    def id_for(self, name):
        return int(self.get(name)['id'])

    def name_for_id(self, platform_id):
        for name in self.data:
            if self.id_for(name) == platform_id:
                return name
        raise ValueError(f"Unknown platform id: {platform_id}")
