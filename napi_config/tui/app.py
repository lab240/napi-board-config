import curses
import difflib
import textwrap
from ..core.models import INTERFACES, IF_BY_KEY


class UI:
    ACTIONS = [
        ("action", "load_defaults"),
        ("action", "read"),
        ("action", "write_eeprom"),
        ("action", "write_env"),
        ("action", "profile_load"),
        ("action", "profile_add"),
        ("action", "profile_delete"),
        ("action", "github_db"),
        ("action", "view_macs"),
        ("action", "generate_macs"),
        ("action", "view_boot"),
    ]

    def __init__(self, stdscr, service):
        self.stdscr = stdscr
        self.service = service
        self.cfg = service.defaults()
        self.action_states = {}
        self.boot_summary = "Not detected"
        self.blob = None
        self.status = "Ready"
        self.cursor = 0
        self.rows = list(self.ACTIONS) + [
            ("field", "platform"),
            ("field", "product_id"),
            ("field", "product_rev"),
            ("field", "board_name"),
            ("field", "comment"),
        ]
        self.rows += [("iface", i.key) for i in INTERFACES]
        self.rows += [
            ("rtc", "rtc_enabled"),
            ("field", "rtc_type"),
            ("instance", "serial_number"),
            ("instance", "mfg_date"),
            ("instance", "mac_count"),
            ("field", "env_target"),
            ("action", "enable_i2c1_eeprom"),
            ("action", "quit"),
        ]

    def is_action_cursor(self):
        return 0 <= self.cursor < len(self.ACTIONS)

    def run(self):
        curses.curs_set(0)
        self.stdscr.keypad(True)
        while True:
            self.draw()
            ch = self.stdscr.getch()
            if ch == 3:  # Ctrl-C
                return
            if self.is_action_cursor() and ch in (curses.KEY_LEFT, curses.KEY_RIGHT,
                                                   curses.KEY_UP, curses.KEY_DOWN,
                                                   ord("h"), ord("l"), ord("k"), ord("j")):
                n = len(self.ACTIONS)
                cols = 2 if self.stdscr.getmaxyx()[1] >= 64 else 1
                if cols == 2:
                    if ch in (curses.KEY_LEFT, ord("h")):
                        self.cursor = max(0, self.cursor - 1)
                    elif ch in (curses.KEY_RIGHT, ord("l")):
                        self.cursor = min(n-1, self.cursor + 1)
                    elif ch in (curses.KEY_UP, ord("k")):
                        self.cursor = max(0, self.cursor - 2)
                    else:
                        nxt = self.cursor + 2
                        self.cursor = nxt if nxt < n else min(n, len(self.rows)-1)
                else:
                    if ch in (curses.KEY_UP, ord("k")):
                        self.cursor = (self.cursor - 1) % len(self.rows)
                    elif ch in (curses.KEY_DOWN, ord("j")):
                        self.cursor = (self.cursor + 1) % len(self.rows)
            elif ch in (curses.KEY_UP, ord("k")):
                self.cursor = (self.cursor - 1) % len(self.rows)
            elif ch in (curses.KEY_DOWN, ord("j")):
                self.cursor = (self.cursor + 1) % len(self.rows)
            elif ch == ord(" "):
                kind, key = self.rows[self.cursor]
                if kind == "iface":
                    self.toggle_interface(key)
                elif kind == "rtc":
                    self.toggle_rtc()
            elif ch in (10, 13, curses.KEY_ENTER):
                if self.activate():
                    return
            elif ch in (ord("q"), ord("Q")):
                return

    def action_label(self, key):
        label = {
            "load_defaults": "[ Load defaults ]",
            "read": "[ Load EEPROM ]",
            "write_eeprom": "[ Write EEPROM ]",
            "write_env": "[ Write overlay settings ]",
            "profile_load": "[ Load board from local DB ]",
            "profile_add": "[ Save board to local DB ]",
            "profile_delete": "[ Delete board from local DB ]",
            "github_db": "[ Load DB from GitHub ]",
            "view_macs": "[ View MACs ]",
            "generate_macs": "[ Generate new MACs ]",
            "enable_i2c1_eeprom": "[ Add EEPROM overlay and reboot ]",
            "view_boot": "[ View current boot config ]",
            "quit": "[ Quit ]",
        }[key]
        state = getattr(self, 'action_states', {}).get(key, {})
        return label + (' disabled' if state.get('enabled') is False else '')

    def config_text(self, kind, key):
        if kind == "field":
            if key == "platform":
                return f"Platform     : {self.cfg.platform}"
            if key == "product_id":
                return f"Product ID   : {self.cfg.product_id}"
            if key == "product_rev":
                return f"Product rev  : {self.cfg.product_rev}"
            if key == "board_name":
                return f"Board name   : {self.cfg.board_name}"
            if key == "comment":
                return f"Comment      : {self.cfg.comment}"
            if key == "env_target":
                return f"Boot config  : {self.boot_summary}"
            if key == "rtc_type":
                rtc = self.cfg.rtc_i2c1.upper() if self.cfg.rtc_i2c1 != "none" else "-"
                return f"    RTC type : {rtc}"
        if kind == "instance":
            if key == "serial_number":
                return f"Serial number: {self.cfg.serial_number:08d}"
            if key == "mfg_date":
                return f"Mfg date     : {self.cfg.mfg_date or '-'}"
            if key == "mac_count":
                return f"MAC addresses: {len(self.cfg.macs)}"
        if kind == "rtc":
            return f"[{'x' if self.cfg.rtc_i2c1 != 'none' else ' '}] RTC"
        if kind == "iface":
            idef = IF_BY_KEY[key]
            mark = "x" if key in self.cfg.enabled else " "
            suffix = ""
            blocked = self.blocked_by(key)
            if blocked and key not in self.cfg.enabled:
                suffix += f" (blocked by {', '.join(blocked)})"
            return f"[{mark}] {idef.title}{suffix}"
        return self.action_label(key)

    def draw(self):
        self.action_states = {key: self.service.action_status(self.cfg, key)
                              for key in ('read', 'write_eeprom', 'enable_i2c1_eeprom', 'view_boot', 'write_env')}
        try:
            info = self.service.boot_info()
            self.boot_summary = info['path'] + ' (prefix: ' + (info['overlay_prefix'] or 'none') + ')'
        except (ValueError, OSError) as exc:
            self.boot_summary = str(exc)
        s = self.stdscr
        s.erase()
        h, w = s.getmaxyx()
        if h < 8 or w < 28:
            try:
                s.addnstr(0, 0, "Terminal too small", max(1, w-1))
                s.addnstr(1, 0, f"{w}x{h}; need >=28x8", max(1, w-1))
            except curses.error:
                pass
            s.refresh()
            return

        title = " NAPI Board Config v17 "
        try:
            s.addnstr(0, max(0,(w-len(title))//2), title, w-1, curses.A_BOLD)
        except curses.error:
            pass

        action_cols = 2 if w >= 64 else 1
        action_rows = (len(self.ACTIONS)+action_cols-1)//action_cols
        logical = []

        logical.append(("section", "--- ACTIONS ---", None))
        for r in range(action_rows):
            logical.append(("actionrow", r, None))
        logical.append(("section", "--- BOARD CONFIG ---", None))

        first_cfg = len(self.ACTIONS)
        env_index = next(i for i,x in enumerate(self.rows) if x == ("field","env_target"))
        service_index = next(i for i,x in enumerate(self.rows) if x == ("action","enable_i2c1_eeprom"))
        for idx in range(first_cfg, env_index):
            logical.append(("item", idx, None))
        logical.append(("section", "--- BOOT CONFIGURATION ---", None))
        logical.append(("item", env_index, None))
        logical.append(("section", "--- SERVICE ---", None))
        for idx in range(service_index, len(self.rows)):
            logical.append(("item", idx, None))

        # Determine which logical screen line contains the cursor.
        cursor_line = 1
        for n, entry in enumerate(logical):
            typ, val, _ = entry
            if typ == "actionrow":
                r=val
                ids=[r*action_cols+c for c in range(action_cols) if r*action_cols+c < len(self.ACTIONS)]
                if self.cursor in ids: cursor_line=n
            elif typ=="item" and val==self.cursor:
                cursor_line=n

        visible=max(1,h-3)
        if not hasattr(self,"scroll_top"): self.scroll_top=0
        if cursor_line < self.scroll_top: self.scroll_top=cursor_line
        elif cursor_line >= self.scroll_top+visible:
            self.scroll_top=cursor_line-visible+1
        self.scroll_top=max(0,min(self.scroll_top,max(0,len(logical)-visible)))

        y=1
        for typ,val,_ in logical[self.scroll_top:self.scroll_top+visible]:
            try:
                if typ=="section":
                    s.addnstr(y,1,val,max(1,w-2),curses.A_BOLD)
                elif typ=="actionrow":
                    r=val
                    if action_cols==2:
                        colw=max(1,(w-3)//2)
                        for c in range(2):
                            idx=r*2+c
                            if idx>=len(self.ACTIONS): continue
                            text=self.action_label(self.ACTIONS[idx][1])
                            attr=curses.A_REVERSE if idx==self.cursor else curses.A_NORMAL
                            s.addnstr(y,1+c*colw,text,max(1,colw-1),attr)
                    else:
                        idx=r
                        text=self.action_label(self.ACTIONS[idx][1])
                        attr=curses.A_REVERSE if idx==self.cursor else curses.A_NORMAL
                        s.addnstr(y,1,text,max(1,w-2),attr)
                else:
                    idx=val
                    kind,key=self.rows[idx]
                    text=self.config_text(kind,key)
                    # RTC type is visibly disabled while RTC is off.
                    if key=="rtc_type" and self.cfg.rtc_i2c1=="none":
                        attr=curses.A_DIM
                    else:
                        attr=curses.A_REVERSE if idx==self.cursor else curses.A_NORMAL
                    s.addnstr(y,1,text,max(1,w-2),attr)
            except curses.error:
                pass
            y+=1

        try:
            kind, key = self.rows[self.cursor]
            state = self.action_states.get(key, {}) if kind == 'action' else {}
            message = 'disabled: ' + state['reason'] if state.get('enabled') is False else self.status
            s.addnstr(h-2,1,message,max(1,w-2))
            nav="Arrows move  Space toggle  Enter select  q quit  Ctrl-C exit"
            s.addnstr(h-1,1,nav,max(1,w-2),curses.A_DIM)
        except curses.error:
            pass
        s.refresh()

    def blocked_by(self, key):
        return [IF_BY_KEY[other].title for other in self.service.blockers(self.cfg, key)]

    def toggle_interface(self, key):
        self.perform(lambda: self.service.toggle_interface(self.cfg, key), update=True)

    def toggle_rtc(self):
        self.perform(lambda: self.service.toggle_rtc(self.cfg), update=True)

    def activate(self):
        kind,key=self.rows[self.cursor]
        if kind == 'action':
            state = self.service.action_status(self.cfg, key)
            if not state['enabled']:
                self.status = 'disabled: ' + state['reason']
                self.view_text(self.status)
                return False
        if kind=="field":
            self.edit_field(key); return False
        if kind=="iface":
            self.toggle_interface(key); return False
        if kind=="rtc":
            self.toggle_rtc(); return False
        if kind=="instance":
            self.edit_instance(key); return False
        {
            "load_defaults":self.load_defaults,
            "read":self.read_eeprom,
            "write_eeprom":self.write_eeprom,
            "write_env":self.write_env,
            "view_boot":self.view_boot,
            "profile_load":self.profile_load,
            "profile_add":self.profile_add,
            "profile_delete":self.profile_delete,
            "github_db":self.load_db_github,
            "generate_macs":self.generate_macs,
            "view_macs":self.view_macs,
            "enable_i2c1_eeprom":self.enable_i2c1_eeprom,
        }.get(key,lambda:None)()
        return key=="quit"

    def prompt(self,label,initial):
        h,w=self.stdscr.getmaxyx()
        curses.echo(); curses.curs_set(1)
        try:
            self.stdscr.move(h-2,1); self.stdscr.clrtoeol()
            prompt=f"{label} [{initial}]: "
            self.stdscr.addstr(h-2,1,prompt)
            raw=self.stdscr.getstr(h-2,1+len(prompt),max(1,w-len(prompt)-3))
            value=raw.decode("utf-8").strip()
            return value if value else str(initial)
        finally:
            curses.noecho(); curses.curs_set(0)

    def edit_field(self, key):
        if key == 'env_target':
            self.view_boot()
            return
        if key == 'platform':
            self.perform(lambda: self.service.next_platform(self.cfg), update=True)
        elif key == 'rtc_type':
            self.perform(lambda: self.service.next_rtc(self.cfg), update=True)
        else:
            def operation():
                value = self.prompt(key, getattr(self.cfg, key))
                if key in ('product_id', 'product_rev'):
                    value = int(value, 0)
                return self.service.update(self.cfg, key, value)
            self.perform(operation, update=True)

    def confirm_yes(self, message):
        h, w = self.stdscr.getmaxyx()
        ph = min(9, h)
        pw = min(max(42, min(70, w - 2)), w)
        y0 = max(0, (h - ph) // 2)
        x0 = max(0, (w - pw) // 2)
        win = curses.newwin(ph, pw, y0, x0)
        win.keypad(True)
        win.erase()
        win.box()

        # Wrap the question inside the dialog instead of putting it on one terminal line.
        usable = max(10, pw - 4)
        words = message.split()
        lines, cur = [], ""
        for word in words:
            candidate = word if not cur else cur + " " + word
            if len(candidate) <= usable:
                cur = candidate
            else:
                if cur:
                    lines.append(cur)
                cur = word
        if cur:
            lines.append(cur)
        lines = lines[:3]

        try:
            for i, line in enumerate(lines):
                win.addnstr(1 + i, 2, line, usable)
            prompt_y = min(ph - 3, 2 + len(lines))
            win.addnstr(prompt_y, 2, "Type yes to confirm:", usable, curses.A_BOLD)
            win.addnstr(ph - 2, 2, "Anything else = cancel", usable, curses.A_DIM)
        except curses.error:
            pass

        curses.echo()
        curses.curs_set(1)
        try:
            input_y = min(ph - 2, prompt_y + 1)
            win.move(input_y, 2)
            win.clrtoeol()
            win.addstr(input_y, 2, "> ")
            win.refresh()
            raw = win.getstr(input_y, 4, max(1, pw - 6))
            value = raw.decode("utf-8", errors="replace").strip()
            return value == "yes"
        except KeyboardInterrupt:
            raise
        finally:
            curses.noecho()
            curses.curs_set(0)

    def enable_i2c1_eeprom(self):
        def operation():
            plan = self.service.boot_plan(self.cfg, ensure_eeprom=True)
            if not plan['changed']:
                self.status = 'EEPROM overlay already configured'
                return False
            self.preview_boot(plan)
            if not self.confirm_yes('Add EEPROM overlay and reboot? Boot configuration will be changed.'):
                self.status = 'Cancelled'
                return False
            self.service.apply_boot_plan(plan, confirmed=True)
            self.service.reboot(confirmed=True)
        self.perform(operation)



    def edit_instance(self, key):
        if key == 'mac_count':
            self.view_macs()
            return
        def operation():
            initial = self.service.suggested_date(self.cfg) if key == 'mfg_date' else getattr(self.cfg, key)
            value = self.prompt(key, initial)
            if key == 'serial_number':
                value = int(value, 0)
            return self.service.update(self.cfg, key, value)
        self.perform(operation, update=True)

    def generate_macs(self):
        if not self.confirm_yes('Generate 8 new MACs and replace current MACs? EEPROM stays unchanged.'):
            self.status = 'MAC generation cancelled'
            return
        self.perform(lambda: self.service.generate_macs(self.cfg, confirmed=True), update=True)

    def view_macs(self):
        addresses = self.service.mac_strings(self.cfg)
        text = 'MAC addresses\n\n' + '\n'.join(f'{i}: {mac}' for i, mac in enumerate(addresses, 1)) if addresses else 'No MAC addresses generated'
        self.popup(text, wait=True)

    def load_defaults(self):
        if self.confirm_yes('Load defaults? Board configuration and instance data will be reset.'):
            self.perform(self.service.defaults, update=True)
        else:
            self.status = 'Load defaults cancelled'


    def load_db_github(self):
        def operation():
            data = self.service.download_profiles()
            if self.confirm_yes(f"Replace local DB with {len(data['boards'])} downloaded profiles?"):
                self.service.replace_profiles(data, confirmed=True)
            else:
                self.status = 'DB update cancelled'
                return False
        self.perform(operation)

    def read_eeprom(self):
        if self.confirm_yes('Load EEPROM? Current configuration in memory will be replaced.'):
            self.perform(self.service.read_eeprom, update=True)
        else:
            self.status = 'EEPROM load cancelled'

    def write_eeprom(self):
        if self.confirm_yes('WRITE EEPROM with current configuration?'):
            self.perform(lambda: self.service.write_eeprom(self.cfg, confirmed=True))
        else:
            self.status = 'EEPROM write cancelled'

    def write_env(self):
        def operation():
            plan = self.service.boot_plan(self.cfg)
            self.preview_boot(plan)
            if not plan['changed']:
                self.status = 'Overlay settings already configured'
                return False
            if self.confirm_yes('Write overlay settings? Backup will be created. No reboot.'):
                self.service.apply_boot_plan(plan, confirmed=True)
            else:
                self.status = 'Boot write cancelled'
                return False
        self.perform(operation)

    def view_boot(self):
        def operation():
            info = self.service.boot_info()
            self.view_text(info['path'] + '\nPrefix: ' + (info['overlay_prefix'] or 'none') + '\n\n' + info['content'])
        self.perform(operation)

    def preview_boot(self, plan):
        diff = ''.join(difflib.unified_diff(plan['before'].splitlines(keepends=True),
                                          plan['after'].splitlines(keepends=True),
                                          fromfile='current', tofile='proposed'))
        self.view_text(plan['target'] + '\nPrefix: ' + (plan['overlay_prefix'] or 'none') + '\n\n' + (diff or 'No changes'))

    def view_text(self, text):
        position = 0
        while True:
            h, w = self.stdscr.getmaxyx()
            lines = []
            for line in text.splitlines():
                lines.extend(textwrap.wrap(line, max(1, w-2), replace_whitespace=False, drop_whitespace=False) or [''])
            visible = max(1, h-2)
            position = max(0, min(position, max(0, len(lines)-visible)))
            self.stdscr.erase()
            for row, line in enumerate(lines[position:position+visible]):
                try:
                    self.stdscr.addnstr(row, 1, line, max(1, w-2))
                except curses.error:
                    pass
            try:
                self.stdscr.addnstr(h-1, 0, 'Up/Down scroll  PgUp/PgDn  q/Esc close', max(1, w-1), curses.A_DIM)
            except curses.error:
                pass
            self.stdscr.refresh()
            ch = self.stdscr.getch()
            if ch == 3:
                raise KeyboardInterrupt
            if ch in (27, ord('q'), ord('Q')):
                return
            if ch in (curses.KEY_DOWN, ord('j')):
                position += 1
            elif ch in (curses.KEY_UP, ord('k')):
                position -= 1
            elif ch == curses.KEY_NPAGE:
                position += visible
            elif ch == curses.KEY_PPAGE:
                position -= visible

    def choose_profile(self,title):
        boards = self.service.list_profiles()
        if not boards:
            self.popup("Local board database is empty."); return None
        pos=0
        while True:
            h,w=self.stdscr.getmaxyx()
            ph=min(max(8,len(boards)+5),max(8,h-2))
            pw=min(max(50, min(w-2, 78)), w-2)
            y0=max(0,(h-ph)//2); x0=max(0,(w-pw)//2)
            win=curses.newwin(ph,pw,y0,x0); win.keypad(True)
            win.erase(); win.box()
            try: win.addnstr(1,2,title,max(1,pw-4),curses.A_BOLD)
            except curses.error: pass
            visible=max(1,ph-4)
            top=max(0,min(pos-visible+1,max(0,len(boards)-visible)))
            for row,i in enumerate(range(top,min(len(boards),top+visible)),start=2):
                b=boards[i]
                text=f"ID={b.get('id')} rev={b.get('rev')}  {b.get('name')}"
                attr=curses.A_REVERSE if i==pos else curses.A_NORMAL
                try: win.addnstr(row,2,text,max(1,pw-4),attr)
                except curses.error: pass
            try: win.addnstr(ph-2,2,"Enter select   Esc/q cancel",max(1,pw-4),curses.A_DIM)
            except curses.error: pass
            win.refresh()
            ch=win.getch()
            if ch==3: raise KeyboardInterrupt
            if ch in (27,ord("q"),ord("Q")):
                self.status="Board load cancelled" if "Load" in title else "Selection cancelled"
                return None
            if ch in (curses.KEY_UP,ord("k")): pos=(pos-1)%len(boards)
            elif ch in (curses.KEY_DOWN,ord("j")): pos=(pos+1)%len(boards)
            elif ch in (10,13,curses.KEY_ENTER): return boards[pos]

    def profile_load(self):
        def operation():
            profile = self.choose_profile('Load board from local DB')
            if profile and self.confirm_yes(f"Load {profile['name']}? Replace board config; keep serial, date and MACs."):
                return self.service.apply_profile(self.cfg, profile, confirmed=True)
            self.status = 'Board load cancelled'
            return False
        self.perform(operation, update=True)

    def profile_add(self):
        if self.confirm_yes('Save current board profile to local DB?'):
            self.perform(lambda: self.service.save_profile(self.cfg, confirmed=True))
        else:
            self.status = 'Local DB save cancelled'

    def profile_delete(self):
        def operation():
            profile = self.choose_profile('Delete board from local DB')
            if profile and self.confirm_yes(f"Delete {profile['name']} from local DB?"):
                self.service.delete_profile(profile, confirmed=True)
            else:
                self.status = 'Delete cancelled'
                return False
        self.perform(operation)

    def popup(self,text,wait=True):
        lines=text.splitlines(); h,w=self.stdscr.getmaxyx()
        ph=min(max(6,h-2),max(8,len(lines)+4))
        pw=min(max(20,w-2),max(52,min(max((len(x) for x in lines),default=40)+4,w-2)))
        ph=min(ph,h); pw=min(pw,w)
        y0=max(0,(h-ph)//2); x0=max(0,(w-pw)//2)
        win=curses.newwin(ph,pw,y0,x0); win.box()
        for i,line in enumerate(lines[:max(0,ph-3)]):
            try: win.addnstr(i+1,2,line,max(1,pw-4))
            except curses.error: pass
        if wait:
            try: win.addnstr(ph-2,2,"Press any key",max(1,pw-4),curses.A_DIM)
            except curses.error: pass
        win.refresh()
        if wait:
            ch=win.getch()
            if ch==3: raise KeyboardInterrupt

    def perform(self, operation, update=False):
        try:
            result = operation()
            if result is False:
                return
            if update:
                self.cfg = result
                self.blob = None
            self.status = 'Operation completed'
        except (ValueError, TypeError, OSError, KeyError) as exc:
            self.status = str(exc)
            curses.beep()


def run(service):
    try:
        curses.wrapper(lambda screen: UI(screen, service).run())
    except KeyboardInterrupt:
        return 130
    return 0
