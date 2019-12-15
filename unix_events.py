'''Selector event loop for Unix with signal handling.'''



import errno

import io

import itertools

import os

import selectors

import signal

import socket

import stat

import subprocess

import sys

import threading

import warnings



from . import base_events

from . import base_subprocess

from . import constants

from . import coroutines

from . import events

from . import exceptions

from . import futures

from . import selector_events

from . import tasks

from . import transports

from .log import logger





__all__ = (

    'SelectorEventLoop',

    'AbstractChildWatcher', 'SafeChildWatcher',

    'FastChildWatcher', 'PidfdChildWatcher',

    'MultiLoopChildWatcher', 'ThreadedChildWatcher',

    'DefaultEventLoopPolicy',

)





if sys.platform == 'win32':  # pragma: no cover

    raise ImportError('Signals are not really supported on Windows')





def _sighandler_noop(signum, frame):

    '''Dummy signal handler.'''

    pass





class _UnixSelectorEventLoop(selector_events.BaseSelectorEventLoop):

    '''Unix event loop.

    Adds signal handling and UNIX Domain Socket support to SelectorEventLoop.

    '''



    def __init__(self, selector=None):

        super().__init__(selector)

        self._signal_handlers = {}



    def close(self):

        super().close()

        if not sys.is_finalizing():

            for sig in list(self._signal_handlers):

                self.remove_signal_handler(sig)

        else:

            if self._signal_handlers:

                warnings.warn(f'Closing the loop {self!r} '

                              f'on interpreter shutdown '

