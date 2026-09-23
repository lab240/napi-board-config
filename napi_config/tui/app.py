import curses
import textwrap
from ..core.models import INTERFACES, IF_BY_KEY


class UI:
    ACTIONS = [("action", key) for key in (
        "view_eeprom", "read", "write_eeprom", "profile_load", "profile_add", "profile_delete",
        "github_db", "view_generate_macs", "write_env")]
    SERVICES = [("action", key) for key in (
        "load_defaults", "reset_eeprom", "reset_processor", "enable_i2c1_eeprom", "quit")]

    def __init__(self, stdscr, service):
        self.stdscr = stdscr
        self.service = service
        self.cfg, initial_status = service.initial_configuration()
        self.processor_info = service.processor_status()
        self.action_states = {}
        self.boot_summary = "Not detected"
        self.blob = None
        self.status = initial_status
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
            ("instance", "processor_binding"),
            ("instance", "mfg_date"),
            ("instance", "mac_count"),
            ("field", "env_target"),
        ]
        self.service_start = len(self.rows)
        self.rows += self.SERVICES

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
            if ch in (curses.KEY_LEFT, curses.KEY_RIGHT, ord("h"), ord("l")):
                if self.stdscr.getmaxyx()[1] >= 80:
                    if self.is_action_cursor() and ch in (curses.KEY_RIGHT, ord("l")):
                        self.cursor = self.service_start + min(self.cursor, len(self.SERVICES)-1)
                    elif self.cursor >= self.service_start and ch in (curses.KEY_LEFT, ord("h")):
                        self.cursor = self.cursor - self.service_start
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
            "view_eeprom": "[ View EEPROM ]",
            "read": "[ Load EEPROM ]",
            "write_eeprom": "[ Write EEPROM ]",
            "write_env": "[ View and write boot config ]",
            "profile_load": "[ Load board from local DB ]",
            "profile_add": "[ Save board to local DB ]",
            "profile_delete": "[ Delete board from local DB ]",
            "github_db": "[ Load DB from GitHub ]",
            "view_macs": "[ View MACs ]",
            "view_generate_macs": "[ View and generate MACs ]",
            "reset_processor": "[ Reset processor ID ]",
            "generate_macs": "[ Generate MACs ]",
            "enable_i2c1_eeprom": "[ Add EEPROM overlay and reboot ]",
            "view_boot": "[ View current boot config ]",
            "view_processor": "[ View processor binding ]",
            "bind_processor": "[ Bind processor ]",
            "rebind_processor": "[ Rebind processor ]",
            "migrate_eeprom": "[ Migrate EEPROM v2 to v3 ]",
            "write_processor": "[ View and write processor ID ]",
            "reset_eeprom": "[ Reset EEPROM ]",
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
            if key == 'processor_binding':
                info = self.processor_info
                if info['state'] == 'OTP UNAVAILABLE':
                    return 'Processor ID : OTP UNAVAILABLE'
                suffix = ('not in EEPROM' if info['state'] == 'NOT BOUND' else ''
                          if info['state'] == 'MATCH' else 'PROCESSOR MISMATCH; EEPROM: ' + info['stored_id'])
                return 'Processor ID : ' + info['current_id'] + (' (' + suffix + ')' if suffix else '')
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
                              for key in ('view_eeprom', 'read', 'write_eeprom', 'enable_i2c1_eeprom', 'view_boot', 'write_env',
                                          'reset_processor', 'reset_eeprom')}
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

        title = " NAPI Board Config v23 "
        try:
            s.addnstr(0, max(0,(w-len(title))//2), title, w-1, curses.A_BOLD)
        except curses.error:
            pass

        wide = w >= 80
        logical = []
        if wide:
            logical.append(("menuheaders", None, None))
            for r in range(max(len(self.ACTIONS), len(self.SERVICES))):
                ids = ([r] if r < len(self.ACTIONS) else [])
                if r < len(self.SERVICES):
                    ids.append(self.service_start + r)
                logical.append(("menurow", ids, None))
        else:
            logical.append(("section", "--- ACTIONS ---", None))
            logical.extend(("item", i, None) for i in range(len(self.ACTIONS)))
        logical.append(("section", "--- BOARD CONFIG ---", None))
        first_cfg = len(self.ACTIONS)
        env_index = next(i for i, x in enumerate(self.rows) if x == ("field", "env_target"))
        logical.extend(("item", i, None) for i in range(first_cfg, env_index))
        logical.append(("section", "--- BOOT CONFIGURATION ---", None))
        logical.append(("item", env_index, None))
        if not wide:
            logical.append(("section", "--- SERVICE ---", None))
            logical.extend(("item", i, None) for i in range(self.service_start, len(self.rows)))
        cursor_line = 0
        for n, (typ, val, _) in enumerate(logical):
            if (typ == "menurow" and self.cursor in val) or (typ == "item" and val == self.cursor):
                cursor_line = n

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
                elif typ == "menuheaders":
                    colw = (w-3)//2
                    s.addnstr(y, 1, "--- ACTIONS ---", colw-1, curses.A_BOLD)
                    s.addnstr(y, 1+colw, "--- SERVICE ---", colw-1, curses.A_BOLD)
                elif typ == "menurow":
                    colw = (w-3)//2
                    for idx in val:
                        column = 0 if idx < len(self.ACTIONS) else 1
                        attr = curses.A_REVERSE if idx == self.cursor else curses.A_NORMAL
                        s.addnstr(y, 1+column*colw, self.action_label(self.rows[idx][1]), colw-1, attr)
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
            if kind in ('iface', 'rtc'):
                try:
                    warnings = self.service.overlays(self.cfg)['warnings']
                    if warnings:
                        message = 'WARNING: ' + '; '.join(warnings)
                except (ValueError, OSError) as exc:
                    message = str(exc)
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
            "view_eeprom":self.view_eeprom,
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
            "view_generate_macs":self.view_generate_macs,
            "reset_processor":self.reset_processor,
            "enable_i2c1_eeprom":self.enable_i2c1_eeprom,
            "view_processor":self.view_processor,
            "bind_processor":lambda: self.change_instance('bind'),
            "rebind_processor":lambda: self.change_instance('rebind'),
            "migrate_eeprom":lambda: self.change_instance('migrate'),
            "write_processor":lambda: self.change_instance('write'),
            "reset_eeprom":self.reset_eeprom,
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
            setup = self.service.eeprom_setup(self.cfg.platform)
            if setup['available'] or not setup['candidates']:
                self.view_text(setup['message'])
                return False
            candidate = setup['preferred'] or self.choose_eeprom_overlay(setup['candidates'])
            if candidate is None:
                self.status = 'EEPROM setup cancelled'
                return False
            plan = self.service.boot_plan(self.cfg, eeprom_overlay=candidate['name'])
            if not plan['changed']:
                self.view_text('Overlay already configured. Check chip, bus and address. Reboot manually if changes are pending.')
                return False
            if not self.preview_boot(plan, offer_write=True):
                self.status = 'View only; boot file unchanged'
                return False
            if not self.confirm_yes(f"Enable {candidate['name']} and REBOOT? Confirm chip, bus and address match."):
                self.status = 'Cancelled'
                return False
            self.service.apply_boot_plan(plan, confirmed=True)
            self.service.reboot(confirmed=True)
        self.perform(operation)

    def choose_eeprom_overlay(self, candidates):
        position = 0
        while True:
            h, w = self.stdscr.getmaxyx()
            self.stdscr.erase()
            visible = max(1, h-4)
            top = max(0, position-visible+1)
            try:
                self.stdscr.addnstr(0, 0, 'EEPROM overlay candidates: verify chip/bus/address', max(1, w-1), curses.A_BOLD)
                for row, candidate in enumerate(candidates[top:top+visible], 2):
                    attr = curses.A_REVERSE if top+row-2 == position else curses.A_NORMAL
                    self.stdscr.addnstr(row, 1, candidate['file'], max(1, w-2), attr)
                self.stdscr.addnstr(h-1, 0, 'Up/Down  Enter select  q/Esc cancel', max(1, w-1))
            except curses.error:
                pass
            self.stdscr.refresh()
            ch = self.stdscr.getch()
            if ch == 3:
                raise KeyboardInterrupt
            if ch in (27, ord('q'), ord('Q')):
                return None
            if ch in (10, 13, curses.KEY_ENTER):
                return candidates[position]
            if ch in (curses.KEY_DOWN, ord('j')):
                position = (position+1) % len(candidates)
            elif ch in (curses.KEY_UP, ord('k')):
                position = (position-1) % len(candidates)

    def edit_instance(self, key):
        if key == 'processor_binding':
            self.view_processor()
            return
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
        offer_write = []
        def operation():
            plan = self.service.mac_plan(self.cfg)
            text = ('RK3308 OTP ID: ' + plan['otp_id'] + '\nMAC source: ' + plan['source']
                    + '\n\nCurrent configuration:\n' + ('\n'.join(self.service.mac_rows(self.cfg)) or 'No MAC addresses'))
            if plan['eeprom_current'] and plan['eeprom_current']['macs'] != plan['current']['macs']:
                stored = self.service.configuration_from_document(plan['eeprom_current'])
                text += '\n\nCurrent EEPROM:\n' + '\n'.join(self.service.mac_rows(stored))
            generated = self.service.configuration_from_document(plan['generated'])
            text += '\n\nGenerated:\n' + '\n'.join(self.service.mac_rows(generated))
            if plan['already_matches']:
                self.view_text(text + '\n\nMAC addresses already match this SoC.')
                self.last_mac_plan = plan
                self.status = 'MAC addresses already match this SoC.'
                offer_write.append(True)
                return False
            if self.cfg.macs or (plan['eeprom_current'] and plan['eeprom_current']['macs']):
                text += '\n\nMAC addresses already exist. Replacement requires confirmation.'
            if not self.view_text(text, offer_write=True, action_label='apply MACs'):
                self.status = 'MAC generation cancelled'
                return False
            if not self.confirm_yes('Regenerate MAC addresses in memory? EEPROM stays unchanged.'):
                self.status = 'MAC generation cancelled'
                return False
            candidate = self.service.apply_mac_plan(self.cfg, plan, confirmed=True)
            self.last_mac_plan = plan
            offer_write.append(True)
            return candidate
        self.perform(operation, update=True)
        if offer_write:
            self.offer_mac_eeprom_write()

    def offer_mac_eeprom_write(self):
        def operation():
            try:
                plan = self.service.mac_eeprom_plan(self.cfg.macs)
            except ValueError as exc:
                self.view_text('MACs remain in memory.\n\n' + str(exc))
                self.status = 'MACs in memory; MAC-only EEPROM write unavailable'
                return False
            if not plan['changed']:
                self.status = 'EEPROM MAC addresses already match; no write needed'
                return False
            comparison = self.service.mac_eeprom_comparison(plan)
            metadata = ''
            source = getattr(self, 'last_mac_plan', None)
            if source:
                metadata = 'RK3308 OTP ID: ' + source['otp_id'] + '\nMAC source: ' + source['source']
            if not self.view_comparison(comparison, metadata, action_label='write MACs to EEPROM'):
                self.status = 'MACs in memory; EEPROM write declined'
                return False
            if not self.confirm_yes('Write only MACs to EEPROM? mac_count and CRC are updated; other EEPROM fields are preserved.'):
                self.status = 'MACs in memory; EEPROM write cancelled'
                return False
            self.service.apply_mac_eeprom_plan(plan, confirmed=True)
            self.status = 'EEPROM MACs written and verified; other fields preserved'
            return False
        self.perform(operation)

    def view_generate_macs(self):
        info = self.service.processor_status()
        text = ('Current MACs:\n' + ('\n'.join(self.service.mac_rows(self.cfg)) or 'No MAC addresses')
                + '\n\nCurrent OTP ID: ' + (info['current_id'] or 'unavailable')
                + ('\n' + info['error'] if info['error'] else ''))
        if self.view_text(text, offer_write=True, action_label='generate MACs'):
            self.generate_macs()

    def reset_processor(self):
        def operation():
            plan = self.service.reset_processor_plan()
            if not plan['changed']:
                self.view_comparison(plan['comparison'], offer_write=False)
                self.status = 'Processor ID is already unset; no write performed'
                return False
            if not self.view_comparison(plan['comparison'], action_label='reset processor ID'):
                return False
            if not self.confirm_yes('Clear only processor ID? Other EEPROM fields are preserved. Backup is saved first.'):
                return False
            self.service.apply_reset_processor_plan(plan, confirmed=True)
            return self.service.reset_processor_configuration(self.cfg)
        self.perform(operation, update=True)

    def view_macs(self):
        rows = self.service.mac_rows(self.cfg)
        self.view_text('MAC addresses\n\n' + '\n'.join(rows) if rows else 'No MAC addresses generated')

    def view_processor(self):
        def operation():
            info = self.service.processor_status()
            self.view_text('Processor binding: ' + info['state']
                           + '\nStored ID: ' + (info['stored_id'] or 'not bound')
                           + '\nCurrent OTP ID: ' + (info['current_id'] or 'unavailable')
                           + ('\n' + info['error'] if info['error'] else ''))
        self.perform(operation)

    def change_instance(self, action):
        saved = []
        def operation():
            plan = (self.service.migration_plan() if action == 'migrate'
                    else self.service.processor_write_plan(self.cfg) if action == 'write'
                    else self.service.processor_plan(rebind=action == 'rebind'))
            if plan.get('writable') is False:
                self.view_text(plan['comparison']['title'] + '\nCurrent OTP ID: '
                               + plan['comparison']['rows'][0]['proposed'] + '\n\n' + plan['reason'])
                self.status = 'Processor ID viewed; EEPROM unchanged'
                return False
            if not plan['changed']:
                self.view_text('Current OTP ID: ' + plan['otp_id'].hex()
                               + '\nProcessor ID already matches EEPROM; no write needed.')
                return False
            if not self.view_comparison(plan['comparison'], action_label=action):
                self.status = 'View only; EEPROM unchanged'
                return False
            question = ('WRITE FULL displayed configuration and processor ID to empty EEPROM? Backup is saved first.'
                        if plan.get('kind') == 'initialize' else
                        plan['comparison']['title'] + '? EEPROM will be written; MACs and serial are preserved.')
            if not self.confirm_yes(question):
                self.status = 'EEPROM change cancelled'
                return False
            self.service.apply_processor_write_plan(plan, confirmed=True)
            saved.append(self.service.last_eeprom_backup)
            return self.service.update(self.service.read_eeprom(), 'comment', self.cfg.comment)
        self.perform(operation, update=True)
        if saved:
            self.status = 'EEPROM verified; backup: ' + saved[0]

    def reset_eeprom(self):
        completed = []
        def operation():
            plan = self.service.reset_eeprom_plan()
            if not self.view_comparison(plan['comparison'], action_label='reset EEPROM'):
                self.status = 'EEPROM reset cancelled'
                return False
            if not self.confirm_yes('RESET ALL 256 EEPROM BYTES? Current menu settings are preserved. Binary backup is saved first.'):
                self.status = 'EEPROM reset cancelled'
                return False
            self.service.apply_reset_eeprom_plan(plan, confirmed=True)
            completed.append(self.service.last_eeprom_backup)
            return False
        self.perform(operation)
        if completed:
            self.processor_info = self.service.processor_status()
            self.status = 'EEPROM empty; current settings preserved. Use Write EEPROM.'
            if completed[0]:
                self.status += ' Backup: ' + completed[0]

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

    def view_eeprom(self):
        def operation():
            self.view_text(self.service.eeprom_view())
            return False
        self.perform(operation)

    def read_eeprom(self):
        if self.confirm_yes('Load EEPROM? Current configuration in memory will be replaced.'):
            self.perform(self.service.read_eeprom, update=True)
        else:
            self.status = 'EEPROM load cancelled'

    def write_eeprom(self):
        def operation():
            write_plan = self.service.eeprom_write_plan(self.cfg)
            comparison = write_plan['comparison']
            metadata = ''
            plan = getattr(self, 'last_mac_plan', None)
            if plan and plan['generated']['macs'] == self.service.document(self.cfg)['macs']:
                metadata = 'RK3308 OTP ID: ' + plan['otp_id'] + '\nMAC source: ' + plan['source']
            if not self.view_comparison(comparison, metadata) or not self.confirm_yes('WRITE EEPROM with current configuration?'):
                self.status = 'EEPROM write cancelled'
                return False
            if not self.service.apply_eeprom_write_plan(write_plan, confirmed=True):
                self.status = 'EEPROM configuration already matches; no write performed'
                return False
            return self.service.update(self.service.read_eeprom(), 'comment', self.cfg.comment)
        self.perform(operation, update=True)

    def write_env(self):
        def operation():
            plan = self.service.boot_plan(self.cfg)
            comparison = self.service.boot_comparison(plan)
            metadata = plan['target'] + '\nPrefix: ' + (plan['overlay_prefix'] or 'none')
            if plan['warnings']:
                metadata += '\n\nWARNINGS (not written):\n' + '\n'.join(plan['warnings'])
            wants_write = self.view_comparison(comparison, metadata, action_label='write boot config',
                                               offer_write=plan['changed'])
            if not plan['changed']:
                self.status = 'Overlay settings already configured'
                return False
            if not wants_write:
                self.status = 'View only; boot file unchanged'
                return False
            if self.confirm_yes('Write proposed boot config? Backup will be created.'):
                backup = self.service.apply_boot_plan(plan, confirmed=True)
                if backup is not None and self.confirm_yes('Boot config saved and verified. Reboot now?'):
                    self.service.reboot(confirmed=True)
                self.status = 'Boot config saved; changes take effect after reboot'
                return False
            else:
                self.status = 'Boot write cancelled'
                return False
        self.perform(operation)

    def view_boot(self):
        self.write_env()

    def preview_boot(self, plan, offer_write=False):
        # Presentation only; parsing and string generation belong to Core.
        text = plan['target'] + '\nPrefix: ' + (plan['overlay_prefix'] or 'none')
        if plan.get('warnings'):
            text += '\n\nWARNINGS (not written to boot file):'
            for warning in plan['warnings']:
                text += '\n' + warning
        if not plan['changed']:
            text += '\n\nNo changes'
        text += ('\n\nCURRENT:\n' + plan['current_overlay_string']
                + '\n' + plan['current_user_overlay_string']
                + '\n\nPROPOSED:\n' + plan['proposed_overlay_string']
                + '\n' + plan['proposed_user_overlay_string'])
        return self.view_text(text, offer_write=offer_write)

    def view_comparison(self, comparison, metadata='', action_label='write', offer_write=True):
        position = 0
        changed_attr = curses.A_BOLD
        try:
            if curses.has_colors():
                try:
                    curses.use_default_colors()
                    curses.init_pair(1, curses.COLOR_YELLOW, -1)
                except curses.error:
                    curses.init_pair(1, curses.COLOR_YELLOW, curses.COLOR_BLACK)
                changed_attr |= curses.color_pair(1)
        except curses.error:
            pass
        while True:
            h, w = self.stdscr.getmaxyx()
            width = max(1, w-2)
            lines = []

            def wrap(value, size):
                return textwrap.wrap(value, max(1, size), replace_whitespace=False,
                                     drop_whitespace=False) or ['']

            def add_text(value, attr=curses.A_NORMAL):
                for line in value.splitlines():
                    lines.extend([[(1, part, attr)] for part in wrap(line, width)])

            add_text(comparison['title'], curses.A_BOLD)
            if metadata:
                add_text(metadata)
            if comparison['error']:
                add_text(comparison['error'], changed_attr)
            add_text(comparison['note'])
            add_text('* changed value', changed_attr)
            add_text('')
            if w >= 80:
                label_width = min(28, max(len(row['field']) for row in comparison['rows']) + 2)
                value_width = max(1, (width - label_width - 6) // 2)
                proposed_x = 1 + label_width + 2
                current_x = proposed_x + value_width + 2
                lines.append([(1, 'Field', curses.A_BOLD), (proposed_x, comparison.get('proposed_label', 'Proposed EEPROM'), curses.A_BOLD),
                              (current_x, comparison.get('current_label', 'Current EEPROM'), curses.A_BOLD)])
                lines.append([(1, '-' * width, curses.A_DIM)])
                for row in comparison['rows']:
                    label = ('* ' if row['changed'] else '  ') + row['field']
                    cells = [wrap(label, label_width), wrap(row['proposed'], value_width),
                             wrap(row['current'], value_width)]
                    for index in range(max(map(len, cells))):
                        segments = []
                        for cell, x, attr in zip(cells, (1, proposed_x, current_x),
                                                 (curses.A_NORMAL,
                                                  changed_attr if row['changed'] else curses.A_NORMAL,
                                                  curses.A_NORMAL)):
                            if index < len(cell):
                                segments.append((x, cell[index], attr))
                        lines.append(segments)
            else:
                for heading, key in [(comparison.get('proposed_label', 'Proposed EEPROM') + ':', 'proposed'),
                                     (comparison.get('current_label', 'Current EEPROM') + ':', 'current')]:
                    add_text(heading, curses.A_BOLD)
                    for row in comparison['rows']:
                        changed = key == 'proposed' and row['changed']
                        prefix = '* ' if changed else ''
                        add_text(prefix + row['field'] + ': ' + row[key],
                                 changed_attr if changed else curses.A_NORMAL)
                    add_text('')
            visible = max(1, h-2)
            position = max(0, min(position, max(0, len(lines)-visible)))
            self.stdscr.erase()
            for y, segments in enumerate(lines[position:position+visible]):
                for x, line, attr in segments:
                    try:
                        self.stdscr.addnstr(y, x, line, max(1, w-x-1), attr)
                    except curses.error:
                        pass
            try:
                footer = ('Enter: confirm ' + action_label if offer_write else 'Enter: close') + '  q/Esc: cancel  PgUp/PgDn: scroll'
                self.stdscr.addnstr(h-1, 0, footer, max(1, w-1), curses.A_DIM)
            except curses.error:
                pass
            self.stdscr.refresh()
            ch = self.stdscr.getch()
            if ch == 3:
                raise KeyboardInterrupt
            if ch in (27, ord('q'), ord('Q')):
                return False
            if ch in (10, 13, curses.KEY_ENTER):
                return offer_write
            if ch in (curses.KEY_DOWN, ord('j')):
                position += 1
            elif ch in (curses.KEY_UP, ord('k')):
                position -= 1
            elif ch == curses.KEY_NPAGE:
                position += visible
            elif ch == curses.KEY_PPAGE:
                position -= visible

    def view_text(self, text, offer_write=False, action_label='write'):
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
                footer = 'Enter: confirm ' + action_label + '  q/Esc: view only' if offer_write else 'Up/Down scroll  PgUp/PgDn  q/Esc close'
                self.stdscr.addnstr(h-1, 0, footer, max(1, w-1), curses.A_DIM)
            except curses.error:
                pass
            self.stdscr.refresh()
            ch = self.stdscr.getch()
            if ch == 3:
                raise KeyboardInterrupt
            if ch in (27, ord('q'), ord('Q')):
                return False
            if offer_write and ch in (10, 13, curses.KEY_ENTER):
                return True
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
                self.processor_info = self.service.processor_status()
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
