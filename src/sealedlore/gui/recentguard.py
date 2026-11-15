"""GTK's recent files list, left as it was by the app's own file choosers.

GTK's file chooser adds every file chosen in it to the desktop's recent files list,
$XDG_DATA_HOME/recently-used.xbel: a record on the disk of what was attached, saved or
opened from the app, there for every other program to read. It runs in the app's own
process - a Gtk.FileChooserNative too, wherever GTK shows its own dialog rather than the
desktop portal's: outside a sandbox, and with GTK_USE_PORTAL=0, which the app sets where
the desktop asks for the portal (the portal refuses a process that isn't dumpable) - through
the process's one Gtk.RecentManager (GTK 3.24.41's source, gtkrecentmanager.c and
gtkfilechooserwidget.c):

  - a chooser that ends with files chosen adds them to the manager's model and marks it to
    be written; 250 ms later the manager emits "changed", and the signal's class handler
    writes the whole list from the model - an entry older than gtk-recent-files-max-age
    (30 days) dropped, everything in GTK's own layout, a link in the list's place replaced
    by a file (GLib's g_file_set_contents), the file left 0600;
  - the same "changed", unmarked, is how the manager re-reads the list after another
    program wrote it (a file monitor): nothing is written then;
  - and as the main loop ends (Gtk.main at level 0, a Gtk.Application's shutdown), the
    manager, if it was ever made, writes the list from its model once more
    (_gtk_recent_manager_sync), with no signal.

Switching recent files off for the app (gtk-recent-files-enabled, or a max age of 0) is no
way out: GTK then writes an empty list over the user's, every program's entries gone,
whenever a chooser adds a file or the setting changes while the manager exists.

So the list is put back instead (Keeper), whatever GTK's writes in this process changed:

  - when one of the app's choosers answers with files chosen (run, answer_file_input,
    guard_pywebview: as it answers, for GTK adds them then), the list as it is - its bytes,
    or that there is none - is the clean copy, and a write is pending;
  - but GTK can have written them already: a file named in a Save dialog's entry (or a
    place typed into its location bar) is checked asynchronously, and the answer then comes
    by the dialog's default button, which GTK holds pressed for 250 ms (GtkButton's
    activate timeout) after adding the file - while a "changed" already on its way (the
    manager's 250 ms timer, started when another program changed the list, or when the
    list was put back after a choice of the app's a moment before) writes the list with it.
    So from the moment a chooser opens until it answers, every list seen is kept (opened,
    closed: the same three paths), and where the list as the answer comes already holds a
    file chosen as added since the chooser opened, the clean copy is the last list seen that
    holds none of them, and the list is put back at once;
  - after each "changed" (connect_after: after GTK's own write), with a write pending a
    list that isn't the clean copy is put back, or removed where there was none; with none
    pending (another program's change, re-read), the list is the new clean copy. A write
    stays pending until the manager holds every file chosen as added by this app since the
    choice - GTK adds them asynchronously, and may write more than once - or, where GTK never
    takes them in (recent files switched off by the user), HOLD_SECONDS after the last
    choice;
  - after the main loop ends (put_back, called once Gtk.main() or app.run() returns), a
    list that isn't the clean copy is put back.

Putting back: the bytes compared whole; written whole (a temp file of a name of its own
beside the list, fsynced, os.replace'd, the folder fsynced) with the mode the list had; a
list that was a link made a link to the same place again, and written through to what it
names only when that is a regular file under $XDG_DATA_HOME (a link to /dev/null, say, is
made again, and /dev/null never written). Nothing outside $XDG_DATA_HOME
(~/.local/share when it isn't set) is ever written, and nothing at all where GTK's list is
not $XDG_DATA_HOME/recently-used.xbel. A failure is said on stderr and never fails the
app: no exception, no dialog.

The known limit: a program writing the list while the app's own write is pending (in the
quarter of a second after a choice, or as the app quits, before GTK has read it again)
loses that write - GTK's own write would already have lost it.

And a chooser shown on those paths fits the screen (fit): GTK sizes one from the font and
the size it saved last, and on a 1280x800 screen, or under a tiling compositor (Hyprland),
its header bar - Cancel, Open, the location bar - was off the screen. While one of the app's
choosers is open, every Gtk.FileChooserDialog realized (the app's own, or the one GTK makes
for a Gtk.FileChooserNative) is held to the work area of the monitor it opens on, less a
margin (geometry hints, which GTK holds its own size to and tells a Wayland compositor), and
where the app may place it (X11) moved to lie inside that area.

This is SealedLore's copy (src/sealedlore/gui/recentguard.py) of DA Assistant's
(assistant/recentguard.py), the canonical one, from which it differs only in this paragraph
(the DA apps' security standard, Nov 2026). SealedLore is a Qt app with no PyGObject: only
the Keeper is used, told of each choice and of each write by gui/file_dialogs.py, and the
functions here that would import GTK are never called.
"""

from __future__ import annotations

import math
import os
import secrets
import stat
import sys
import tempfile
import time
from datetime import datetime
from html import unescape
from urllib.parse import unquote, urlsplit

RECENT_FILES = "recently-used.xbel"
# The most of the list read: GTK keeps at most a thousand entries, a few hundred KB.
MOST = 32 * 1024 * 1024
# How long a write stays pending after a choice whose files GTK never takes in.
HOLD_SECONDS = 30
# The most lists kept while a chooser is open: the one it opened on, and the newest.
SEEN = 8
# A chooser fits the work area of the monitor it opens on, this share of the area's width
# and height (and at least FIT_MARGIN_MIN pixels) left free on each side.
FIT_MARGIN = 0.05
FIT_MARGIN_MIN = 16

ABSENT, FILE, LINK, OTHER = "absent", "file", "link", "other"


def _say(text):
    try:
        print(f"[recent files] {text}", file=sys.stderr, flush=True)
    except (OSError, ValueError):
        pass


def data_home(env=None):
    """$XDG_DATA_HOME where it is an absolute path, else ~/.local/share (as GLib reads it)."""
    env = os.environ if env is None else env
    value = env.get("XDG_DATA_HOME") or ""
    if os.path.isabs(value):
        return os.path.normpath(value)
    home = env.get("HOME") or os.path.expanduser("~")
    return os.path.normpath(os.path.join(home, ".local", "share"))


def _inside(path, folder):
    path, folder = os.path.realpath(path), os.path.realpath(folder)
    return os.path.commonpath([path, folder]) == folder


def _read_regular(path):
    """(bytes, mode) of the regular file at `path` - a link at its last name not followed -
    or None for anything else, a file past MOST among them."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return None
        chunks, size = [], 0
        while size <= MOST:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        if size > MOST:
            return None
        return b"".join(chunks), stat.S_IMODE(info.st_mode)
    except OSError:
        return None
    finally:
        os.close(fd)


def _target(path):
    """What the link at `path` names: (FILE, bytes, mode, its real path), (ABSENT,) or
    (OTHER,)."""
    real = os.path.realpath(path)
    try:
        named = os.stat(path)
    except FileNotFoundError:
        return (ABSENT,)
    except OSError:
        return (OTHER,)
    if not stat.S_ISREG(named.st_mode):
        return (OTHER,)                     # a device, a folder: never even opened
    read = _read_regular(real)
    return (OTHER,) if read is None else (FILE, read[0], read[1], real)


def look(path):
    """The list at `path` as it is: (ABSENT,), (FILE, bytes, mode), (LINK, the link's text,
    what it names) or (OTHER,) - a folder, a device, a file that can't be read."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return (ABSENT,)
    except OSError:
        return (OTHER,)
    if stat.S_ISLNK(info.st_mode):
        try:
            return (LINK, os.readlink(path), _target(path))
        except OSError:
            return (OTHER,)
    if not stat.S_ISREG(info.st_mode):
        return (OTHER,)
    read = _read_regular(path)
    return (OTHER,) if read is None else (FILE, read[0], read[1])


def _sync_folder(folder):
    try:
        fd = os.open(folder, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _write_whole(path, data, mode):
    """`data` in `path`'s place, whole, with `mode`."""
    folder = os.path.dirname(path)
    fd, temp = tempfile.mkstemp(prefix=f".{os.path.basename(path)}.", suffix=".part",
                                dir=folder)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fchmod(handle.fileno(), mode)
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise
    _sync_folder(folder)


def _relink(path, text):
    """A link to `text` in `path`'s place, whole."""
    folder = os.path.dirname(path)
    for _attempt in range(100):
        temp = os.path.join(folder, f".{os.path.basename(path)}.{secrets.token_hex(8)}.part")
        try:
            os.symlink(text, temp)
            break
        except FileExistsError:
            continue
    else:
        raise OSError("no free name beside the list for the link")
    try:
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise
    _sync_folder(folder)


def _remove(path):
    """The regular file at `path` taken away (anything else left)."""
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            return
        os.unlink(path)
    except FileNotFoundError:
        return
    _sync_folder(os.path.dirname(path))


def file_key(uri):
    """A file: URI as the path it names (GTK and a chooser may escape one differently),
    anything else as it is."""
    try:
        parts = urlsplit(uri)
    except ValueError:
        return uri
    if parts.scheme != "file":
        return uri
    return os.path.normpath(unquote(parts.path, errors="surrogateescape"))


def _attribute(tag, name):
    marker = f' {name}="'
    start = tag.find(marker)
    if start < 0:
        return None
    start += len(marker)
    end = tag.find('"', start)
    return unescape(tag[start:end]) if end >= 0 else None


def _seconds(stamp):
    """GLib's ISO 8601 time ("2026-11-15T04:00:00.123456Z") as whole seconds, rounded up:
    GTK stamps a file as it adds it, which can be a moment before a chooser answers."""
    try:
        return math.ceil(datetime.fromisoformat(stamp).timestamp())
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def added(data):
    """What a recent files list holds: each file (file_key) with the last time any program
    added it. Read in one pass; anything that isn't GTK's own layout is left out."""
    found = {}
    if not data:
        return found
    text = data.decode("utf-8", "replace")
    for piece in text.split("</bookmark>"):
        start = piece.rfind("<bookmark ")
        if start < 0:
            continue
        piece = piece[start:]
        end = piece.find(">")
        href = _attribute(piece[: end + 1], "href") if end >= 0 else None
        if not href:
            continue
        stamps = [0]
        for tag in piece.split("<bookmark:application ")[1:]:
            close = tag.find(">")
            if close >= 0:
                stamps.append(_seconds(_attribute(" " + tag[: close + 1], "modified")) or 0)
        if max(stamps):
            key = file_key(href)
            found[key] = max(max(stamps), found.get(key, 0))
    return found


def _bytes_of(listing):
    """The bytes of a list as look() saw it, a link's file's included; None for no file."""
    if listing[0] == FILE:
        return listing[1]
    if listing[0] == LINK and listing[2][0] == FILE:
        return listing[2][1]
    return None


def holds(listing, uris, since):
    """Whether the list `listing` (as look() saw it) holds one of `uris` as added, by any
    program, at `since` or later."""
    keys = {file_key(uri) for uri in uris}
    return any(stamp >= since for key, stamp in added(_bytes_of(listing)).items()
               if key in keys)


def _glib_later(seconds, call):
    from gi.repository import GLib

    def once():
        call()
        return False

    source = GLib.timeout_add_seconds(seconds, once)
    return lambda: GLib.source_remove(source)


class Keeper:
    """The list at `path` kept as it was by GTK's writes in this process (see the module),
    for `manager` (the process's Gtk.RecentManager), on the GTK main thread.
    `application` is the name GTK registers the files chosen under
    (GLib.get_application_name()); `clock` and `later(seconds, call) -> cancel` are
    time.time and a GLib timeout, but for tests."""

    def __init__(self, manager, path, application, clock=time.time, later=_glib_later):
        self._manager, self.path, self._application = manager, path, application
        self._clock, self._later = clock, later
        self._home = os.path.dirname(path)
        self.clean = look(path)
        self.pending = False
        self._awaiting = set()
        self._since = 0
        self._cancel = None
        self._open = 0
        self._opened_at = 0
        self._seen = []
        manager.connect_after("changed", self._changed)

    @property
    def watching(self):
        """Whether the list's changes matter now: a write is pending, or a chooser is open."""
        return self.pending or self._open > 0

    def opened(self):
        """A chooser opened: until it answers (closed), every list seen is kept, so that a
        write of GTK's landing before the answer is put back too (chose)."""
        if self._open == 0:
            self._opened_at = int(self._clock())
            self._seen = [look(self.path)]
        self._open += 1

    def closed(self):
        """A chooser that opened has answered, or is gone."""
        self._open = max(0, self._open - 1)
        if self._open == 0:
            self._seen = []

    def _saw(self, now):
        if self._open and self._seen[-1] != now:
            self._seen.append(now)
            if len(self._seen) > SEEN:
                del self._seen[1:len(self._seen) - SEEN + 1]

    def _clean_for(self, now, uris):
        """The clean copy for a choice of `uris`: the list as it is, or - where it holds one
        of them as added since the chooser opened, GTK's write having come first - the last
        list seen since then that holds none of them."""
        if not self._open or not holds(now, uris, self._opened_at):
            return now
        for seen in reversed(self._seen):
            if not holds(seen, uris, self._opened_at):
                return seen
        return self._seen[0]

    def chose(self, uris):
        """A chooser answered with these files chosen: GTK's write of them is pending."""
        uris = {uri for uri in uris if isinstance(uri, str) and uri}
        if not uris:
            return
        now = None
        if not self.pending:
            now = look(self.path)
            self.clean = self._clean_for(now, uris)
            self.pending = True
            # GTK stamps a file as it adds it: since the chooser opened, where one did
            self._since = self._opened_at if self._open else int(self._clock())
        self._awaiting |= uris
        if self._cancel is not None:
            self._cancel()
        self._cancel = self._later(HOLD_SECONDS, self._held)
        if now is not None and now != self.clean:
            # GTK's write came first, the files in it: taken in, and put back now
            self._awaiting = {uri for uri in self._awaiting
                              if not holds(now, [uri], self._since)}
            self._changed()

    def _held(self):
        self._cancel = None
        self._settle()

    def _settle(self):
        self.pending = False
        self._awaiting.clear()
        if self._cancel is not None:
            self._cancel()
            self._cancel = None

    def _taken_in(self, uri):
        """Whether the manager holds `uri` as added by this app since the choice."""
        if not self._application:
            return False
        try:
            info = self._manager.lookup_item(uri)
            if info is None or not info.has_application(self._application):
                return False
            return info.get_application_info(self._application)[-1] >= self._since
        except Exception:  # noqa: BLE001 - GLib.Error when it isn't there: not taken in
            return False

    def _changed(self, _manager=None):
        try:
            now = look(self.path)
            self._saw(now)
            if not self.pending:
                self.clean = now
                return
            if now != self.clean:
                self._put(now)
            self._awaiting = {uri for uri in self._awaiting if not self._taken_in(uri)}
            if not self._awaiting:
                self._settle()
        except Exception as e:  # noqa: BLE001 - never a crash: the list is left as it is
            _say(f"{self.path} not put back: {e}")

    def put_back(self):
        """The list as it was, where it isn't: after the main loop has ended (GTK's last
        write)."""
        try:
            now = look(self.path)
            if now != self.clean:
                self._put(now)
        except Exception as e:  # noqa: BLE001 - never a crash: the list is left as it is
            _say(f"{self.path} not put back: {e}")

    def _put(self, now):
        clean, path = self.clean, self.path
        if OTHER in (clean[0], now[0]):
            _say(f"{path} left as it is: it was or is no file that can be read")
            return
        if clean[0] == ABSENT:
            if now[0] == FILE:
                _remove(path)
            return
        if clean[0] == FILE:
            _write_whole(path, clean[1], clean[2])
            return
        if now[:2] != clean[:2]:
            _relink(path, clean[1])
        was = clean[2]
        if was[0] != FILE:
            return                          # a link to /dev/null, say: never written
        current = _target(path)
        if current == was:
            return
        if current[0] != FILE or current[3] != was[3] or not _inside(was[3], self._home):
            _say(f"{path}: what its link names changed, and is left as it is")
            return
        _write_whole(was[3], was[1], was[2])


_NONE = object()
_keeper = None
# The file choosers answering a page's <input type="file">, held until they answer.
_open: set = set()


def _gtk():
    import gi

    gi.require_version("Gtk", "3.0")
    from gi.repository import GLib, Gtk

    return GLib, Gtk


def keeper():
    """The process's Keeper, made the first time (and with it GTK's recent manager, which
    the chooser would make anyway), on the GTK main thread; None where the list can't be
    kept (said once)."""
    global _keeper
    if _keeper is None:
        _keeper = _NONE
        try:
            GLib, Gtk = _gtk()
            home = data_home()
            if os.path.normpath(GLib.get_user_data_dir()) != home:
                raise RuntimeError(f"GLib's data folder ({GLib.get_user_data_dir()}) is not "
                                   f"$XDG_DATA_HOME ({home})")
            manager = Gtk.RecentManager.get_default()
            path = os.path.normpath(manager.props.filename or "")
            if path != os.path.join(home, RECENT_FILES):
                raise RuntimeError(f"GTK's list is {path}, not in {home}")
            _keeper = Keeper(manager, path, GLib.get_application_name())
        except Exception as e:  # noqa: BLE001 - no GTK, another list: the list isn't kept
            _say(f"the recent files list isn't kept as it was: {e}")
    return None if _keeper is _NONE else _keeper


def put_back():
    """After the main loop has ended (Gtk.main() or app.run() returned): the list as it
    was, where GTK's write as the loop ended changed it. Nothing where no chooser was made."""
    if _keeper is not None and _keeper is not _NONE:
        _keeper.put_back()


ACCEPTED = (-3, -5, -8, -10)    # Gtk.ResponseType ACCEPT, OK, YES, APPLY: GTK adds the files


def _accepted(response):
    try:
        return int(response) in ACCEPTED
    except (TypeError, ValueError):
        return False


def _strings(read):
    """What `read()` gives, the strings in it; [] where it gives none or fails."""
    try:
        values = list(read())
    except Exception:  # noqa: BLE001 - not a chooser's answer: no files
        return []
    return [value for value in values if isinstance(value, str) and value]


def _uris(chooser):
    """The files `chooser` chose, as URIs."""
    found = _strings(chooser.get_uris) if hasattr(chooser, "get_uris") else []
    if not found and hasattr(chooser, "get_filenames"):
        GLib = _gtk()[0]
        found = _strings(lambda: [GLib.filename_to_uri(path, None)
                                  for path in _strings(chooser.get_filenames)])
    return found


def _answered(guarding, chooser, response):
    """`chooser` answered `response`: the files it chose, where it accepted them."""
    if _accepted(response):
        guarding.chose(_uris(chooser))


def fit_room(width, height):
    """The most a chooser may take of a work area `width` x `height`: the area less its
    margins (FIT_MARGIN of each side, at least FIT_MARGIN_MIN pixels), never under 1."""
    across = max(FIT_MARGIN_MIN, int(width * FIT_MARGIN))
    down = max(FIT_MARGIN_MIN, int(height * FIT_MARGIN))
    return max(1, width - 2 * across), max(1, height - 2 * down)


def placed(x, y, width, height, area):
    """Where a window of `width` x `height` at (x, y) goes to lie inside `area` (x, y,
    width, height): moved in as little as it takes, its top left kept on the area where the
    window is larger than it."""
    left, top, across, down = area
    x = max(left, min(x, left + across - width))
    y = max(top, min(y, top + down - height))
    return x, y


def _monitor(window):
    """The monitor `window` opens on: its parent's, where it has one on a screen, else its
    own, else the display's primary (or first) monitor."""
    display = window.get_display()
    for each in (window.get_transient_for(), window):
        surface = each.get_window() if each is not None else None
        if surface is not None:
            found = display.get_monitor_at_window(surface)
            if found is not None:
                return found
    return display.get_primary_monitor() or display.get_monitor(0)


def _area(window):
    area = _monitor(window).get_workarea()
    return area.x, area.y, area.width, area.height


def _keep_on(window, _event=None):
    """A chooser mapped: where the window system lets the app place it (X11), moved to lie
    inside its monitor's work area. Wayland places it itself, within the size fit() set."""
    try:
        if window.get_display().__gtype__.name != "GdkX11Display":
            return False
        x, y = window.get_position()
        frame = window.get_window().get_frame_extents()
        to = placed(x, y, frame.width, frame.height, _area(window))
        if to != (x, y):
            window.move(*to)
    except Exception as e:  # noqa: BLE001 - never a crash: the chooser stays where it is
        _say(f"a file chooser not kept on the screen: {e}")
    return False


def fit(window):
    """A file chooser (a Gtk.FileChooserDialog: the app's own, or the one GTK makes for a
    Gtk.FileChooserNative) fits the work area of the monitor it opens on: never larger than
    the area less its margins (geometry hints, which GTK holds every size it asks for to,
    and which Wayland compositors are told), and, where the app may place it (X11), never
    off it. GTK's own size for a chooser comes from the font and what it saved last, and on
    a 1280x800 screen, or under a tiling compositor, its header bar - Cancel and Open - was
    off the screen."""
    try:
        from gi.repository import Gdk

        width, height = fit_room(*_area(window)[2:])
        hints = Gdk.Geometry()
        hints.max_width, hints.max_height = width, height
        window.set_geometry_hints(None, hints, Gdk.WindowHints.MAX_SIZE)
        across, down = window.get_default_size()
        if across > width or down > height:
            window.set_default_size(min(across, width), min(down, height))
        window.connect("map-event", _keep_on)
    except Exception as e:  # noqa: BLE001 - never a crash: the chooser opens as GTK sizes it
        _say(f"a file chooser not fitted to the screen: {e}")


def _fit_choosers():
    """fit() for every Gtk.FileChooserDialog realized from now on (an emission hook on
    "realize": GtkWindow's show realizes it before it asks for its size and maps it, so it
    opens at its fitted size; a hook on "show" crashed PyGObject, called for widgets still
    being made); what takes it away again."""
    from gi.repository import GObject

    Gtk = _gtk()[1]

    def hook(widget, *_rest):
        if isinstance(widget, Gtk.FileChooserDialog):
            fit(widget)
        return True

    GObject.type_class_ref(Gtk.Widget)
    hooked = GObject.add_emission_hook(Gtk.Widget, "realize", hook)
    return lambda: GObject.remove_emission_hook(Gtk.Widget, "realize", hooked)


def _fitting():
    """_fit_choosers(), or where it can't be had (no GTK), a hook that takes nothing away."""
    try:
        return _fit_choosers()
    except Exception as e:  # noqa: BLE001 - never a crash: the chooser opens as GTK sizes it
        _say(f"file choosers not fitted to the screen: {e}")
        return lambda: None


def run(chooser):
    """chooser.run() for a Gtk.FileChooserNative or Gtk.FileChooserDialog the app made,
    the files it chose kept out of the recent files list: its answer. They are told to the
    keeper as the chooser answers, not once run() has returned: GTK adds them as it answers,
    and run() can come back after GTK's write (a Save dialog's, measured)."""
    unfit = _fitting()
    try:
        guarding = keeper()
        if guarding is None:
            return chooser.run()
        try:
            chooser.connect("response", lambda answering, response:
                            _answered(guarding, answering, response))
            heard = True
        except Exception:  # noqa: BLE001 - a stand-in with no signals: told once answered
            heard = False
        guarding.opened()
        try:
            response = chooser.run()
            if not heard:
                _answered(guarding, chooser, response)
        finally:
            guarding.closed()
        return response
    finally:
        unfit()


def answer_file_input(webview, request):
    """A WebKit2.WebView's "run-file-chooser" (a page's <input type="file">), answered as
    WebKitGTK's own default handler does (WebKitWebView.cpp: a Gtk.FileChooserNative, Open,
    modal on the view's window, the request's filter, one file or several, the one it had
    chosen), the files chosen kept out of the recent files list. True: answered."""
    _GLib, Gtk = _gtk()
    toplevel = webview.get_toplevel()
    onscreen = (isinstance(toplevel, Gtk.Window) and toplevel.is_toplevel()
                and not isinstance(toplevel, Gtk.OffscreenWindow))
    parent = toplevel if onscreen else None
    multiple = bool(request.get_select_multiple())
    chooser = Gtk.FileChooserNative.new("Select Files" if multiple else "Select File", parent,
                                        Gtk.FileChooserAction.OPEN, None, None)
    if parent is not None:
        chooser.set_modal(True)
    wanted = request.get_mime_types_filter()
    if wanted is not None:
        chooser.set_filter(wanted)
    chooser.set_select_multiple(multiple)
    selected = request.get_selected_files()
    if selected:
        chooser.set_filename(selected[0])
    guarding = keeper()

    unfit = _fitting()

    def answered(native, response):
        unfit()
        _open.discard(native)
        if _accepted(response) and int(response) == int(Gtk.ResponseType.ACCEPT):
            if guarding is not None:
                _answered(guarding, native, response)
            request.select_files(native.get_filenames())
        else:
            request.cancel()
        if guarding is not None:
            guarding.closed()
        native.destroy()

    chooser.connect("response", answered)
    _open.add(chooser)
    if guarding is not None:
        guarding.opened()
    chooser.show()
    return True


def _is_webview(widget):
    kind = getattr(widget, "__gtype__", None)
    while kind is not None and kind.name not in ("", "GObject"):
        if kind.name == "WebKitWebView":
            return True
        kind = kind.parent
    return False


def answer_file_inputs(widget):
    """Every WebKit2.WebView in `widget` (a window) answers its pages' <input type="file">
    through answer_file_input. How many there were."""
    _GLib, Gtk = _gtk()
    found, stack = 0, [widget]
    while stack:
        each = stack.pop()
        if _is_webview(each):
            each.connect("run-file-chooser", answer_file_input)
            found += 1
        elif isinstance(each, Gtk.Container):
            stack.extend(each.get_children())
    return found


def _hook_responses(on_response):
    """on_response(dialog, response) for every Gtk.Dialog's "response" from now on (an
    emission hook: it runs before the dialog's own handlers); what takes it away again."""
    from gi.repository import GObject

    Gtk = _gtk()[1]

    def hook(dialog, response, *_rest):
        on_response(dialog, response)
        return True

    GObject.type_class_ref(Gtk.Dialog)            # its signals are known once its class is
    hooked = GObject.add_emission_hook(Gtk.Dialog, "response", hook)
    return lambda: GObject.remove_emission_hook(Gtk.Dialog, "response", hooked)


def _wrap_pywebview_dialogs():
    """pywebview's own file dialog (its GTK backend's BrowserView.create_file_dialog, which
    window.create_file_dialog and a download's Save dialog both run on the GTK main thread),
    the files it chose kept out of the recent files list: told to the keeper as the dialog
    answers, from a hook on Gtk.Dialog's "response" while it runs, as run() does."""
    from webview.platforms import gtk as backend

    view = backend.BrowserView
    original = view.create_file_dialog
    if getattr(original, "recentguard", False):
        return

    def create_file_dialog(self, *args, **kwargs):
        guarding = keeper()
        if guarding is None:
            unfit = _fitting()
            try:
                return original(self, *args, **kwargs)
            finally:
                unfit()
        chooser = _gtk()[1].FileChooser

        def answered(dialog, response):
            if isinstance(dialog, chooser):
                _answered(guarding, dialog, response)

        unhook = _hook_responses(answered)
        unfit = _fitting()
        guarding.opened()
        try:
            return original(self, *args, **kwargs)
        finally:
            guarding.closed()
            unfit()
            unhook()

    create_file_dialog.recentguard = True
    create_file_dialog.__wrapped__ = original
    view.create_file_dialog = create_file_dialog


def _before_show(window):
    try:
        answer_file_inputs(window.native)
    except Exception as e:  # noqa: BLE001 - never a crash: WebKit's own chooser answers
        _say(f"the window's file inputs not guarded: {e}")


def guard_pywebview(window):
    """A pywebview window (its GTK backend) whose file choosers keep the files they chose
    out of the recent files list: its own dialogs, and its pages' <input type="file">. Before
    webview.start()."""
    try:
        _wrap_pywebview_dialogs()
        window.events.before_show += _before_show
    except Exception as e:  # noqa: BLE001 - never a crash: the window opens unguarded, said
        _say(f"the window's file choosers not guarded: {e}")
