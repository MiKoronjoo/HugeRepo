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

                              f'stage, skipping signal handlers removal',

                              ResourceWarning,

                              source=self)

                self._signal_handlers.clear()



    def _process_self_data(self, data):

        for signum in data:

            if not signum:

                # ignore null bytes written by _write_to_self()

                continue

            self._handle_signal(signum)



    def add_signal_handler(self, sig, callback, *args):

        '''Add a handler for a signal.  UNIX only.

        Raise ValueError if the signal number is invalid or uncatchable.

        Raise RuntimeError if there is a problem setting up the handler.

        '''

        if (coroutines.iscoroutine(callback) or

                coroutines.iscoroutinefunction(callback)):

            raise TypeError('coroutines cannot be used '

                            'with add_signal_handler()')

        self._check_signal(sig)

        self._check_closed()

        try:

            # set_wakeup_fd() raises ValueError if this is not the

            # main thread.  By calling it early we ensure that an

            # event loop running in another thread cannot add a signal

            # handler.

            signal.set_wakeup_fd(self._csock.fileno())

        except (ValueError, OSError) as exc:

            raise RuntimeError(str(exc))



        handle = events.Handle(callback, args, self, None)

        self._signal_handlers[sig] = handle



        try:

            # Register a dummy signal handler to ask Python to write the signal

