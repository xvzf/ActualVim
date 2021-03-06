import queue
import sublime
import sublime_plugin
import threading
import traceback

from . import neo
from . import settings
from .edit import Edit


def copy_sel(sel):
    if isinstance(sel, sublime.View):
        sel = sel.sel()
    return [(r.a, r.b) for r in sel]

# called by neo.py once neovim is loaded
def neovim_loaded():
    # ensure we have the newest neo module
    global neo
    from . import neo

    if settings.enabled():
        ActualVim.enable()

def neovim_unloaded():
    if neo._loaded and settings.enabled():
        ActualVim.enable(False)

try:
    _views
except NameError:
    _views = {}


class ActualVim:
    def __init__(self, view):
        if view.settings().get('actual_proxy'):
            return

        self.busy = threading.RLock()
        self.keyq = queue.Queue()

        self.view = view
        self.last_sel = None
        self.buf = None
        self.sub_changes = None
        self.vim_changes = None
        self.screen_changes = 0
        self.last_highlights = None
        self.last_status = None
        self.last_size = None
        self.block = False
        self.block_hit = False

        # first scroll is buggy
        self.first_scroll = True

        # settings are marked here when applying mode-specific settings, and erased after
        self.tmpsettings = []

        # track last settings we synced to vim, so we can update vim on change
        self.last_settings = None

        # tracks our drag_select type
        self.drag_select = None

        # tracks popup menu status
        self.popup = None

        en = settings.enabled()
        s = {
            'actual_intercept': en,
            'actual_mode': en,
            # it's most likely a buffer will start in command mode
            'inverse_caret_state': en,
        }
        for k, v in s.items():
            view.settings().set(k, v)

        lfd = settings.get('large_file_disable')
        bytes = lfd.get('bytes', -1)
        lines = lfd.get('lines', -1)
        # TODO: view.lines() could be slow here
        # hopefully view.size() shortcuts it
        if (0 < bytes < view.size()) or (0 < lines < len(view.lines(sublime.Region(0, view.size())))):
            fn = view.file_name() or view.name() or 'untitled'
            print('ActualVim: disabling input for "{}" as size exceeds "large_file_disable" setting'.format(fn))
            view.settings().set('actual_intercept', False)

    @classmethod
    def get(cls, view, create=True, exact=True):
        vid = view.id()
        m = _views.get(vid)
        if not m and create:
            try:
                m = cls(view)
            except Exception:
                traceback.print_exc()
                return
            _views[vid] = m
        elif m and exact and m.view != view:
            # TODO: ...what is this solving?
            return None
        return m

    @classmethod
    def remove(cls, view):
        _views.pop(view.id(), None)

    @classmethod
    def reload_classes(cls):
        # reload classes by creating a new blank instance without init and overlaying dicts
        for vid, view in _views.items():
            new = cls.__new__(cls)
            nd = {}
            # copy view dict first to get attrs, new second to get methods
            nd.update(view.__dict__)
            nd.update(new.__dict__)
            new.__dict__.update(nd)
            _views[vid] = new

    @classmethod
    def enable(cls, enable=True):
        for av in _views.values():
            s = av.view.settings()
            s.set('actual_intercept', enable)
            s.set('actual_mode', enable)

        # TODO: cursor isn't adjusted here, not sure why
        # (so if it's on a newline, it will stay there when caret switches)
        av = cls.get(sublime.active_window().active_view())
        if av:
            if av.actual:
                av.activate()
                av.sel_to_vim()
                av.sel_from_vim()
            av.update_view()

    @property
    def actual(self):
        return neo._loaded and self.view and self.settings.get('actual_mode') and self.settings.get('actual_intercept')

    def sel_changed(self):
        new_sel = copy_sel(self.view)
        changed = new_sel != self.last_sel
        self.last_sel = new_sel
        return changed

    def vim_text_point(self, row, col):
        view = self.view
        pos = view.text_point(row, 0)
        line = view.substr(sublime.Region(pos, pos + col))
        vcol = len(line.encode('utf-8')[:col].decode('utf-8'))
        return view.text_point(row, vcol)

    def vim_rowcol(self, point):
        view = self.view
        row, col = view.rowcol(point)
        line = view.substr(sublime.Region(point - col, point))
        vcol = len(line[:col].encode('utf-8'))
        return row, vcol

    def visual(self, mode, a, b):
        view = self.view
        regions = []
        sr, sc = a
        er, ec = b

        a = self.vim_text_point(sr, sc)
        b = self.vim_text_point(er, ec)

        name = neo.MODES.get(mode)
        if not name:
            print('ActualVim warning: unhandled selection mode', repr(mode))

        if name == 'visual line':
            # visual line mode
            if a > b:
                start = view.line(a).b
                end = view.line(b).a
            else:
                start = view.line(a).a
                end = view.line(b).b
            regions.append((start, end))
        elif name == 'visual':
            # visual mode
            if a > b:
                a += 1
            else:
                b += 1
            regions.append((a, b))
        elif name == 'visual block':
            # visual block mode
            left = min(sc, ec)
            right = max(sc, ec) + 1
            top = min(sr, er)
            bot = max(sr, er)
            end = view.text_point(top, right)

            for i in range(top, bot + 1):
                line = view.line(view.text_point(i, 0))
                _, end = view.rowcol(line.b)
                if left <= end:
                    a = view.text_point(i, left)
                    b = view.text_point(i, min(right, end))
                    if sc > ec:
                        a, b = b, a
                    regions.append((a, b))
        else:
            regions.append((a, b))

        return [sublime.Region(*r) for r in regions]

    @property
    def changed(self):
        return any((
            self.sub_changes is None or self.sub_changes < self.view.change_count(),
            # "revert" changes size without increasing change count
            self.last_size is not None and self.last_size != self.view.size(),
        ))

    def mark_changed(self, advance=0):
        self.sub_changes = self.view.change_count() + advance
        if advance:
            self.last_size = None
        else:
            self.last_size = self.view.size()

    @property
    def settings(self):
        return self.view.settings()

    @property
    def avsettings(self):
        top = settings.get('settings', {})

        base = {}
        if not self.actual:
            combined = top.get('sublime', {})
            base['settings'] = combined
        else:
            combined = top.get('vim', {})
            modes = combined.pop('modes')
            mode = neo.vim.mode
            name = neo.MODES.get(mode)
            combined.update(modes.get(name, {}))
            if mode in neo.VISUAL_MODES:
                combined.update(modes.get('all visual', {}))
            elif mode in neo.INSERT_MODES:
                combined.update(modes.get('all insert', {}))

            base['settings'] = combined
            base['bell'] = combined.pop('bell')
        return base

    def activate(self):
        if not neo._loaded: return
        neo.vim.force_ready()
        # first activate
        if self.buf is None:
            self.buf = neo.vim.buf_new(self)
            # disable undo on first insert
            self.buf.options['undolevels'] = -1
            self.sync_to_vim()
            # re-enable undo
            self.buf.options['undolevels'] = -123456
            path = self.view.file_name()
            if path:
                self.set_path(path)

        if neo.vim.activate(self):
            self.status_from_vim()
            self.update_view()
            self.highlight()

    def update_view(self):
        combined = self.avsettings.get('settings', {})
        for k in self.tmpsettings:
            self.settings.erase(k)
        self.tmpsettings = combined.keys()

        for k, v in combined.items():
            self.settings.set(k, v)

        vp = self.view.viewport_extent()
        width, height = vp[0] / self.view.em_width(), vp[1] / self.view.line_height()
        if self.actual:
            neo.vim.resize(width, height)
            # update_view is called all the time, and asking vim for things is expensive
            # so vim's tab priority comes automatically during sel_from_vim()
            if settings.get('indent_priority') == 'sublime':
                self.settings_to_vim()

    def settings_to_vim(self):
        # only send this to vim if something changes
        tmp = {name: self.settings.get(name) for name in ('translate_tabs_to_spaces', 'tab_size')}
        tmp['read_only'] = self.view.is_read_only()
        if tmp != self.last_settings:
            if tmp['translate_tabs_to_spaces']:
                neo.vim.cmd('set expandtab ts={ts} shiftwidth={ts} softtabstop=0 smarttab'.format(ts=tmp['tab_size']))
            else:
                neo.vim.cmd('set noexpandtab softtabstop=0')
            if tmp['read_only']:
                neo.vim.cmd('set noma')
            else:
                neo.vim.cmd('set ma')
        neo.vim.status(force=True)

    def settings_from_vim(self, et, ts):
        if et:
            self.settings.set('translate_tabs_to_spaces', et)
        self.settings.set('tab_size', ts)

    def sync_to_vim(self, force=False):
        if not neo._loaded: return
        if self.block:
            self.block_hit = True
            return

        if not (self.changed or force) or not self.buf:
            if self.last_size is None:
                self.last_size = self.view.size()
            return

        self.mark_changed()
        neo.vim.force_ready()
        text = self.view.substr(sublime.Region(0, self.view.size())).split('\n')
        self.buf[:] = text
        self.sel_to_vim(force)
        self.vim_changes = neo.vim.buf_tick(self.buf)

    def sync_from_vim(self, edit=None):
        if not neo._loaded: return
        if not self.actual: return

        def update(view, edit):
            with self.busy:
                # only sync text content if vim buffer changed
                # TODO: change to buf.vars['changedtick'] when neovim master (0.2.0?) is stable
                # TODO: batch this with sel/status?
                tick = neo.vim.buf_tick(self.buf)
                if self.vim_changes is None or tick > self.vim_changes:
                    self.vim_changes = tick
                    # TODO: global UI change is GROSS, do deltas if possible
                    text = '\n'.join(self.buf[:])
                    sel = view.sel()
                    r = sel[0]
                    for s in list(sel)[1:]:
                        r = r.cover(s)
                    view.replace(edit, sublime.Region(r.begin(), view.size()), text[r.begin():])
                    view.replace(edit, sublime.Region(0, r.begin()), text[:r.begin()])

                self.mark_changed()
                self.sel_from_vim(edit=edit)
                self.status_from_vim()

        if edit:
            update(self.view, edit)
        else:
            Edit.defer(self.view, update)

    def sel_to_vim(self, force=False):
        if not neo._loaded: return
        if not self.actual: return
        if self.sel_changed() and not self.changed:
            neo.vim.force_ready()
            # single selection for now...
            # TODO: block
            # TODO multiple select vim plugin integration
            sel = self.view.sel()[0]
            vim = neo.vim
            b = self.vim_rowcol(sel.b)
            b = (b[0] + 1, b[1] + 1)

            mode = 'v'
            if self.drag_select == 'lines':
                mode = 'V'

            if self.drag_select == 'columns':
                mode = '<c-v>'
                sel = self.view.sel()
                first, last = sel[0], sel[-1]
                a = self.vim_rowcol(last.a)
                b = self.vim_rowcol(first.b)
                a = (a[0] + 1, a[1] + 1)
                b = (b[0] + 1, b[1] + 1)
                if b[1] > a[1]:
                    b = (b[0], b[1] - 1)
                vim.select(a, b, mode=mode)
            else:
                if sel.b == sel.a:
                    vim.select(b)
                else:
                    a = self.vim_rowcol(sel.a)
                    a = (a[0] + 1, a[1] + 1)
                    if a > b:
                        a = (a[0], a[1] - 1)
                    elif b > a:
                        b = (b[0], b[1] - 1)
                    if self.drag_select == 'lines':
                        mode = 'V'
                        if a > b:
                            a = (a[0] - 1, a[1])
                        else:
                            b = (b[0] - 1, b[1])
                    vim.select(a, b, mode=mode)

            self.sel_from_vim()
            self.update_view()

    def sel_from_vim(self, edit=None):
        if not neo._loaded: return
        if not self.actual: return

        status = neo.vim.status()
        a = (status['vline'], status['vcol'])
        b = (status['cline'], status['ccol'])

        if settings.get('indent_priority') == 'vim':
            self.settings_from_vim(status['expandtab'], status['ts'])
        new_sel = self.visual(status['mode'], a, b)

        def select(view, edit):
            sel = view.sel()
            sel.clear()
            sel.add_all(new_sel)
            self.sel_changed()

            # defer first scroll: vis detection seems buggy during load
            if self.first_scroll:
                self.first_scroll = False
                sublime.set_timeout(self.sel_from_vim, 50)
                return

            # make sure new selection is visible
            vis = view.visible_region()
            if len(sel) == 1:
                b = sel[0].b
                lines = view.lines(vis)
                # single cursor might be at edge of screen, make sure line is fully on screen
                if not vis.contains(b) or (lines[0].contains(b) or lines[-1].contains(b)):
                    view.show(b, show_surrounds=False)
            else:
                for cur in sel:
                    if vis.contains(cur.b):
                        break
                else:
                    view.show(sel)

        if edit:
            select(self.view, edit)
        else:
            Edit.defer(self.view, select)

    def status_from_vim(self):
        status = neo.vim.status_line
        if status:
            self.view.set_status('actual', status)
        else:
            self.view.erase_status('actual')

    def press(self, key, edit=None):
        if not neo._loaded: return
        if self.buf is None:
            return

        self.keyq.put(key)
        # process the key, then all buffered keys
        with self.busy:
            key = self.keyq.get()
            _, ready = neo.vim.press(key)
            if ready:
                # TODO: trigger UI update on vim event, not here?
                # well, if we don't figure it out before returning control
                # to sublime, we get more events from sublime to figure out if we need to ignore
                self.sync_from_vim(edit=edit)
                # (trigger this somewhere else? vim mode change callback?)
                self.update_view()
            return ready

    def close(self):
        if neo._loaded:
            neo.vim.force_ready()
            if self.buf is not None:
                neo.vim.buf_close(self.buf)
        ActualVim.remove(self.view)

    def set_path(self, path):
        self.buf.name = path

    # neovim event callbacks
    def on_bell(self):
        bell = self.avsettings.get('bell', {})
        duration = bell.pop('duration', None)
        if duration:
            def remove_bell():
                for name in bell.keys():
                    self.settings.erase(name)
                self.update_view()

            for k, v in bell.items():
                self.settings.set(k, v)
            sublime.set_timeout(remove_bell, int(duration * 1000))

    def on_popupmenu(self, cmd, args):
        def html_escape(s):
            return s.replace('&', '&amp;').replace('<', '&lt;')

        def render(update=False):
            if self.popup:
                template = '''
                    <style>
                        .actualvim-popup-item-selected {{
                            background-color: color(var(--background) blend(grey 80%));
                        }}
                        .actualvim-popup-item {{
                            padding: 6px 14px 6px 14px;
                        }}
                        html, body, #actualvim-popup {{
                            padding: 0;
                            margin: 0;
                        }}
                    </style>
                    <div id="actualvim-popup">
                    {items}
                    </div>
                '''
                # TODO: use item['kind']?
                item_template = '''
                <div class="actualvim-popup-item{classes}">{text}</div>
                '''
                items = []
                for i, item in enumerate(self.popup['items']):
                    item = item.copy()
                    item['classes'] = ''
                    if i == self.popup['selected']:
                        item['classes'] = ' actualvim-popup-item-selected'
                    items.append(item_template.format_map(item))
                html = template.format(items='\n'.join(items))
                if self.view.is_popup_visible() and update:
                    self.view.update_popup(html)
                else:
                    self.view.show_popup(html, 0, -1, 300, 600, None, None)

        if cmd == 'popupmenu_show':
            items, selected, row, col = args[0]
            items = [{'text': html_escape(item[0]), 'kind': html_escape(item[1])} for item in items]
            self.popup = {'items': items, 'selected': selected, 'pos': (row, col)}
            render(update=False)
        elif cmd == 'popupmenu_hide':
            self.view.hide_popup()
        elif cmd == 'popupmenu_select':
            if self.popup:
                self.popup['selected'] = args[0][0]
            render(update=True)

    def on_write(self):
        self.view.run_command('save')

    def on_complete(self, findstart, base):
        def cur():
            modified, et, ts, a, b = neo.vim.status()
            sel = self.visual(neo.vim.mode, a, b)
            return sel[0].b

        if int(findstart):
            word = self.view.word(cur())
            r, c = self.vim_rowcol(word.a)
            return c

        loc = cur()
        completions, flags = sublime_plugin.on_query_completions(self.view.id(), base, [loc])
        if not flags & sublime.INHIBIT_WORD_COMPLETIONS:
            completions += self.view.extract_completions(base)

        # TODO: .sublime-completion support?
        if not flags & sublime.INHIBIT_EXPLICIT_COMPLETIONS:
            pass

        return completions

    def highlight(self, highlights=None):
        if not settings.get('highlights', False):
            return

        highlights = highlights or self.last_highlights
        if not highlights:
            return

        # TODO: autocmd VimResized?
        # TODO: split views?
        # TODO: allow configuring scope ("colormap")
        status = neo.vim.status(False)
        if not status:
            return

        def filt(h):
            whitelist = {'background', 'underline', 'reverse'}
            return all((
                set(h.highlight.keys()).intersection(whitelist),
                h.line < status['wheight'],
            ))
        highlights = tuple(filter(filt, highlights))

        wview = status['wview']
        lineoff = wview['topline'] - wview['topfill'] - 1
        coloff = wview['leftcol'] - wview['skipcol']
        if highlights == self.last_highlights:
            return
        self.last_highlights = highlights

        regions = []
        lines = {
            (line + lineoff): self.buf[line + lineoff]
            for line in {hl.line for hl in highlights}
        }
        for hl in highlights:
            line = hl.line + lineoff
            start = hl.start + coloff
            end = hl.end + coloff
            # fix tabs
            if not status['expandtab']:
                fix = lambda pos: pos - lines[line][:pos].count('\t') * (status['ts'] - 1)
                start, end = fix(start), fix(end)
            a = self.view.text_point(line, start)
            b = self.view.text_point(line, end)
            regions.append(sublime.Region(a, b))

        if regions:
            self.view.add_regions('actualvim_highlight', regions, 'error', '', sublime.DRAW_NO_OUTLINE)
        else:
            self.view.erase_regions('actualvim_highlight')

    def on_redraw(self, data, screen):
        if screen.changes <= self.screen_changes:
            return
        self.screen_changes = screen.changes
        hl = screen.highlights()
        sublime.set_timeout(lambda: self.highlight(hl), 0)

ActualVim.reload_classes()
