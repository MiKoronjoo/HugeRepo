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

        except (BlockingIOError, InterruptedError):

            if registered_fd is None:

                self._sock_add_cancellation_callback(fut, sock)

            self.add_writer(fd, self._sock_sendfile_native_impl, fut,

                            fd, sock, fileno,

                            offset, count, blocksize, total_sent)

        except OSError as exc:

            if (registered_fd is not None and

                    exc.errno == errno.ENOTCONN and

                    type(exc) is not ConnectionError):

                # If we have an ENOTCONN and this isn't a first call to

                # sendfile(), i.e. the connection was closed in the middle

                # of the operation, normalize the error to ConnectionError

                # to make it consistent across all Posix systems.

                new_exc = ConnectionError(

                    'socket is not connected', errno.ENOTCONN)

                new_exc.__cause__ = exc

                exc = new_exc

            if total_sent == 0:

                # We can get here for different reasons, the main

                # one being 'file' is not a regular mmap(2)-like

                # file, in which case we'll fall back on using

                # plain send().

                err = exceptions.SendfileNotAvailableError(

                    'os.sendfile call failed')

                self._sock_sendfile_update_filepos(fileno, offset, total_sent)

                fut.set_exception(err)

            else:

                self._sock_sendfile_update_filepos(fileno, offset, total_sent)

                fut.set_exception(exc)

        except (SystemExit, KeyboardInterrupt):

            raise

        except BaseException as exc:

            self._sock_sendfile_update_filepos(fileno, offset, total_sent)

            fut.set_exception(exc)

        else:

            if sent == 0:

                # EOF

                self._sock_sendfile_update_filepos(fileno, offset, total_sent)

                fut.set_result(total_sent)

            else:

                offset += sent

                total_sent += sent

                if registered_fd is None:

                    self._sock_add_cancellation_callback(fut, sock)

                self.add_writer(fd, self._sock_sendfile_native_impl, fut,

                                fd, sock, fileno,

                                offset, count, blocksize, total_sent)



    def _sock_sendfile_update_filepos(self, fileno, offset, total_sent):

        if total_sent > 0:

            os.lseek(fileno, offset, os.SEEK_SET)



    def _sock_add_cancellation_callback(self, fut, sock):

        def cb(fut):

            if fut.cancelled():

                fd = sock.fileno()

                if fd != -1:

                    self.remove_writer(fd)

        fut.add_done_callback(cb)





class _UnixReadPipeTransport(transports.ReadTransport):



    max_size = 256 * 1024  # max bytes we read in one event loop iteration



    def __init__(self, loop, pipe, protocol, waiter=None, extra=None):

        super().__init__(extra)

        self._extra['pipe'] = pipe

        self._loop = loop

        self._pipe = pipe

        self._fileno = pipe.fileno()

        self._protocol = protocol

        self._closing = False

        self._paused = False



        mode = os.fstat(self._fileno).st_mode

        if not (stat.S_ISFIFO(mode) or

                stat.S_ISSOCK(mode) or

                stat.S_ISCHR(mode)):

            self._pipe = None

            self._fileno = None

            self._protocol = None

            raise ValueError('Pipe transport is for pipes/sockets only.')



        os.set_blocking(self._fileno, False)



        self._loop.call_soon(self._protocol.connection_made, self)

        # only start reading when connection_made() has been called

        self._loop.call_soon(self._loop._add_reader,

                             self._fileno, self._read_ready)

        if waiter is not None:

            # only wake up the waiter when connection_made() has been called

            self._loop.call_soon(futures._set_result_unless_cancelled,

                                 waiter, None)



    def __repr__(self):

        info = [self.__class__.__name__]

        if self._pipe is None:

            info.append('closed')

        elif self._closing:

            info.append('closing')

        info.append(f'fd={self._fileno}')

        selector = getattr(self._loop, '_selector', None)

        if self._pipe is not None and selector is not None:

            polling = selector_events._test_selector_event(

                selector, self._fileno, selectors.EVENT_READ)

            if polling:

                info.append('polling')

            else:

                info.append('idle')

        elif self._pipe is not None:

            info.append('open')

        else:

            info.append('closed')

        return '<{}>'.format(' '.join(info))



    def _read_ready(self):

        try:

            data = os.read(self._fileno, self.max_size)

        except (BlockingIOError, InterruptedError):

            pass

        except OSError as exc:

            self._fatal_error(exc, 'Fatal read error on pipe transport')

        else:

            if data:

                self._protocol.data_received(data)

            else:

                if self._loop.get_debug():

                    logger.info('%r was closed by peer', self)

                self._closing = True

                self._loop._remove_reader(self._fileno)

                self._loop.call_soon(self._protocol.eof_received)

                self._loop.call_soon(self._call_connection_lost, None)



    def pause_reading(self):

        if self._closing or self._paused:

            return

        self._paused = True

        self._loop._remove_reader(self._fileno)

        if self._loop.get_debug():

            logger.debug('%r pauses reading', self)



    def resume_reading(self):

        if self._closing or not self._paused:

            return

        self._paused = False

        self._loop._add_reader(self._fileno, self._read_ready)

        if self._loop.get_debug():

            logger.debug('%r resumes reading', self)



    def set_protocol(self, protocol):

        self._protocol = protocol



    def get_protocol(self):

        return self._protocol



    def is_closing(self):

        return self._closing



    def close(self):

        if not self._closing:

            self._close(None)



    def __del__(self, _warn=warnings.warn):

        if self._pipe is not None:

            _warn(f'unclosed transport {self!r}', ResourceWarning, source=self)

            self._pipe.close()



    def _fatal_error(self, exc, message='Fatal error on pipe transport'):

        # should be called by exception handler only

        if (isinstance(exc, OSError) and exc.errno == errno.EIO):

            if self._loop.get_debug():

                logger.debug('%r: %s', self, message, exc_info=True)

        else:

