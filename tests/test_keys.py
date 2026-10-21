import io
import os
import pty
import threading

import keys


def type_later(master, data: bytes):
    # Keys typed before the prompt appears are flushed on purpose (stray typeahead
    # must not answer a destructive question), so the test types after the read starts.
    threading.Timer(0.3, os.write, (master, data)).start()


def test_reads_one_key_from_a_terminal_without_waiting_for_enter():
    # The whole point: a single keypress must be enough, no newline follows it.
    master, slave = pty.openpty()
    type_later(master, b"y")
    with os.fdopen(slave, "r", closefd=False) as tty:
        assert keys.read_key(tty, io.StringIO()) == "y"


def test_escape_key_is_returned_as_is():
    master, slave = pty.openpty()
    type_later(master, b"\x1b")
    with os.fdopen(slave, "r", closefd=False) as tty:
        assert keys.read_key(tty, io.StringIO()) == "\x1b"


def test_terminal_settings_are_restored_after_reading():
    import termios
    master, slave = pty.openpty()
    mask = termios.ICANON | termios.ECHO | termios.ISIG  # the kernel also toggles status bits like PENDIN
    before = termios.tcgetattr(slave)[3] & mask
    type_later(master, b"n")
    with os.fdopen(slave, "r", closefd=False) as tty:
        keys.read_key(tty, io.StringIO())
    assert termios.tcgetattr(slave)[3] & mask == before


def test_non_terminal_input_falls_back_to_first_character_of_a_line():
    assert keys.read_key(io.StringIO("y\n"), io.StringIO()) == "y"


def test_closed_input_counts_as_no_answer():
    assert keys.read_key(io.StringIO(""), io.StringIO()) == ""
