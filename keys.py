"""Read one keypress without waiting for Enter."""
from __future__ import annotations

import sys
import termios
import tty
from typing import TextIO


def read_key(stdin: TextIO = sys.stdin, stdout: TextIO = sys.stdout) -> str:
    """Return the key pressed, or "" when input is closed or interrupted (Ctrl+C/Ctrl+D)."""
    try:
        if not stdin.isatty():
            return stdin.readline()[:1]
        fd = stdin.fileno()
        saved = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)  # keeps Ctrl+C working, unlike raw mode
            key = stdin.read(1)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)
    except (KeyboardInterrupt, EOFError):
        return ""
    if key.isprintable():
        print(key, file=stdout)
    return key
