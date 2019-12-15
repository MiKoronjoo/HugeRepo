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

            # number in the wakup file descriptor. _process_self_data() will

            # read signal numbers from this file descriptor to handle signals.

            signal.signal(sig, _sighandler_noop)



            # Set SA_RESTART to limit EINTR occurrences.

            signal.siginterrupt(sig, False)

        except OSError as exc:

            del self._signal_handlers[sig]

            if not self._signal_handlers:

                try:

                    signal.set_wakeup_fd(-1)

                except (ValueError, OSError) as nexc:

                    logger.info('set_wakeup_fd(-1) failed: %s', nexc)



            if exc.errno == errno.EINVAL:

                raise RuntimeError(f'sig {sig} cannot be caught')

            else:

                raise



    def _handle_signal(self, sig):

        '''Internal helper that is the actual signal handler.'''

        handle = self._signal_handlers.get(sig)

        if handle is None:

            return  # Assume it's some race condition.

        if handle._cancelled:

            self.remove_signal_handler(sig)  # Remove it properly.

        else:

            self._add_callback_signalsafe(handle)



    def remove_signal_handler(self, sig):

        '''Remove a handler for a signal.  UNIX only.

        Return True if a signal handler was removed, False if not.

        '''

        self._check_signal(sig)

        try:

            del self._signal_handlers[sig]

        except KeyError:

            return False



        if sig == signal.SIGINT:

            handler = signal.default_int_handler

        else:

            handler = signal.SIG_DFL



        try:

            signal.signal(sig, handler)

        except OSError as exc:

            if exc.errno == errno.EINVAL:

                raise RuntimeError(f'sig {sig} cannot be caught')

            else:

                raise



        if not self._signal_handlers:

            try:

                signal.set_wakeup_fd(-1)

            except (ValueError, OSError) as exc:

                logger.info('set_wakeup_fd(-1) failed: %s', exc)



        return True



    def _check_signal(self, sig):

        '''Internal helper to validate a signal.

        Raise ValueError if the signal number is invalid or uncatchable.

        Raise RuntimeError if there is a problem setting up the handler.

        '''

        if not isinstance(sig, int):

            raise TypeError(f'sig must be an int, not {sig!r}')



        if sig not in signal.valid_signals():

            raise ValueError(f'invalid signal number {sig}')



    def _make_read_pipe_transport(self, pipe, protocol, waiter=None,

                                  extra=None):

        return _UnixReadPipeTransport(self, pipe, protocol, waiter, extra)



    def _make_write_pipe_transport(self, pipe, protocol, waiter=None,

                                   extra=None):

        return _UnixWritePipeTransport(self, pipe, protocol, waiter, extra)



    async def _make_subprocess_transport(self, protocol, args, shell,

                                         stdin, stdout, stderr, bufsize,

                                         extra=None, **kwargs):

        with events.get_child_watcher() as watcher:

            if not watcher.is_active():

                # Check early.

                # Raising exception before process creation

                # prevents subprocess execution if the watcher

                # is not ready to handle it.

                raise RuntimeError('asyncio.get_child_watcher() is not activated, '

                                   'subprocess support is not installed.')

            waiter = self.create_future()

            transp = _UnixSubprocessTransport(self, protocol, args, shell,

                                              stdin, stdout, stderr, bufsize,

                                              waiter=waiter, extra=extra,

                                              **kwargs)



            watcher.add_child_handler(transp.get_pid(),

                                      self._child_watcher_callback, transp)

            try:

                await waiter

            except (SystemExit, KeyboardInterrupt):

                raise

            except BaseException:

                transp.close()

                await transp._wait()

                raise



        return transp



    def _child_watcher_callback(self, pid, returncode, transp):

        self.call_soon_threadsafe(transp._process_exited, returncode)



    async def create_unix_connection(

            self, protocol_factory, path=None, *,

            ssl=None, sock=None,

            server_hostname=None,

            ssl_handshake_timeout=None):

        assert server_hostname is None or isinstance(server_hostname, str)

        if ssl:

            if server_hostname is None:

                raise ValueError(

                    'you have to pass server_hostname when using ssl')

        else:

            if server_hostname is not None:

                raise ValueError('server_hostname is only meaningful with ssl')

            if ssl_handshake_timeout is not None:

                raise ValueError(

                    'ssl_handshake_timeout is only meaningful with ssl')



        if path is not None:

            if sock is not None:

                raise ValueError(

                    'path and sock can not be specified at the same time')



            path = os.fspath(path)

            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM, 0)

            try:

                sock.setblocking(False)

                await self.sock_connect(sock, path)

            except:

                sock.close()

                raise



        else:

            if sock is None:

                raise ValueError('no path and sock were specified')

            if (sock.family != socket.AF_UNIX or

                    sock.type != socket.SOCK_STREAM):

                raise ValueError(

                    f'A UNIX Domain Stream Socket was expected, got {sock!r}')

            sock.setblocking(False)



        transport, protocol = await self._create_connection_transport(

            sock, protocol_factory, ssl, server_hostname,

            ssl_handshake_timeout=ssl_handshake_timeout)

        return transport, protocol



    async def create_unix_server(

            self, protocol_factory, path=None, *,

            sock=None, backlog=100, ssl=None,

            ssl_handshake_timeout=None,

            start_serving=True):

        if isinstance(ssl, bool):

            raise TypeError('ssl argument must be an SSLContext or None')



        if ssl_handshake_timeout is not None and not ssl:

            raise ValueError(

                'ssl_handshake_timeout is only meaningful with ssl')



        if path is not None:

            if sock is not None:

                raise ValueError(

                    'path and sock can not be specified at the same time')



            path = os.fspath(path)

            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)



            # Check for abstract socket.  and  paths are supported.

            if path[0] not in (0, '\x00'):

                try:

                    if stat.S_ISSOCK(os.stat(path).st_mode):

                        os.remove(path)

                except FileNotFoundError:

                    pass

                except OSError as err:

                    # Directory may have permissions only to create socket.

                    logger.error('Unable to check or remove stale UNIX socket '

                                 '%r: %r', path, err)



            try:

                sock.bind(path)

            except OSError as exc:

                sock.close()

                if exc.errno == errno.EADDRINUSE:

                    # Let's improve the error message by adding

                    # with what exact address it occurs.

                    msg = f'Address {path!r} is already in use'

                    raise OSError(errno.EADDRINUSE, msg) from None

                else:

                    raise

            except:

                sock.close()

                raise

        else:

            if sock is None:

                raise ValueError(

                    'path was not specified, and no sock specified')



            if (sock.family != socket.AF_UNIX or

                    sock.type != socket.SOCK_STREAM):

                raise ValueError(

                    f'A UNIX Domain Stream Socket was expected, got {sock!r}')



        sock.setblocking(False)

        server = base_events.Server(self, [sock], protocol_factory,

                                    ssl, backlog, ssl_handshake_timeout)

        if start_serving:

            server._start_serving()

            # Skip one loop iteration so that all 'loop.add_reader'

            # go through.

            await tasks.sleep(0, loop=self)



        return server



    async def _sock_sendfile_native(self, sock, file, offset, count):

        try:

            os.sendfile

        except AttributeError:

            raise exceptions.SendfileNotAvailableError(

                'os.sendfile() is not available')

        try:

            fileno = file.fileno()

        except (AttributeError, io.UnsupportedOperation) as err:

            raise exceptions.SendfileNotAvailableError('not a regular file')

        try:

            fsize = os.fstat(fileno).st_size

        except OSError:

            raise exceptions.SendfileNotAvailableError('not a regular file')

        blocksize = count if count else fsize

        if not blocksize:

            return 0  # empty file



        fut = self.create_future()

        self._sock_sendfile_native_impl(fut, None, sock, fileno,

                                        offset, count, blocksize, 0)

        return await fut



    def _sock_sendfile_native_impl(self, fut, registered_fd, sock, fileno,

                                   offset, count, blocksize, total_sent):

        fd = sock.fileno()

        if registered_fd is not None:

            # Remove the callback early.  It should be rare that the

            # selector says the fd is ready but the call still returns

            # EAGAIN, and I am willing to take a hit in that case in

            # order to simplify the common case.

            self.remove_writer(registered_fd)

        if fut.cancelled():

            self._sock_sendfile_update_filepos(fileno, offset, total_sent)

            return

        if count:

            blocksize = count - total_sent

            if blocksize <= 0:

                self._sock_sendfile_update_filepos(fileno, offset, total_sent)

                fut.set_result(total_sent)

                return



        try:

            sent = os.sendfile(fd, fileno, offset, blocksize)

