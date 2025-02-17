# Copyright (C) 2010-2023 Free Software Foundation, Inc.

# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import signal
import threading
import traceback
import os
import sys
import _gdb
from contextlib import contextmanager

# Python 3 moved "reload"
if sys.version_info >= (3, 4):
    from importlib import reload
else:
    from imp import reload

from _gdb import *

# Historically, gdb.events was always available, so ensure it's
# still available without an explicit import.
import _gdbevents as events

sys.modules["gdb.events"] = events


class _GdbFile(object):
    # These two are needed in Python 3
    encoding = "UTF-8"
    errors = "strict"

    def __init__(self, stream):
        self.stream = stream

    def close(self):
        # Do nothing.
        return None

    def isatty(self):
        return False

    def writelines(self, iterable):
        for line in iterable:
            self.write(line)

    def flush(self):
        flush(stream=self.stream)

    def write(self, s):
        write(s, stream=self.stream)


sys.stdout = _GdbFile(STDOUT)

sys.stderr = _GdbFile(STDERR)

# Default prompt hook does nothing.
prompt_hook = None

# Ensure that sys.argv is set to something.
# We do not use PySys_SetArgvEx because it did not appear until 2.6.6.
sys.argv = [""]

# Initial pretty printers.
try:
    pretty_printers
except NameError:
    pretty_printers = []

# Initial type printers.
try:
    type_printers
except NameError:
    type_printers = []
# Initial xmethod matchers.
try:
    xmethods
except NameError:
    xmethods = []
# Initial frame filters.
try:
    frame_filters
except NameError:
    frame_filters = {}
# Initial frame unwinders.
try:
    frame_unwinders
except NameError:
    frame_unwinders = []


def _execute_unwinders(pending_frame):
    """Internal function called from GDB to execute all unwinders.

    Runs each currently enabled unwinder until it finds the one that
    can unwind given frame.

    Arguments:
        pending_frame: gdb.PendingFrame instance.

    Returns:
        Tuple with:

          [0] gdb.UnwindInfo instance
          [1] Name of unwinder that claimed the frame (type `str`)

        or None, if no unwinder has claimed the frame.
    """
    for objfile in objfiles():
        for unwinder in objfile.frame_unwinders:
            if unwinder.enabled:
                unwind_info = unwinder(pending_frame)
                if unwind_info is not None:
                    return (unwind_info, unwinder.name)

    for unwinder in current_progspace().frame_unwinders:
        if unwinder.enabled:
            unwind_info = unwinder(pending_frame)
            if unwind_info is not None:
                return (unwind_info, unwinder.name)

    for unwinder in frame_unwinders:
        if unwinder.enabled:
            unwind_info = unwinder(pending_frame)
            if unwind_info is not None:
                return (unwind_info, unwinder.name)

    return None


def _execute_file(filepath):
    """This function is used to replace Python 2's PyRun_SimpleFile.

    Loads and executes the given file.

    We could use the runpy module, but its documentation says:
    "Furthermore, any functions and classes defined by the executed code are
    not guaranteed to work correctly after a runpy function has returned."
    """
    globals = sys.modules["__main__"].__dict__
    set_file = False
    # Set file (if not set) so that the imported file can use it (e.g. to
    # access file-relative paths). This matches what PyRun_SimpleFile does.
    if not hasattr(globals, "__file__"):
        globals["__file__"] = filepath
        set_file = True
    try:
        with open(filepath, "rb") as file:
            # We pass globals also as locals to match what Python does
            # in PyRun_SimpleFile.
            compiled = compile(file.read(), filepath, "exec")
            exec(compiled, globals, globals)
    finally:
        if set_file:
            del globals["__file__"]


# Convenience variable to GDB's python directory
PYTHONDIR = os.path.dirname(os.path.dirname(__file__))

# Auto-load all functions/commands.

# Packages to auto-load.

packages = ["function", "command", "printer"]

# pkgutil.iter_modules is not available prior to Python 2.6.  Instead,
# manually iterate the list, collating the Python files in each module
# path.  Construct the module name, and import.


def _auto_load_packages():
    for package in packages:
        location = os.path.join(os.path.dirname(__file__), package)
        if os.path.exists(location):
            py_files = filter(
                lambda x: x.endswith(".py") and x != "__init__.py", os.listdir(location)
            )

            for py_file in py_files:
                # Construct from foo.py, gdb.module.foo
                modname = "%s.%s.%s" % (__name__, package, py_file[:-3])
                try:
                    if modname in sys.modules:
                        # reload modules with duplicate names
                        reload(__import__(modname))
                    else:
                        __import__(modname)
                except:
                    sys.stderr.write(traceback.format_exc() + "\n")


_auto_load_packages()


def GdbSetPythonDirectory(dir):
    """Update sys.path, reload gdb and auto-load packages."""
    global PYTHONDIR

    try:
        sys.path.remove(PYTHONDIR)
    except ValueError:
        pass
    sys.path.insert(0, dir)

    PYTHONDIR = dir

    # note that reload overwrites the gdb module without deleting existing
    # attributes
    reload(__import__(__name__))
    _auto_load_packages()


def current_progspace():
    "Return the current Progspace."
    return selected_inferior().progspace


def objfiles():
    "Return a sequence of the current program space's objfiles."
    return current_progspace().objfiles()


def solib_name(addr):
    """solib_name (Long) -> String.\n\
Return the name of the shared library holding a given address, or None."""
    return current_progspace().solib_name(addr)


def block_for_pc(pc):
    "Return the block containing the given pc value, or None."
    return current_progspace().block_for_pc(pc)


def find_pc_line(pc):
    """find_pc_line (pc) -> Symtab_and_line.
    Return the gdb.Symtab_and_line object corresponding to the pc value."""
    return current_progspace().find_pc_line(pc)


def set_parameter(name, value):
    """Set the GDB parameter NAME to VALUE."""
    # Handle the specific cases of None and booleans here, because
    # gdb.parameter can return them, but they can't be passed to 'set'
    # this way.
    if value is None:
        value = "unlimited"
    elif isinstance(value, bool):
        if value:
            value = "on"
        else:
            value = "off"
    execute("set " + name + " " + str(value), to_string=True)


@contextmanager
def with_parameter(name, value):
    """Temporarily set the GDB parameter NAME to VALUE.
    Note that this is a context manager."""
    old_value = parameter(name)
    set_parameter(name, value)
    try:
        # Nothing that useful to return.
        yield None
    finally:
        set_parameter(name, old_value)


@contextmanager
def blocked_signals():
    """A helper function that blocks and unblocks signals."""
    if not hasattr(signal, "pthread_sigmask"):
        yield
        return

    to_block = {signal.SIGCHLD, signal.SIGINT, signal.SIGALRM, signal.SIGWINCH}
    old_mask = signal.pthread_sigmask(signal.SIG_BLOCK, to_block)
    try:
        yield None
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, old_mask)


class Thread(threading.Thread):
    """A GDB-specific wrapper around threading.Thread

    This wrapper ensures that the new thread blocks any signals that
    must be delivered on GDB's main thread."""

    def start(self):
        # GDB requires that these be delivered to the main thread.  We
        # do this here to avoid any possible race with the creation of
        # the new thread.  The thread mask is inherited by new
        # threads.
        with blocked_signals():
            super().start()
