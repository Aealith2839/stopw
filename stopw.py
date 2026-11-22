#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Aealith2839
# Project history and development credits: see CREDITS.md.
# Licensing information: see LICENSING.md and COPYING.

"""stopw: a compact GNOME countdown timer.

stopw lets the user set an hours/minutes/seconds countdown and choose one
action to run when it reaches zero:

- Shut down the computer.
- Restart the computer.
- Suspend the computer.
- Show a GNOME notification, optionally with a sound.
- Run a user-defined shell command.

The application is written with Python, GTK 4 and libadwaita. Preferences
are stored through GSettings in the ``io.github.aealith2839.stopw`` schema. System
actions are requested through D-Bus, with ``logind`` as the preferred
backend and ConsoleKit as a fallback.

The countdown is driven by a monotonic deadline rather than by counting
individual timeout callbacks. This keeps the remaining time accurate even
when GTK delivers a callback late. During system suspend, the remaining
time is frozen; after the user unlocks the session, the countdown resumes
from that preserved value.

The final five seconds are a visual phase of the countdown, not an extra
delay. The real remaining time is kept separately from its displayed
``00:00:00`` rendering. Stop and application closure remove the single
countdown source, so no action can run afterwards. Suspension preserves
the remaining time and never restarts a timer that was already stopped.

Custom commands are the only operations performed with ``subprocess``.
"""

import gettext
import math
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

try:
    import gi

    gi.require_version('Gtk', '4.0')
    gi.require_version('Adw', '1')

    from gi.repository import Adw, Gdk, Gio, GLib, Gtk
except (ImportError, ValueError) as error:
    # GTK, PyGObject or libadwaita is unavailable. A GNOME notification
    # cannot be shown reliably at this point because the GUI stack itself
    # failed to load, so report the problem on stderr and exit.
    print(
        f'stopw: missing GTK/PyGObject/libadwaita dependency: {error}',
        file=sys.stderr,
    )
    sys.exit(1)


# Stable application identifier. It is used for GSettings, the GNOME
# application launcher, the icon name, single-instance behaviour and
# GNOME notifications.
APP_ID = 'io.github.aealith2839.stopw'

# Short application name displayed to the user and used as the gettext
# translation domain.
APP_NAME = 'stopw'

# Directory containing this Python file. When stopw is run directly from
# the source tree, this is the project root.
BASE_DIR = Path(__file__).resolve().parent

# Translation directory used when stopw is executed directly from the
# source tree. Meson installs compiled translations under the standard
# share/locale hierarchy instead.
LOCALE_DIR = BASE_DIR / 'locale'

# Maximum timer duration: 99 hours, 59 minutes and 59 seconds.
MAX_TIMER_SECONDS = 359999

# Number of seconds during which the digits blink once the countdown
# reaches zero. This is a visual indication only; it does not postpone
# the selected action.
BLINK_DURATION_SECONDS = 5

# Interval, in milliseconds, between the two visibility states of the
# blinking digits.
BLINK_INTERVAL_MILLISECONDS = 500

# Maximum time, in seconds, allowed for a custom command to complete.
# When this limit is reached, the command process group is killed.
CUSTOM_COMMAND_TIMEOUT_SECONDS = 30

# Time, in seconds, after which an unedited time field is deselected.
SELECTION_TIMEOUT_SECONDS = 5

# A copy installed as PREFIX/bin/stopw finds shared application data in
# PREFIX/share. For example, an installation in ~/.local/bin uses
# ~/.local/share, while a system-wide installation in /usr/bin uses
# /usr/share. This also makes the installed translation directory
# predictable without relying on an environment variable.
SHARE_DIR = BASE_DIR.parent / 'share'

# Prefer translations shipped beside a source-tree copy of stopw. If that
# directory does not exist, use the standard locale directory belonging to
# the installation prefix. gettext falls back to the original English
# strings when no catalogue is available for the active language.
ACTIVE_LOCALE_DIR = (
    LOCALE_DIR if LOCALE_DIR.is_dir() else SHARE_DIR / 'locale'
)

_ = gettext.translation(
    APP_NAME,
    localedir=str(ACTIVE_LOCALE_DIR),
    fallback=True,
).gettext


# User-visible labels for the actions available from the Action button.
# The keys are stable internal identifiers; they are also stored in
# GSettings when the user starts a timer with "Remember action" enabled.
ACTIONS = {
    'shutdown': _('Shut down'),
    'reboot': _('Restart'),
    'suspend': _('Suspend'),
    'notify': _('Notification'),
    'custom': _('Command'),
}

# User-visible labels displayed while a countdown is running. They tell
# the user which action is armed, without making the action button
# available for another selection.
RUNNING_LABELS = {
    'shutdown': _('Before shutdown'),
    'reboot': _('Before restart'),
    'suspend': _('Before suspend'),
    'notify': _('Before notification'),
    'custom': _('Before command'),
}


def load_settings():
    """Return a GSettings object for stopw.

    The schema must be available before the main window is created. A
    missing schema would otherwise cause an unrecoverable Gio error while
    the application is trying to read or write preferences.

    When stopw is run directly from the source tree, a locally compiled
    schema in ``data/`` is used first. This makes development possible
    without installing the complete application. Otherwise, the default
    GSettings schema search path is used; for a local installation, Meson
    installs the compiled schema under ``~/.local/share/glib-2.0/schemas``.

    Raises:
        RuntimeError: If the ``io.github.aealith2839.stopw`` schema cannot be found.
    """
    parent_source = Gio.SettingsSchemaSource.get_default()
    local_schema_dir = BASE_DIR / 'data'

    # Start from the normal system and user schema sources. Add the local
    # source only when its compiled schema file is present; this avoids
    # creating an invalid or empty schema source during development.
    schema_source = parent_source

    if (local_schema_dir / 'gschemas.compiled').exists():
        schema_source = Gio.SettingsSchemaSource.new_from_directory(
            str(local_schema_dir),
            parent_source,
            False,
        )

    schema = (
        schema_source.lookup(APP_ID, True)
        if schema_source is not None
        else None
    )

    if schema is None:
        raise RuntimeError(
            'The io.github.aealith2839.stopw GSettings schema is missing. '
            'Compile the GSettings schema.'
        )

    # Create the settings object from the explicitly located schema. This
    # guarantees that stopw uses the schema it has just validated, rather
    # than relying on a potentially different schema with the same ID.
    return Gio.Settings.new_full(schema, None, None)


class EditorDialog(Adw.Window):
    """Transactional editor for notification and custom-command settings.

    The dialog never changes GSettings while the user is editing values.
    Only an explicit click on **Save** writes the edited values through
    the owner window's ``write()`` method.

    For the notification editor, a selected audio file is first prepared
    by GTK without being played. The selected path is stored only after
    GTK reports that the media is prepared and contains an audio stream.
    This prevents an unreadable or non-audio file from replacing a
    previously working custom sound.

    Closing the dialog, pressing Escape, or dismissing the file chooser
    abandons all pending changes, including a newly selected but not yet
    validated audio file.
    """

    # Maximum time, in seconds, allowed for GTK to prepare a selected
    # audio file. This prevents a damaged or unusually slow media file
    # from leaving the Save button disabled indefinitely.
    AUDIO_PROBE_TIMEOUT_SECONDS = 10

    # Interval, in milliseconds, between checks of the media preparation
    # state. The interval is short enough to feel responsive while avoiding
    # a busy loop.
    AUDIO_PROBE_INTERVAL_MILLISECONDS = 50

    def __init__(self, parent, command=False):
        """Create either the notification editor or the command editor.

        Args:
            parent: The main stopw window that owns this dialog.
            command: ``True`` creates the custom-command editor;
                ``False`` creates the notification editor.
        """
        super().__init__(transient_for=parent, modal=True)

        # The owner window provides access to GSettings, error reporting
        # and interface refresh after a successful save.
        self.owner = parent

        # Select which editor this instance represents. The two editors
        # share the same transactional save and cancellation behaviour.
        self.command = command

        # ``closed`` becomes True as soon as the dialog begins closing.
        # It prevents asynchronous media-probe callbacks from modifying a
        # dialog that the user has already dismissed.
        self.closed = False

        # GTK media object used to validate a newly selected audio file.
        # It is never played: validation only requires preparation.
        self.probe = None

        # GLib source used to poll the preparation state of ``probe``.
        self.probe_source = None

        # Monotonic time after which an incomplete media preparation is
        # treated as an error.
        self.probe_deadline = None

        # The custom sound path currently committed in GSettings. It is
        # replaced only after a newly selected file has passed validation.
        self.path = parent.settings.get_string('custom-sound-path')

        self.set_title(
            _('Custom Command')
            if command
            else _('Notification Settings')
        )
        self.set_resizable(False)
        self.set_default_size(380, -1)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)

        # Keep a consistent margin around both dialog variants.
        for side in ('top', 'bottom', 'start', 'end'):
            getattr(box, 'set_margin_' + side)(18)

        self.set_content(box)

        box.append(
            Gtk.Label(
                label=(
                    _('Your command:')
                    if command
                    else _('Customize the end-of-countdown alert:')
                ),
                xalign=0,
            )
        )

        self.entry = Gtk.Entry(hexpand=True)

        # The command editor edits ``custom-command``; the notification
        # editor edits the optional notification message.
        key = 'custom-command' if command else 'notification-text'
        self.entry.set_text(parent.settings.get_string(key))
        box.append(self.entry)

        if not command:
            # The three sound modes form a mutually exclusive group:
            # no sound, GNOME default sound, or a user-selected file.
            self.radios = {}
            first = None

            for mode, label in [
                ('none', _('No sound')),
                ('default', _('Default sound')),
                ('custom', _('Custom audio file')),
            ]:
                button = Gtk.CheckButton(label=label)

                if first is not None:
                    button.set_group(first)
                else:
                    first = button

                self.radios[mode] = button
                box.append(button)

            # Restore the committed sound mode. An unknown stored value
            # safely falls back to the default sound mode.
            mode = parent.settings.get_string('notification-sound')
            self.radios.get(mode, self.radios['default']).set_active(True)

            self.browse = Gtk.Button(label=_('Browse…'))
            self.browse.connect('clicked', self.choose_audio)
            box.append(self.browse)

            # Show only the file name, not the complete path, to keep the
            # dialog compact. The complete path remains stored in GSettings.
            self.filename = Gtk.Label(
                label=(
                    os.path.basename(self.path)
                    or _('No file selected')
                )
            )
            self.filename.set_ellipsize(3)
            box.append(self.filename)

            # Show or hide the audio-file controls whenever the custom
            # sound mode is selected or deselected.
            self.radios['custom'].connect(
                'toggled',
                lambda *_: self.update_audio_controls(),
            )
            self.update_audio_controls()

        self.save = Gtk.Button(label=_('Save'))
        self.save.add_css_class('suggested-action')
        self.save.connect('clicked', self.commit)
        box.append(self.save)

        if command:
            # A custom command must contain at least one non-whitespace
            # character before it can be saved.
            self.entry.connect(
                'changed',
                lambda entry: self.save.set_sensitive(
                    bool(entry.get_text().strip())
                ),
            )

            # Pressing Enter in the entry saves the command, provided that
            # it is valid.
            self.entry.connect('activate', lambda *_: self.commit(None))
            self.save.set_sensitive(
                bool(self.entry.get_text().strip())
            )

        # Escape always closes the dialog without saving.
        keys = Gtk.EventControllerKey()
        keys.connect('key-pressed', self.key_pressed)
        self.add_controller(keys)

        self.connect('close-request', self.on_close)

    def key_pressed(self, controller, keyval, keycode, state):
        """Close the dialog when Escape is pressed.

        Returns:
            bool: ``True`` when Escape has been handled; otherwise
            ``False`` so GTK can process the event normally.
        """
        if keyval == Gdk.KEY_Escape:
            self.close()
            return True

        return False

    def update_audio_controls(self):
        """Enable audio controls only for the custom-sound mode."""
        active = self.radios['custom'].get_active()
        self.browse.set_sensitive(active)
        self.filename.set_visible(active)

    def choose_audio(self, *unused_args):
        """Open the GNOME file chooser for a custom notification sound."""
        dialog = Gtk.FileDialog(title=_('Select a sound'))
        dialog.open(self, None, self.audio_selected)

    def audio_selected(self, dialog, result):
        """Handle the file chosen by the user.

        The selected file is not accepted immediately. GTK first prepares
        a media object for it; the path is retained only when preparation
        succeeds and the media contains an audio stream.

        Args:
            dialog: The file chooser that produced the result.
            result: The asynchronous result returned by the chooser.
        """
        try:
            file = dialog.open_finish(result)

            # Ignore the result if the user closed this dialog while the
            # file chooser was still open.
            if self.closed or file is None:
                return

            path = file.get_path()

            # Remote files and URIs cannot be used reliably by the local
            # media playback implementation.
            if not path:
                raise ValueError('The selected file must be local.')

            # Cancel any validation still running for a previously chosen
            # file before starting validation for the new selection.
            self.cancel_probe()

            self.probe = Gtk.MediaFile.new_for_filename(path)
            self.probe_deadline = (
                time.monotonic() + self.AUDIO_PROBE_TIMEOUT_SECONDS
            )

            # Do not allow a save while the selected file has not yet been
            # validated.
            self.save.set_sensitive(False)

            self.probe_source = GLib.timeout_add(
                self.AUDIO_PROBE_INTERVAL_MILLISECONDS,
                self.check_probe,
                path,
            )

        except GLib.Error as error:
            # Closing or cancelling the chooser is a normal user action,
            # not an error. Leave the previously committed settings intact.
            if (
                error.matches(
                    Gtk.DialogError.quark(),
                    Gtk.DialogError.DISMISSED,
                )
                or error.matches(
                    Gio.io_error_quark(),
                    Gio.IOErrorEnum.CANCELLED,
                )
            ):
                return

            self.owner.report(
                _('Unable to read the custom audio file.'),
                error,
            )

        except Exception as error:
            self.owner.report(
                _('Unable to read the custom audio file.'),
                error,
            )

    def check_probe(self, path):
        """Check whether GTK has successfully prepared the selected audio.

        Args:
            path: Filesystem path of the audio file being validated.

        Returns:
            int: ``GLib.SOURCE_CONTINUE`` while preparation is still in
            progress, or ``GLib.SOURCE_REMOVE`` when validation ends.
        """
        # The dialog may have been closed while validation was running.
        if self.closed:
            self.probe_source = None
            return GLib.SOURCE_REMOVE

        error = self.probe.get_error()
        prepared = self.probe.is_prepared()

        # Keep waiting while GTK has not reported an error, has not yet
        # finished preparation, and the validation deadline has not passed.
        if (
            not error
            and not prepared
            and time.monotonic() < self.probe_deadline
        ):
            return GLib.SOURCE_CONTINUE

        self.probe_source = None

        if error or not prepared or not self.probe.has_audio():
            self.owner.report(
                _('Unable to read the custom audio file.'),
                error
                or 'The media has no audio stream or preparation timed out.',
            )
        else:
            # The file is valid. Retain its path for the next Save action
            # and display its file name in the dialog.
            self.path = path
            self.filename.set_text(os.path.basename(path))

        # Whether validation succeeded or failed, allow Save again. If the
        # file was invalid, Save will retain the previously committed path.
        self.probe = None
        self.save.set_sensitive(True)

        return GLib.SOURCE_REMOVE

    def cancel_probe(self):
        """Stop and discard any audio-file validation still in progress."""
        if self.probe_source is not None:
            GLib.source_remove(self.probe_source)
            self.probe_source = None

        self.probe = None

    def commit(self, *unused_args):
        """Write the edited values to GSettings and close on success.

        This method is the only path through which this dialog modifies
        preferences. Closing, pressing Escape, or cancelling a file choice
        never calls it.

        Args:
            *unused_args: GTK signal arguments, ignored by this handler.
        """
        if not self.save.get_sensitive():
            return

        if self.command:
            text = self.entry.get_text()

            # Defensive check: the Save button is already disabled for an
            # empty command, but Enter could still reach this method.
            if not text.strip():
                return

            ok = self.owner.write('custom-command', text)
        else:
            # Determine the currently selected sound mode from the radio
            # group. Exactly one mode must be active.
            mode = next(
                mode
                for mode, radio in self.radios.items()
                if radio.get_active()
            )

            # Write every notification preference. ``ok`` remains False if
            # any write fails, so the dialog stays open and the user can
            # retry after seeing the error.
            ok = self.owner.write(
                'notification-text',
                self.entry.get_text(),
            )
            ok = (
                self.owner.write('notification-sound', mode)
                and ok
            )
            ok = (
                self.owner.write('custom-sound-path', self.path)
                and ok
            )

        # Refresh the main window even after a partial write failure: the
        # interface must reflect whatever values were successfully saved.
        self.owner.refresh()

        if ok:
            self.close()

    def on_close(self, *_):
        """Prepare the dialog for closure without saving pending changes.

        Returns:
            bool: ``False`` to let GTK complete the normal window closure.
        """
        self.closed = True
        self.cancel_probe()

        return False


class StopwWindow(Adw.ApplicationWindow):
    """Main stopw window and owner of the countdown state.

    The window keeps one authoritative logical state:

    - ``remaining`` is the currently displayed and editable remaining time.
    - ``validated_duration`` is the duration accepted by the most recent
      Start action.
    - ``current_action`` is the action currently selected in the interface.
    - ``validated_action`` is the action accepted by the most recent Start
      action.

    The countdown uses a monotonic deadline rather than decrementing a
    separate integer on every timeout callback. If GTK delivers a callback
    late, the remaining time is recalculated from the deadline and therefore
    does not drift.

    During system suspend, the deadline is replaced by the frozen remaining
    time. No callback schedules an action independently of that deadline.
    A timer that was already stopped before suspension is never restarted
    when the system wakes up.
    """

    def __init__(self, app, settings):
        """Create the main window and restore saved preferences.

        Args:
            app: The owning ``StopwApplication`` instance.
            settings: The validated ``io.github.aealith2839.stopw`` GSettings object.
        """
        super().__init__(application=app, title=APP_NAME)

        # Keep references required by callbacks, preference persistence,
        # error reporting and the application lifecycle.
        self.app = app
        self.settings = settings

        # ``closed`` prevents callbacks, D-Bus replies and worker threads
        # from interacting with a window that is being destroyed.
        self.closed = False

        # ``active`` means that a countdown is running or that the window
        # is waiting for the session to unlock after a suspension.
        self.active = False

        # ``sleeping`` is True while the system is suspended.
        self.sleeping = False

        # Monotonic time at which the countdown reaches zero. It is set
        # only while a countdown is running.
        self.deadline = None

        # GLib source that periodically recalculates the remaining time.
        self.tick_source = None

        # GLib source that automatically deselects an unedited time field.
        self.selection_source = None

        # Index of the selected time field: 0 for hours, 1 for minutes and
        # 2 for seconds. ``None`` means that no field is selected.
        self.active_field = None

        # Digits typed by the user for the currently selected field.
        self.typing = ''

        # GTK media player used for the configured notification sound.
        self.media = None

        # Custom-command processes started by this window. The set and its
        # lock allow cleanup to terminate commands started from worker
        # threads.
        self.command_processes = set()
        self.process_lock = threading.Lock()

        # System D-Bus connection and subscription identifiers used to
        # monitor suspend and session-lock state.
        self.bus = None
        self.sleep_subscription = None

        # Restore only the last duration explicitly accepted by Start.
        # Values edited but not started are never restored.
        self.remaining = (
            float(
                max(
                    0,
                    min(
                        MAX_TIMER_SECONDS,
                        settings.get_int('last-timer-seconds'),
                    ),
                )
            )
            if settings.get_boolean('remember-timer')
            else 0.0
        )

        # Restore only an action that was explicitly accepted by Start and
        # that is still a known action identifier.
        saved = (
            settings.get_string('last-action')
            if settings.get_boolean('remember-action')
            else ''
        )
        self.current_action = saved if saved in ACTIONS else ''

        # Initialize the values that Start will persist. They become equal
        # to the current interface values as soon as Start is pressed.
        self.validated_duration = int(self.remaining)
        self.validated_action = self.current_action

        # Keep the window compact and prevent accidental resizing.
        self.set_size_request(240, 250)
        self.set_resizable(False)

        self.build_ui()
        self.monitor_sleep()
        self.connect('close-request', self.close_requested)
        self.refresh()

    def report(self, message, detail=''):
        """Report an error to the console and, when possible, to GNOME.

        Technical details are always written to stderr. A GNOME
        notification is sent only while the window is still open; after
        closure, the console remains the reliable diagnostic channel.

        Args:
            message: Short, user-facing error message.
            detail: Optional technical detail, exception or diagnostic
                information intended for the console.
        """
        print(
            f'stopw: {message}\n{detail}',
            file=sys.stderr,
        )

        if not self.closed:
            notification = Gio.Notification.new(APP_NAME)
            notification.set_body(message)
            self.app.send_notification('error', notification)

    def write(self, key, value):
        """Write a typed value to GSettings and report failures.

        GSettings setters may return ``False`` instead of raising an
        exception. This method treats both outcomes as failures, reports
        them consistently and returns a single boolean result.

        Args:
            key: GSettings key to write.
            value: Boolean, integer or string value to store.

        Returns:
            bool: ``True`` when the value was written successfully;
            otherwise ``False``.
        """
        try:
            if isinstance(value, bool):
                ok = self.settings.set_boolean(key, value)
            elif isinstance(value, int):
                ok = self.settings.set_int(key, value)
            else:
                ok = self.settings.set_string(key, value)

            if not ok:
                raise RuntimeError(f'GSettings refused to write: {key}')

            return True

        except Exception as error:
            self.report(
                _(
                    'Unable to save preferences. '
                    'See the console for details.'
                ),
                error,
            )
            return False

    def build_ui(self):
        """Create the complete user interface for the main window.

        The interface contains:

        - a header bar with Reset and the application menu;
        - the editable hours/minutes/seconds display;
        - the Action button and its menu;
        - the Start/Stop button.

        The CSS is intentionally embedded in Python. It is small, specific
        to this window and avoids adding an external stylesheet dependency.
        """
        css = Gtk.CssProvider()
        css.load_from_data(
            # Use tabular numerals so digit changes do not alter the width
            # of the countdown display.
            b'.time-digit {'
            b' font-family: Cantarell, sans-serif;'
            b' font-variant-numeric: tabular-nums;'
            b' font-feature-settings: "tnum";'
            b' font-size: 38px;'
            b' font-weight: bold;'
            b' opacity: 0.7;'
            b'}'
            b'.time-digit.selected {'
            b' opacity: 1.0;'
            b'}'
            b'.time-colon {'
            b' font-family: Cantarell, sans-serif;'
            b' font-size: 38px;'
            b' font-weight: bold;'
            b' opacity: 0.7;'
            b' padding: 0px 4px;'
            b'}'
            # Start uses the application's orange accent colour.
            b'.orange-button {'
            b' background: #e66100;'
            b' color: white;'
            b'}'
            # Stop uses a clearly distinguishable red colour.
            b'.red-button {'
            b' background: #c01c28;'
            b' color: white;'
            b'}'
            b'.red-button:hover {'
            b' background: #e01b24;'
            b'}'
            b'.running-label {'
            b' font-weight: bold;'
            b'}'
        )

        # Install the stylesheet for this display. The application priority
        # ensures that these rules take precedence over generic theme rules.
        Gtk.StyleContext.add_provider_for_display(
            self.get_display(),
            css,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.set_content(root)

        header = Adw.HeaderBar()
        header.set_title_widget(Gtk.Label(label=''))

        # Reset returns the countdown to zero. It does not change the
        # selected action or notification/command settings.
        self.reset_button = Gtk.Button(
            icon_name='view-refresh-symbolic',
            tooltip_text=_('Reset (Delete)'),
        )
        self.reset_button.connect(
            'clicked',
            lambda *_: self.reset(),
        )
        header.pack_start(self.reset_button)

        # Store the stateful preference actions so their states can be
        # inspected or updated consistently if future interface code needs
        # to do so.
        self.preferences = {}

        menu = Gio.Menu()

        # Create stateful GActions for both persistence preferences. Their
        # initial states come directly from GSettings.
        for key, label in [
            ('remember-timer', _('Remember timer')),
            ('remember-action', _('Remember action')),
        ]:
            action = Gio.SimpleAction.new_stateful(
                key,
                None,
                GLib.Variant(
                    'b',
                    self.settings.get_boolean(key),
                ),
            )
            action.connect(
                'change-state',
                self.preference_changed,
                key,
            )

            self.add_action(action)
            self.preferences[key] = action
            menu.append(label, 'win.' + key)

        about = Gio.SimpleAction.new('about', None)
        about.connect('activate', lambda *_: self.about())
        self.add_action(about)

        section = Gio.Menu()
        section.append(_('About stopw'), 'win.about')
        menu.append_section(None, section)

        header.pack_start(
            Gtk.MenuButton(
                icon_name='open-menu-symbolic',
                menu_model=menu,
            )
        )
        root.append(header)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)

        for side, margin in [
            ('top', 12),
            ('bottom', 14),
            ('start', 16),
            ('end', 16),
        ]:
            getattr(body, 'set_margin_' + side)(margin)

        body.set_vexpand(True)
        root.append(body)

        # The time display is focusable so that keyboard input can be
        # directed to the selected hours, minutes or seconds field.
        self.time_box = Gtk.Box(
            halign=Gtk.Align.CENTER,
            focusable=True,
        )
        self.digits = []

        # Create three editable digit groups: hours, minutes and seconds.
        # A colon label is inserted before the second and third groups.
        for index in range(3):
            if index:
                colon = Gtk.Label(label=':')
                colon.add_css_class('time-colon')
                self.time_box.append(colon)

            label = Gtk.Label(label='00')
            label.add_css_class('time-digit')

            click = Gtk.GestureClick()
            click.set_button(1)
            click.connect(
                'pressed',
                self.field_clicked,
                index,
            )
            label.add_controller(click)

            self.digits.append(label)
            self.time_box.append(label)

        # A vertical mouse-wheel movement changes the selected field by one
        # unit. Discrete scrolling prevents fractional wheel events from
        # producing unexpected intermediate values.
        scroll = Gtk.EventControllerScroll.new(
            Gtk.EventControllerScrollFlags.VERTICAL
            | Gtk.EventControllerScrollFlags.DISCRETE
        )
        scroll.connect('scroll', self.scroll_time)
        self.time_box.add_controller(scroll)
        body.append(self.time_box)

        # The stack switches between action selection and the label shown
        # while a countdown is running. No transition is used because the
        # state change must be immediate.
        self.stack = Gtk.Stack(vexpand=True)
        self.stack.set_transition_type(Gtk.StackTransitionType.NONE)

        selection = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        # Push the Action button towards the bottom of the available space.
        selection.append(Gtk.Box(vexpand=True))

        self.action_button = Gtk.MenuButton(label=_('Action'))
        self.action_button.set_size_request(-1, 42)

        actions_menu = Gio.Menu()
        self.action_objects = []

        # Create one GAction for each end-of-countdown action. The action
        # identifiers are stable and are also used by GSettings.
        for name, label in ACTIONS.items():
            action = Gio.SimpleAction.new('choose-' + name, None)
            action.connect(
                'activate',
                self.choose_action,
                name,
            )

            self.add_action(action)
            self.action_objects.append(action)
            actions_menu.append(
                label,
                'win.choose-' + name,
            )

        # Add entries that open the configuration dialogs. They do not
        # change the currently selected action.
        for name, label, command in [
            (
                'configure-notify',
                _('Configure notification…'),
                False,
            ),
            (
                'configure-command',
                _('Custom command…'),
                True,
            ),
        ]:
            action = Gio.SimpleAction.new(name, None)
            action.connect(
                'activate',
                self.open_editor,
                command,
            )

            self.add_action(action)
            self.action_objects.append(action)
            actions_menu.append(label, 'win.' + name)

        self.action_button.set_menu_model(actions_menu)
        selection.append(self.action_button)

        self.stack.add_named(selection, 'selection')

        # This label identifies the armed action while the countdown runs.
        self.running_label = Gtk.Label()
        self.running_label.add_css_class("running-label")
        self.running_label.set_valign(Gtk.Align.FILL)
        self.running_label.set_yalign(0.47)
        self.stack.add_named(self.running_label, 'running')

        body.append(self.stack)

        self.start_button = Gtk.Button(label=_('Start'))
        self.start_button.set_size_request(-1, 42)
        self.start_button.add_css_class('orange-button')
        self.start_button.connect(
            'clicked',
            lambda *_: self.toggle(),
        )
        body.append(self.start_button)

        # Capture keyboard events before ordinary widgets handle them. This
        # allows Space to start or stop the countdown and Delete to reset it.
        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect('key-pressed', self.key_pressed)
        self.add_controller(keys)

    def preference_changed(self, action, variant, key):
        """Apply a change to one of the persistence preferences.

        Disabling a preference clears the corresponding persisted value.
        This means that the next application start will not restore a
        duration or action. It does not clear the custom command or the
        notification settings, which remain independently stored.

        Args:
            action: The stateful GAction that requested the change.
            variant: The requested boolean state.
            key: ``remember-timer`` or ``remember-action``.
        """
        enabled = variant.get_boolean()

        # Update GSettings first. The visual state is changed only when the
        # write succeeds, so the menu cannot show a state that was refused.
        if self.write(key, enabled):
            action.set_state(variant)

            if key == 'remember-timer':
                # When the preference is enabled, retain the last duration
                # accepted by Start. When it is disabled, clear the stored
                # duration and the in-memory validated duration.
                self.write(
                    'last-timer-seconds',
                    self.validated_duration if enabled else 0,
                )

                if not enabled:
                    self.validated_duration = 0
            else:
                # Apply the same rule to the action: retain the last action
                # accepted by Start, or clear it when memory is disabled.
                self.write(
                    'last-action',
                    self.validated_action if enabled else '',
                )

                if not enabled:
                    self.validated_action = ''

    def choose_action(self, action, parameter, name):
        """Select an end-of-countdown action.

        Action selection is intentionally unavailable while a countdown is
        running. This prevents the user from changing the action after it
        has been armed by Start.

        Args:
            action: The GAction activated from the Action menu.
            parameter: Unused action parameter.
            name: Stable action identifier, such as ``shutdown`` or
                ``notify``.
        """
        if not self.active:
            self.current_action = name
            self.refresh()

    def open_editor(self, action, parameter, command):
        """Open the notification or custom-command editor.

        Configuration dialogs do not change the currently selected action.
        They only edit the settings associated with an action.

        Args:
            action: The GAction activated from the Action menu.
            parameter: Unused action parameter.
            command: ``True`` opens the custom-command editor; ``False``
                opens the notification editor.
        """
        if not self.active:
            EditorDialog(self, command).present()

    def about(self):
        """Show the application About dialog."""
        dialog = Adw.AboutWindow(
            transient_for=self,
            modal=True,
            application_name=APP_NAME,
            application_icon=APP_ID,
            version='0.1.0',
            developer_name='Eric B.',
        )
        dialog.set_comments(_('A compact timer for GNOME'))
        dialog.present()

    # Time input: entered digits are converted back into the total
    # remaining duration, rather than being treated as display-only text.
    def field_clicked(self, gesture, count, x, y, index):
        """Select a time field after a mouse click.

        Args:
            gesture: The click gesture that activated the field.
            count: Number of presses, unused by this handler.
            x: Horizontal click position, unused by this handler.
            y: Vertical click position, unused by this handler.
            index: Field index: 0 for hours, 1 for minutes, 2 for seconds.
        """
        if not self.active:
            self.select(index)
            self.time_box.grab_focus()

    def select(self, index):
        """Select a time field, or deselect all fields.

        Args:
            index: Field index, or ``None`` to remove the selection.
        """
        self.active_field = index
        self.typing = ''
        self.arm_selection()
        self.refresh()

    def arm_selection(self):
        """Start or restart the automatic field-deselection timer.

        A selected field remains available for keyboard input for
        ``SELECTION_TIMEOUT_SECONDS``. Each new interaction restarts that
        delay.
        """
        if self.selection_source is not None:
            GLib.source_remove(self.selection_source)
            self.selection_source = None

        if self.active_field is not None:
            self.selection_source = GLib.timeout_add_seconds(
                SELECTION_TIMEOUT_SECONDS,
                self.expire_selection,
            )

    def expire_selection(self):
        """Deselect the current time field after the selection timeout.

        Returns:
            int: ``GLib.SOURCE_REMOVE`` so the one-shot GLib source ends.
        """
        self.selection_source = None
        self.active_field = None
        self.typing = ''
        self.refresh()

        return GLib.SOURCE_REMOVE

    def values(self):
        """Return the displayed hours, minutes and seconds.

        The remaining time is rounded up for display. For example, a
        remaining duration of 0.4 seconds is displayed as ``00:00:01``.

        Returns:
            list[int]: Hours, minutes and seconds values.
        """
        total = max(
            0,
            min(
                MAX_TIMER_SECONDS,
                math.ceil(self.remaining),
            ),
        )

        return [
            total // 3600,
            total // 60 % 60,
            total % 60,
        ]

    def set_field(self, value):
        """Set the selected time field and rebuild the total duration.

        The value is clamped to the field limit: 99 for hours and 59 for
        minutes or seconds. The complete duration is then recalculated from
        all three fields, ensuring that keyboard input changes the actual
        countdown duration.

        Args:
            value: Requested value for the selected field.
        """
        if self.active or self.active_field is None:
            return

        values = self.values()
        values[self.active_field] = max(
            0,
            min(
                99 if self.active_field == 0 else 59,
                value,
            ),
        )

        self.remaining = float(
            values[0] * 3600 + values[1] * 60 + values[2]
        )
        self.arm_selection()
        self.refresh()

    def adjust(self, delta):
        """Change the selected field by the requested number of units.

        Args:
            delta: Positive or negative number of units to add to the
                selected field.
        """
        if not self.active and self.active_field is not None:
            self.typing = ''
            self.set_field(
                self.values()[self.active_field] + delta,
            )

    def scroll_time(self, controller, dx, dy):
        """Change the selected field with a vertical mouse-wheel movement.

        Args:
            controller: The scroll event controller.
            dx: Horizontal scroll amount, unused by this handler.
            dy: Vertical scroll amount. A positive value scrolls downward
                and decreases the field; a negative value increases it.

        Returns:
            bool: ``True`` when the scroll event has been handled.
        """
        if (
            self.active
            or self.active_field is None
            or dy == 0
        ):
            return False

        self.adjust(-1 if dy > 0 else 1)

        return True

    def key_pressed(self, controller, keyval, keycode, state):
        """Handle keyboard input for the countdown and time fields.

        Space starts or stops the countdown when the Start/Stop button is
        available. Delete resets the countdown. While a time field is
        selected, arrow keys change fields or values, Backspace clears the
        selected field, and digits are accumulated into that field.

        Tab deliberately deselects the time field rather than moving focus
        to another widget. This keeps keyboard interaction focused on the
        countdown until the user explicitly selects another control.

        Args:
            controller: The keyboard event controller.
            keyval: GDK key value.
            keycode: Hardware key code.
            state: Modifier-key state.

        Returns:
            bool: ``True`` when the event has been handled; otherwise
            ``False`` so GTK can process it normally.
        """
        # Let an open Action menu retain control of keyboard events. This
        # avoids Space or other keys unexpectedly operating the timer while
        # the user is navigating the menu.
        popover = self.action_button.get_popover()

        if popover and popover.get_visible():
            return False

        if keyval in (Gdk.KEY_space, Gdk.KEY_KP_Space):
            if self.start_button.get_sensitive():
                self.toggle()

            return True

        if self.active:
            return False

        if keyval in (Gdk.KEY_Delete, Gdk.KEY_KP_Delete):
            self.reset()
            return True

        index = self.active_field

        if index is None:
            return False

        if keyval in (Gdk.KEY_Tab, Gdk.KEY_ISO_Left_Tab):
            self.select(None)
            return False

        if keyval in (Gdk.KEY_Left, Gdk.KEY_Right):
            self.select(
                max(
                    0,
                    min(
                        2,
                        index + (-1 if keyval == Gdk.KEY_Left else 1),
                    ),
                )
            )
            return True

        if keyval in (Gdk.KEY_Up, Gdk.KEY_Down):
            self.adjust(
                1 if keyval == Gdk.KEY_Up else -1,
            )
            return True

        if keyval == Gdk.KEY_BackSpace:
            self.typing = ''
            self.set_field(0)
            return True

        character = chr(Gdk.keyval_to_unicode(keyval) or 0)

        if character in '0123456789':
            # Keep at most the last two typed digits. This allows natural
            # entry of values such as 05, 30 or 59 without requiring an
            # explicit field reset.
            self.typing = (self.typing + character)[-2:]
            self.set_field(int(self.typing))
            return True

        return False

    # Countdown: no additional delay is added after zero.
    def toggle(self):
        """Start the countdown, or stop it if it is already running.

        When Start is pressed, the currently displayed duration and selected
        action become the validated values. They are persisted immediately
        only if the corresponding Remember preferences are enabled.

        A monotonic deadline is calculated from the remaining time. The
        countdown source is then installed to recalculate the remaining time
        from that deadline.
        """
        if self.active:
            self.stop()
        elif self.can_start():
            # Round the displayed duration up to a whole second. This is the
            # duration that Start accepts and, when requested, persists.
            self.validated_duration = math.ceil(self.remaining)
            self.validated_action = self.current_action

            if self.settings.get_boolean('remember-timer'):
                self.write(
                    'last-timer-seconds',
                    self.validated_duration,
                )

            if self.settings.get_boolean('remember-action'):
                self.write(
                    'last-action',
                    self.validated_action,
                )

            # Remove the field selection before entering the running state.
            self.select(None)

            # Invalidate any session-lock check that may still be pending
            # from a previous suspension. A new Start must never be resumed
            # by an obsolete D-Bus reply.
            self.lock_check_serial += 1
            self.waiting_for_unlock = False

            self.active = True
            self.deadline = time.monotonic() + self.remaining

            self.install_tick()
            self.refresh()

    def can_start(self):
        """Return whether a countdown can currently be started.

        A valid duration, a selected action and, for a custom command, a
        non-empty command are required.

        Returns:
            bool: ``True`` when Start may begin a countdown.
        """
        return (
            self.remaining > 0
            and self.current_action in ACTIONS
            and (
                self.current_action != 'custom'
                or bool(
                    self.settings.get_string('custom-command').strip()
                )
            )
        )

    def install_tick(self):
        """Install the periodic countdown source.

        The tick interval is deliberately shorter than one second. This lets
        the interface update promptly after a suspension, while the actual
        remaining time is always derived from the monotonic deadline.
        """
        self.remove_tick()
        self.tick_source = GLib.timeout_add(50, self.tick)

    def remove_tick(self):
        """Remove the periodic countdown source, if one is installed."""
        if self.tick_source is not None:
            GLib.source_remove(self.tick_source)
            self.tick_source = None

    def stop(self):
        """Stop the countdown or the wait for session unlock.

        If a countdown is running, its remaining time is recalculated from
        the monotonic deadline. The time is therefore preserved exactly and
        can be resumed later with Start.

        Incrementing ``lock_check_serial`` invalidates any pending D-Bus
        lock-state reply. A response received after Stop must never cause
        an automatic restart.
        """
        if (
            self.active
            and not self.sleeping
            and self.deadline is not None
        ):
            self.remaining = max(
                0.0,
                self.deadline - time.monotonic(),
            )

        self.lock_check_serial += 1
        self.waiting_for_unlock = False
        self.active = False
        self.deadline = None
        self.remove_tick()
        self.refresh()

    def reset(self):
        """Set the countdown to zero without changing other settings.

        Reset is available only while no countdown is running. It does not
        change the selected action, custom command, notification text or
        notification sound.
        """
        if not self.active:
            self.remaining = 0.0
            self.typing = ''
            self.refresh()

    def tick(self):
        """Recalculate the remaining time and run the action at zero.

        The remaining time is always derived from the monotonic deadline.
        This prevents time drift if GTK delivers this callback late.

        Returns:
            int: ``GLib.SOURCE_CONTINUE`` while the countdown is running,
            or ``GLib.SOURCE_REMOVE`` when it has stopped or completed.
        """
        if (
            self.closed
            or not self.active
            or self.sleeping
            or self.waiting_for_unlock
        ):
            self.tick_source = None
            return GLib.SOURCE_REMOVE

        self.remaining = max(
            0.0,
            self.deadline - time.monotonic(),
        )

        if self.remaining <= 0:
            # Disarm the countdown source before executing the action. If
            # the action is Suspend, the resulting suspension cannot cause
            # this same completed countdown to run again after wakeup.
            self.tick_source = None
            action = self.current_action

            self.active = False
            self.deadline = None
            self.refresh()

            self.execute(action)

            return GLib.SOURCE_REMOVE

        self.refresh()

        return GLib.SOURCE_CONTINUE

    def refresh(self):
        """Update the interface without changing the remaining time.

        During the final five seconds, the countdown continues normally and
        the action label remains visible. Only the numeric digits are
        periodically dimmed, providing a clear visual indication without
        hiding the remaining time.

        No text colour is imposed: the GTK theme's foreground colour remains
        used in both light and dark modes.
        """
        blinking = (
            self.active
            and 0 < self.remaining <= BLINK_DURATION_SECONDS
        )

        # Always display the real remaining time, including during the
        # blinking phase. Do not replace it with zeros.
        values = self.values()

        # Alternate the digit opacity every configured interval. The phase
        # is derived from monotonic time, so it remains stable even if this
        # method is called at an irregular rate.
        phase = (
            int(
                time.monotonic() * 1000
                / BLINK_INTERVAL_MILLISECONDS
            )
            % 2
        )

        # An opacity of 0.65 dims the digits without erasing them. Their
        # colour remains the one chosen by the active GTK theme.
        digit_opacity = 0.65 if blinking and phase else 1.0

        for index, label in enumerate(self.digits):
            label.set_text(f'{values[index]:02d}')
            label.set_opacity(digit_opacity)

            if self.active_field == index and not self.active:
                label.add_css_class('selected')
            else:
                label.remove_css_class('selected')

        self.action_button.set_label(
            ACTIONS.get(self.current_action, _('Action'))
        )

        self.running_label.set_text(
            RUNNING_LABELS.get(self.current_action, '')
        )

        # The action label remains visible throughout the countdown. The
        # former empty "blink" page is no longer used.
        self.stack.set_visible_child_name(
            'running' if self.active else 'selection'
        )

        self.start_button.set_label(
            _('Stop') if self.active else _('Start')
        )

        # Preserve the Start/Stop colour distinction: orange for Start and
        # red for Stop.
        if self.active:
            self.start_button.remove_css_class('orange-button')
            self.start_button.add_css_class('red-button')
        else:
            self.start_button.remove_css_class('red-button')
            self.start_button.add_css_class('orange-button')

        self.start_button.set_sensitive(
            self.active or self.can_start()
        )

        self.reset_button.set_sensitive(
            not self.active and self.remaining > 0
        )

        self.action_button.set_sensitive(not self.active)

        for action in self.action_objects:
            action.set_enabled(not self.active)

    def monitor_sleep(self):
        """Monitor system sleep and session locking.

        Locking the session alone never changes the countdown. After a
        suspension, however, the countdown resumes only once the session
        has been confirmed as unlocked.
        """
        self.session_path = None
        self.lock_subscription = None
        self.waiting_for_unlock = False

        # Logical identifier for lock-state queries. It allows an obsolete
        # reply to be ignored after a new suspension or another query.
        self.lock_check_serial = 0

        try:
            self.bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)

            self.sleep_subscription = self.bus.signal_subscribe(
                'org.freedesktop.login1',
                'org.freedesktop.login1.Manager',
                'PrepareForSleep',
                '/org/freedesktop/login1',
                None,
                Gio.DBusSignalFlags.NONE,
                self.sleep_signal,
            )

            # An application launched by the desktop environment may belong
            # to a user service rather than directly to the session scope.
            # Use the session provided by the environment first, then fall
            # back to the process session if necessary.
            session_id = os.environ.get('XDG_SESSION_ID')
            lookup_error = None

            if session_id:
                try:
                    reply = self.bus.call_sync(
                        'org.freedesktop.login1',
                        '/org/freedesktop/login1',
                        'org.freedesktop.login1.Manager',
                        'GetSession',
                        GLib.Variant('(s)', (session_id,)),
                        None,
                        Gio.DBusCallFlags.NONE,
                        2000,
                        None,
                    )
                    self.session_path = reply.unpack()[0]
                except GLib.Error as error:
                    lookup_error = error

            if self.session_path is None:
                try:
                    reply = self.bus.call_sync(
                        'org.freedesktop.login1',
                        '/org/freedesktop/login1',
                        'org.freedesktop.login1.Manager',
                        'GetSessionByPID',
                        GLib.Variant('(u)', (os.getpid(),)),
                        None,
                        Gio.DBusCallFlags.NONE,
                        2000,
                        None,
                    )
                    self.session_path = reply.unpack()[0]
                except GLib.Error as error:
                    lookup_error = error

            if self.session_path is None:
                # Some launches through user services provide neither
                # XDG_SESSION_ID nor a PID attached to a session. In that
                # case, look for a single graphical session owned by this
                # user. Never choose arbitrarily between several local or
                # remote desktop sessions.
                reply = self.bus.call_sync(
                    'org.freedesktop.login1',
                    '/org/freedesktop/login1',
                    'org.freedesktop.login1.Manager',
                    'ListSessions',
                    None,
                    None,
                    Gio.DBusCallFlags.NONE,
                    2000,
                    None,
                )

                candidates = []

                for session in reply.unpack()[0]:
                    session_id, uid, username, seat, object_path = session

                    if uid != os.getuid():
                        continue

                    properties_reply = self.bus.call_sync(
                        'org.freedesktop.login1',
                        object_path,
                        'org.freedesktop.DBus.Properties',
                        'GetAll',
                        GLib.Variant(
                            '(s)',
                            ('org.freedesktop.login1.Session',),
                        ),
                        None,
                        Gio.DBusCallFlags.NONE,
                        2000,
                        None,
                    )

                    properties = properties_reply.unpack()[0]
                    session_type = properties.get('Type', '')
                    session_class = properties.get('Class', '')
                    session_state = properties.get('State', '')

                    if (
                        session_type in ('wayland', 'x11')
                        and session_class in ('user', 'user-early', 'user-light')
                        and session_state != 'closing'
                    ):
                        candidates.append((session_id, object_path))

                if len(candidates) == 1:
                    self.session_path = candidates[0][1]

                elif len(candidates) > 1:
                    session_ids = ', '.join(
                        candidate[0] for candidate in candidates
                    )
                    raise RuntimeError(
                        'Plusieurs sessions graphiques existent pour cet '
                        'utilisateur ; impossible de choisir celle de stopw '
                        f'unambiguously: {session_ids}'
                    )

                else:
                    raise RuntimeError(
                        'No user graphical session detected. '
                        f'Last lookup error: {lookup_error}'
                    )

            # Listen only to this session object, not to lock changes in
            # other local or remote sessions.
            self.lock_subscription = self.bus.signal_subscribe(
                'org.freedesktop.login1',
                'org.freedesktop.DBus.Properties',
                'PropertiesChanged',
                self.session_path,
                'org.freedesktop.login1.Session',
                Gio.DBusSignalFlags.NONE,
                self.session_lock_changed,
            )

        except Exception as error:
            self.report(
                _(
                    'Unable to fully monitor system suspension and session '
                    'locking. See the console for details.'
                ),
                error,
            )

    def sleep_signal(
        self,
        connection,
        sender,
        path,
        interface,
        name,
        parameters,
    ):
        """Freeze the countdown on suspension; check the session on wakeup."""
        if self.closed:
            return

        going_to_sleep = parameters.unpack()[0]

        if going_to_sleep:
            if self.sleeping:
                return

            # Invalidate any lock-state query still in progress.
            self.lock_check_serial += 1

            if self.active:
                # A second suspension may occur while we are still waiting
                # for the first suspension to be followed by an unlock. In
                # that case, retain the duration already frozen.
                if self.deadline is not None:
                    self.remaining = max(
                        0.0,
                        self.deadline - time.monotonic(),
                    )

                self.remove_tick()
                self.deadline = None
                self.waiting_for_unlock = True

            self.sleeping = True

        else:
            if not self.sleeping:
                return

            self.sleeping = False

            if self.active and self.waiting_for_unlock:
                self.check_session_lock()

        self.refresh()

    def session_lock_changed(
        self,
        connection,
        sender,
        path,
        interface,
        name,
        parameters,
    ):
        """An ordinary lock does not stop the timer.

        Only a timer already frozen by a suspension may be resumed after
        this signal.
        """
        if (
            self.closed
            or self.sleeping
            or not self.active
            or not self.waiting_for_unlock
        ):
            return

        changed_interface, changed, invalidated = parameters.unpack()

        if changed_interface != 'org.freedesktop.login1.Session':
            return

        if 'LockedHint' in changed or 'LockedHint' in invalidated:
            # Read the current state rather than relying on an older signal
            # that may have remained queued during sleep.
            self.check_session_lock()

    def check_session_lock(self):
        """Read the lock state without blocking the GTK main loop."""
        if (
            self.closed
            or self.sleeping
            or not self.active
            or not self.waiting_for_unlock
        ):
            return

        if self.bus is None or self.session_path is None:
            self.report(
                _(
                    'Unable to check whether the session has been unlocked. '
                    'The timer remains paused. You can stop it with Stop.'
                ),
                'logind session unavailable.',
            )
            return

        self.lock_check_serial += 1
        serial = self.lock_check_serial

        self.bus.call(
            'org.freedesktop.login1',
            self.session_path,
            'org.freedesktop.DBus.Properties',
            'Get',
            GLib.Variant(
                '(ss)',
                ('org.freedesktop.login1.Session', 'LockedHint'),
            ),
            GLib.VariantType.new('(v)'),
            Gio.DBusCallFlags.NONE,
            2000,
            None,
            self.session_lock_received,
            serial,
        )

    def session_lock_received(self, connection, result, serial):
        """Resume only if the reply is current and the session is unlocked."""
        try:
            reply = connection.call_finish(result)
            locked = reply.unpack()[0]
        except GLib.Error as error:
            if (
                not self.closed
                and serial == self.lock_check_serial
                and self.active
                and self.waiting_for_unlock
                and not self.sleeping
            ):
                self.report(
                    _(
                        'Unable to check whether the session has been '
                        'unlocked. The timer remains paused. You can stop '
                        'it with Stop.'
                    ),
                    error,
                )
            return

        if (
            self.closed
            or serial != self.lock_check_serial
            or self.sleeping
            or not self.active
            or not self.waiting_for_unlock
        ):
            return

        if locked:
            # Wait for PropertiesChanged to announce the unlock.
            return

        self.waiting_for_unlock = False
        self.deadline = time.monotonic() + self.remaining
        self.install_tick()
        self.refresh()
        
        
    def execute(self, action):
        if self.closed:
            return
        if action in ('shutdown', 'reboot', 'suspend'):
            # Potentially slow D-Bus calls never block GTK.
            threading.Thread(target=self.system_worker, args=(action,), daemon=True).start()
        elif action == 'notify':
            text = self.settings.get_string('notification-text')
            if text.strip():
                notification = Gio.Notification.new(APP_NAME)
                notification.set_body(text)
                self.app.send_notification('timer-complete', notification)
            self.play_sound()
        elif action == 'custom':
            command = self.settings.get_string('custom-command')
            threading.Thread(target=self.command_worker, args=(command,), daemon=True).start()

    def worker_error(self, message, detail):
        def deliver():
            if not self.closed:
                self.report(message, detail)
            return GLib.SOURCE_REMOVE
        GLib.idle_add(deliver)

    def system_worker(self, action):
        try:
            bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
            backend = None
            # Check availability before calling: a logind refusal must
            # never be bypassed by trying a second manager.
            for service, path, interface in [
                ('org.freedesktop.login1', '/org/freedesktop/login1', 'org.freedesktop.login1.Manager'),
                ('org.freedesktop.ConsoleKit', '/org/freedesktop/ConsoleKit/Manager', 'org.freedesktop.ConsoleKit.Manager'),
            ]:
                reply = bus.call_sync('org.freedesktop.DBus', '/org/freedesktop/DBus',
                                      'org.freedesktop.DBus', 'NameHasOwner',
                                      GLib.Variant('(s)', (service,)), None,
                                      Gio.DBusCallFlags.NONE, 5000, None)
                if reply.unpack()[0]:
                    backend = (service, path, interface)
                    break
            if backend is None:
                self.worker_error(_('Unable to perform the system action: no compatible session manager was found.'), 'No D-Bus owner found.')
                return
            method = {'shutdown': 'PowerOff', 'reboot': 'Reboot', 'suspend': 'Suspend'}[action]
            # ConsoleKit2 provides these methods with the interactive
            # boolean. The older no-argument Stop/Restart variants are
            # tried only if the modern method is unknown.
            if self.closed:
                return
            try:
                bus.call_sync(*backend, method, GLib.Variant('(b)', (False,)), None, Gio.DBusCallFlags.NONE, 30000, None)
            except GLib.Error as error:
                remote = Gio.DBusError.get_remote_error(error) or ''
                if backend[0].endswith('ConsoleKit') and remote.endswith('UnknownMethod') and action != 'suspend':
                    bus.call_sync(*backend, 'Stop' if action == 'shutdown' else 'Restart', None, None, Gio.DBusCallFlags.NONE, 30000, None)
                else:
                    raise
        except Exception as error:
            remote = Gio.DBusError.get_remote_error(error) if isinstance(error, GLib.Error) else ''
            refused = any(word in (remote or '') + str(error) for word in ('AccessDenied', 'NotAuthorized', 'Auth', 'Permission', 'Inhibited'))
            message = _('The system action was refused by the session manager.') if refused else _('Unable to perform the system action. See the console for details.')
            self.worker_error(message, str(error))

    def play_sound(self):
        mode = self.settings.get_string('notification-sound')
        if mode == 'none':
            return
        try:
            if self.media:
                self.media.pause()
            if mode == 'custom':
                path = self.settings.get_string('custom-sound-path')
                if not path or not Path(path).is_file():
                    raise FileNotFoundError(path)
                self.media = Gtk.MediaFile.new_for_filename(path)
            else:
                self.media = Gtk.MediaFile.new_for_resource('/io/github/aealith2839/stopw/complete.ogg')
            self.media.connect('notify::error', self.audio_error, mode)
            self.media.play()
            if self.media.get_error():
                self.audio_error(self.media, None, mode)
        except Exception as error:
            message = _('Unable to play the custom audio file.') if mode == 'custom' else _('Unable to play the default sound.')
            self.report(message, error)

    def audio_error(self, media, spec, mode):
        error = media.get_error()
        if error and media is self.media:
            self.media = None
            message = _('Unable to play the custom audio file.') if mode == 'custom' else _('Unable to play the default sound.')
            self.report(message, error)

    def command_worker(self, command):
        process = None
        try:
            # A dedicated process group allows the timeout and application
            # shutdown to stop ordinary shell descendants as well, not only
            # /bin/sh itself.
            with self.process_lock:
                if self.closed:
                    return
                process = subprocess.Popen(['/bin/sh', '-c', command], cwd=str(Path.home()), start_new_session=True)
                self.command_processes.add(process)
        except Exception as error:
            self.worker_error(_('Unable to run the custom command. Check it in a terminal.'), str(error))
            return
        try:
            code = process.wait(timeout=CUSTOM_COMMAND_TIMEOUT_SECONDS)
            if code != 0 and not self.closed:
                self.worker_error(_('The custom command failed. See the console for details.'), f'Exit code: {code}. See stdout/stderr inherited from the terminal.')
        except subprocess.TimeoutExpired:
            self.kill_group(process)
            process.wait()
            self.worker_error(_('The custom command was interrupted after 30 seconds.'), 'Command timeout.')
        finally:
            with self.process_lock:
                self.command_processes.discard(process)

    @staticmethod
    def kill_group(process):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def close_requested(self, *_):
        self.cleanup()
        return False

    def cleanup(self):
        if self.closed:
            return
        self.closed = True
        self.active = False
        self.remove_tick()
        if self.selection_source is not None:
            GLib.source_remove(self.selection_source)
            self.selection_source = None
        # Invalidate pending replies and remove lock-state monitoring, in
        # addition to sleep monitoring.
        self.lock_check_serial += 1
        if self.bus is not None and self.lock_subscription is not None:
            self.bus.signal_unsubscribe(self.lock_subscription)
            self.lock_subscription = None    
        if self.bus is not None and self.sleep_subscription is not None:
            self.bus.signal_unsubscribe(self.sleep_subscription)
            self.sleep_subscription = None
        if self.media:
            self.media.pause()
        with self.process_lock:
            for process in self.command_processes:
                self.kill_group(process)


class StopwApplication(Adw.Application):
    """Stable identifier for the single instance and GNOME notifications."""
    def __init__(self):
        super().__init__(application_id=APP_ID)
        self.window = None
        self.resource = None

    def do_activate(self):
        if self.window is not None and not self.window.closed:
            self.window.present()
            return
        try:
            if Gtk.get_minor_version() < 10:
                raise RuntimeError('GTK 4.10 or newer is required.')
            settings = load_settings()
            # The compiled resource bundle is registered before any audio
            # playback.
            resource_paths = [BASE_DIR / 'build' / 'io.github.aealith2839.stopw.gresource',
                              BASE_DIR / 'io.github.aealith2839.stopw.gresource',
                              SHARE_DIR / 'stopw' / 'io.github.aealith2839.stopw.gresource']
            resource_path = next((path for path in resource_paths if path.is_file()), None)
            if resource_path and self.resource is None:
                self.resource = Gio.Resource.load(str(resource_path))
                Gio.resources_register(self.resource)
            self.window = StopwWindow(self, settings)
            if resource_path is None:
                self.window.report(_('The stopw audio resources are missing. Build the project.'), resource_paths)
            self.window.present()
        except Exception as error:
            print(f'stopw : {error}', file=sys.stderr)
            notification = Gio.Notification.new(APP_NAME)
            notification.set_body(str(error))
            self.send_notification('startup-error', notification)
            self.quit()

    def do_shutdown(self):
        if self.window:
            self.window.cleanup()
        Adw.Application.do_shutdown(self)


if __name__ == '__main__':
    sys.exit(StopwApplication().run(sys.argv))
