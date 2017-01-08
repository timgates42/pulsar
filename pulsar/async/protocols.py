import asyncio
from socket import SOL_SOCKET, SO_KEEPALIVE

from async_timeout import timeout

from pulsar.utils.internet import nice_address, format_address

from .access import LOGGER
from .events import EventHandler, AbortEvent
from .mixins import FlowControl, Timeout


__all__ = ['ProtocolConsumer',
           'Protocol',
           'DatagramProtocol',
           'Connection',
           'Producer',
           'TcpServer',
           'DatagramServer',
           'AbortRequest']


CLOSE_TIMEOUT = 3


class AbortRequest(AbortEvent):
    pass


class ProtocolConsumer(EventHandler):
    """The consumer of data for a server or client :class:`.Connection`.

    It is responsible for receiving incoming data from an end point via the
    :meth:`Connection.data_received` method, decoding (parsing) and,
    possibly, writing back to the client or server via
    the :attr:`transport` attribute.

    .. note::

        For server consumers, :meth:`data_received` is the only method
        to implement.
        For client consumers, :meth:`start_request` should also be implemented.

    A :class:`ProtocolConsumer` is a subclass of :class:`.EventHandler` and it
    has two default :ref:`one time events <one-time-event>`:

    * ``pre_request`` fired when the request is received (for servers) or
      just before is sent (for clients).
      This occurs just before the :meth:`start_request` method.
    * ``post_request`` fired when the request is done. The
      :attr:`on_finished` attribute is a shortcut for the ``post_request``
      :class:`.OneTime` event and therefore can be used to wait for
      the request to have received a full response (clients).

    In addition, it has two :ref:`many times events <many-times-event>`:

    * ``data_received`` fired when new data is received from the transport but
      not yet processed (before the :meth:`data_received` method is invoked)
    * ``data_processed`` fired just after data has been consumed (after the
      :meth:`data_received` method)

    .. note::

        A useful example on how to use the ``data_received`` event is
        the :ref:`wsgi proxy server <tutorials-proxy-server>`.
    """
    _connection = None
    _data_received_count = 0
    ONE_TIME_EVENTS = ('pre_request', 'post_request')

    @property
    def connection(self):
        """The :class:`Connection` of this consumer.
        """
        return self._connection

    @property
    def request(self):
        """The request.

        Used for clients only and available only after the
        :meth:`start` method is invoked.
        """
        return getattr(self, '_request', None)

    @property
    def transport(self):
        """The :class:`Transport` of this consumer
        """
        if self._connection:
            return self._connection.transport

    @property
    def address(self):
        if self._connection:
            return self._connection.address

    @property
    def producer(self):
        """The :class:`Producer` of this consumer.
        """
        if self._connection:
            return self._connection.producer

    def finished(self, exc=None):
        """Event fired once a full response to a request is received. It is
        the ``post_request`` one time event.
        """
        self.event('post_request').fire(exc=exc)

    def data_received(self, data):
        """Called when some data is received.

        **This method must be implemented by subclasses** for both server and
        client consumers.

        The argument is a bytes object.
        """

    def start_request(self):
        """Starts a new request.

        Invoked by the :meth:`start` method to kick start the
        request with remote server. For server :class:`ProtocolConsumer` this
        method is not invoked at all.

        **For clients this method should be implemented** and it is critical
        method where errors caused by stale socket connections can arise.
        **This method should not be called directly.** Use :meth:`start`
        instead. Typically one writes some data from the :attr:`request`
        into the transport. Something like this::

            self.transport.write(self.request.encode())
        """
        raise NotImplementedError

    def start(self, request=None):
        """Starts processing the request for this protocol consumer.

        There is no need to override this method,
        implement :meth:`start_request` instead.
        If either :attr:`connection` or :attr:`transport` are missing, a
        :class:`RuntimeError` occurs.

        For server side consumer, this method simply fires the ``pre_request``
        event.
        """
        conn = self._connection
        conn._processed += 1
        if conn._producer:
            p = getattr(conn._producer, '_requests_processed', 0)
            conn._producer._requests_processed = p + 1
        self.event('post_request').bind(self._finished)
        self._request = request
        try:
            self.event('pre_request').fire()
        except AbortEvent:
            self.logger.debug('Abort request %s', request)
        else:
            if self._request is not None:
                try:
                    self.start_request()
                except Exception as exc:
                    self.finished(exc=exc)

    def abort_request(self):
        """Abort the request.

        This method can be called during the pre-request stage
        """
        raise AbortRequest

    def write(self, data):
        """Delegate writing to the underlying :class:`.Connection`

        Return an empty tuple or a :class:`~asyncio.Future`
        """
        c = self._connection
        if c:
            return c.write(data)
        else:
            raise RuntimeError('No connection')

    def _data_received(self, data):
        # Called by Connection, it updates the counters and invoke
        # the high level data_received method which must be implemented
        # by subclasses
        if not hasattr(self, '_request'):
            self.start()
        self._data_received_count += 1
        result = self.data_received(data)
        self.event('data_processed').fire(data=data)
        return result

    def _finished(self, _, exc=None):
        c = self._connection
        if c and c._current_consumer is self:
            c._current_consumer = None


class PulsarProtocol(EventHandler, FlowControl):
    """A mixin class for both :class:`.Protocol` and
    :class:`.DatagramProtocol`.

    A :class:`PulsarProtocol` is an :class:`.EventHandler` which has
    two :ref:`one time events <one-time-event>`:

    * ``connection_made``
    * ``connection_lost``
    """
    ONE_TIME_EVENTS = ('connection_made', 'connection_lost')

    _transport = None
    _address = None
    _closed = None

    def __init__(self, *, loop=None, session=1, producer=None,
                 low_limit=None, high_limit=None, logger=None, **kw):
        self.logger = logger or LOGGER
        self.session = session
        self._low_limit = low_limit
        self._high_limit = high_limit
        self._loop = loop
        self._producer = producer
        self._low_limit = low_limit
        self._high_limit = high_limit
        self.event('connection_made').bind(self._set_flow_limits)
        self.event('connection_lost').bind(self._wakeup_waiter)

    def __repr__(self):
        address = self._address
        if address:
            return '%s session %s' % (nice_address(address), self.session)
        else:
            return '<pending> session %s' % self.session
    __str__ = __repr__

    @property
    def transport(self):
        """The :ref:`transport <asyncio-transport>` for this protocol.

        Available once the :meth:`connection_made` is called.
        """
        return self._transport

    @property
    def sock(self):
        """The socket of :attr:`transport`.
        """
        if self._transport:
            return self._transport.get_extra_info('socket')

    @property
    def address(self):
        """The address of the :attr:`transport`.
        """
        return self._address

    @property
    def producer(self):
        """The producer of this :class:`Protocol`.
        """
        return self._producer

    @property
    def closed(self):
        """``True`` if the :attr:`transport` is closed.
        """
        if self._transport:
            if hasattr(self._transport, 'is_closing'):
                return self._transport.is_closing()
            return False
        return True

    def close(self):
        """Close by closing the :attr:`transport`

        Return the ``connection_lost`` event which can be used to wait
        for complete transport closure.
        """
        if not self._closed:
            closed = False
            event = self.event('connection_lost')
            if self._transport:
                if self._loop.get_debug():
                    self.logger.debug('Closing connection %s', self)
                if self._transport.can_write_eof():
                    try:
                        self._transport.write_eof()
                    except Exception:
                        pass
                try:
                    self._transport.close()
                    closed = self._loop.create_task(
                        self._close(event.waiter())
                    )
                except Exception:
                    pass
            if not closed:
                self.event('connection_lost').fire()
            self._closed = closed or True

    def abort(self):
        """Abort by aborting the :attr:`transport`
        """
        if self._transport:
            self._transport.abort()
        self.event('connection_lost').fire()

    def connection_made(self, transport):
        """Sets the :attr:`transport`, fire the ``connection_made`` event
        and adds a :attr:`timeout` for idle connections.
        """
        self._transport = transport
        addr = self._transport.get_extra_info('peername')
        if not addr:
            addr = self._transport.get_extra_info('sockname')
        self._address = addr
        sock = transport.get_extra_info('socket')
        try:
            sock.setsockopt(SOL_SOCKET, SO_KEEPALIVE, 1)
        except (OSError, NameError):
            pass
        # let everyone know we have a connection with endpoint
        self.event('connection_made').fire()

    def connection_lost(self, _, exc=None):
        """Fires the ``connection_lost`` event.
        """
        self.event('connection_lost').fire()

    def eof_received(self):
        """The socket was closed from the remote end
        """

    def info(self):
        info = {'connection': {'session': self._session}}
        if self._producer:
            info.update(self._producer.info())
        return info

    async def _close(self, waiter):
        try:
            with timeout(CLOSE_TIMEOUT, loop=self._loop):
                await waiter
        except asyncio.TimeoutError:
            self.logger.warning('Abort connection %s', self)
            self.abort()


class Protocol(PulsarProtocol, asyncio.Protocol):
    """An :class:`asyncio.Protocol` with :ref:`events <event-handling>`
    """
    _data_received_count = 0
    last_change = None

    def write(self, data):
        """Write ``data`` into the wire.

        Returns an empty tuple or a :class:`~asyncio.Future` if this
        protocol has paused writing.
        """
        t = self._transport
        if t:
            if self._paused:
                # # Uses private variable once again!
                # This occurs when the protocol is paused from writing
                # but another data ready callback is fired in the same
                # event-loop frame
                self.logger.debug('protocol cannot write, add data to the '
                                  'transport buffer')
                t._buffer.extend(data)
            else:
                t.write(data)
                self._make_write_waiter()
            self.last_change = self._loop.time()
            return self._write_waiter or ()
        else:
            raise ConnectionResetError('No Transport')


class DatagramProtocol(PulsarProtocol, asyncio.DatagramProtocol):
    """An ``asyncio.DatagramProtocol`` with events`
    """


class Connection(Protocol, Timeout):
    """A :class:`.FlowControl` to handle multiple TCP requests/responses.

    It is a class which acts as bridge between a
    :ref:`transport <asyncio-transport>` and a :class:`.ProtocolConsumer`.
    It routes data arriving from the transport to the
    :meth:`current_consumer`.

    .. attribute:: _consumer_factory

        A factory of :class:`.ProtocolConsumer`.

    .. attribute:: _processed

        number of separate requests processed.
    """
    def __init__(self, consumer_factory=None, timeout=None, **kw):
        super().__init__(**kw)
        self._processed = 0
        self._current_consumer = None
        self._consumer_factory = consumer_factory
        self.event('connection_lost').bind(self._connection_lost)
        self.timeout = timeout

    @property
    def requests_processed(self):
        return self._processed

    def current_consumer(self):
        """The :class:`ProtocolConsumer` currently handling incoming data.

        This instance will receive data when this connection get data
        from the :attr:`~PulsarProtocol.transport` via the
        :meth:`data_received` method.

        If no consumer is available, build a new one and return it.
        """
        if self._current_consumer is None:
            self._build_consumer(None)
        return self._current_consumer

    def data_received(self, data):
        """Delegates handling of data to the :meth:`current_consumer`.

        Once done set a timeout for idle connections when a
        :attr:`~Protocol.timeout` is a positive number (of seconds).
        """
        self._data_received_count = self._data_received_count + 1
        toprocess = data
        while toprocess:
            toprocess = self.current_consumer()._data_received(data)
        self.last_change = self._loop.time()

    def upgrade(self, consumer_factory):
        """Upgrade the :func:`_consumer_factory` callable.

        This method can be used when the protocol specification changes
        during a response (an example is a WebSocket request/response,
        or HTTP tunneling).

        This method adds a ``post_request`` callback to the
        :meth:`current_consumer` to build a new consumer with the new
        :func:`_consumer_factory`.

        :param consumer_factory: the new consumer factory (a callable
            accepting no parameters)
        :return: ``None``.
        """
        self._consumer_factory = consumer_factory
        consumer = self._current_consumer
        if consumer:
            consumer.bind_event('post_request', self._build_consumer)
        else:
            self._build_consumer(None)

    def info(self):
        info = super().info()
        c = info['connection']
        c['request_processed'] = self._processed
        c['data_processed_count'] = self._data_received_count
        c['timeout'] = self.timeout
        return info

    def _build_consumer(self, _, exc=None):
        if not exc or isinstance(exc, AbortEvent):
            consumer = self._producer.build_consumer(self._consumer_factory)
            assert self._current_consumer is None, 'Consumer is not None'
            self._current_consumer = consumer
            consumer._connection = self

    def _connection_lost(self, _, exc=None):
        """It performs these actions in the following order:

        * Fires the ``connection_lost`` :ref:`one time event <one-time-event>`
          if not fired before, with ``exc`` as event data.
        * Cancel the idle timeout if set.
        * Invokes the :meth:`ProtocolConsumer.connection_lost` method in the
          :meth:`current_consumer`.
        """
        if self._current_consumer:
            self._current_consumer.finished(exc=exc)


class Producer(EventHandler):
    """An Abstract :class:`.EventHandler` class for all producers of
    socket (client and servers)
    """
    protocol_factory = None
    """A callable producing protocols.

    The signature of the protocol factory callable must be::

        protocol_factory(session, producer, **params)
    """

    def __init__(self, *, loop=None, protocol_factory=None, name=None,
                 max_requests=None, logger=None):
        self.logger = logger or LOGGER
        self._loop = loop or asyncio.get_event_loop()
        self.protocol_factory = protocol_factory or self.protocol_factory
        self._name = name or self.__class__.__name__
        self._requests_processed = 0
        self.sessions = 0
        self._max_requests = max_requests

    @property
    def requests_processed(self):
        """Total number of requests processed.
        """
        return self._requests_processed

    def create_protocol(self, **kw):
        """Create a new protocol via the :meth:`protocol_factory`

        This method increase the count of :attr:`sessions` and build
        the protocol passing ``self`` as the producer.
        """
        self.sessions += 1
        kw['session'] = self.sessions
        kw['producer'] = self
        kw['loop'] = self._loop
        kw['logger'] = self.logger
        return self.protocol_factory(**kw)

    def build_consumer(self, consumer_factory):
        """Build a consumer for a protocol.

        This method can be used by protocols which handle several requests,
        for example the :class:`Connection` class.

        :param consumer_factory: consumer factory to use.
        """
        consumer = consumer_factory(loop=self._loop)
        consumer.logger = self.logger
        consumer.copy_many_times_events(self)
        return consumer


class TcpServer(Producer):
    """A :class:`.Producer` of server :class:`Connection` for TCP servers.

    .. attribute:: _server

        A :class:`.Server` managed by this Tcp wrapper.

        Available once the :meth:`start_serving` method has returned.
    """
    ONE_TIME_EVENTS = ('start', 'stop')
    _server = None
    _started = None

    def __init__(self, protocol_factory, *, loop=None, address=None,
                 name=None, sockets=None, max_requests=None,
                 keep_alive=None, logger=None):
        super().__init__(loop=loop, protocol_factory=protocol_factory,
                         name=name, max_requests=max_requests, logger=logger)
        self._params = {'address': address, 'sockets': sockets}
        self._keep_alive = max(keep_alive or 0, 0)
        self._concurrent_connections = set()

    def __repr__(self):
        address = self.address
        if address:
            return '%s %s' % (self.__class__.__name__, address)
        else:
            return self.__class__.__name__
    __str_ = __repr__

    @property
    def address(self):
        """Socket address of this server.

        It is obtained from the first socket ``getsockname`` method.
        """
        if self._server is not None:
            return self._server.sockets[0].getsockname()

    @property
    def addresses(self):
        return [sock.getsockname() for sock in self.sockets or ()]

    @property
    def sockets(self):
        if self._server is not None:
            return self._server.sockets

    async def start_serving(self, backlog=100, sslcontext=None):
        """Start serving.

        :param backlog: Number of maximum connections
        :param sslcontext: optional SSLContext object.
        :return: a :class:`.Future` called back when the server is
            serving the socket.
        """
        assert not self._server
        if hasattr(self, '_params'):
            address = self._params['address']
            sockets = self._params['sockets']
            del self._params
            create_server = self._loop.create_server
            if sockets:
                server = None
                for sock in sockets:
                    srv = await create_server(self.create_protocol,
                                              sock=sock,
                                              backlog=backlog,
                                              ssl=sslcontext)
                    if server:
                        server.sockets.extend(srv.sockets)
                    else:
                        server = srv
            else:
                if isinstance(address, tuple):
                    server = await create_server(self.create_protocol,
                                                 host=address[0],
                                                 port=address[1],
                                                 backlog=backlog,
                                                 ssl=sslcontext)
                else:
                    raise NotImplementedError
            self._server = server
            self._started = self._loop.time()
            for sock in server.sockets:
                address = sock.getsockname()
                self.logger.info('%s serving on %s', self._name,
                                 format_address(address))
            self._loop.call_soon(self.event('start').fire)

    async def close(self):
        """Stop serving the :attr:`.Server.sockets`.
        """
        if self._server:
            self._server.close()
            self._server = None
            coro = self._close_connections()
            if coro:
                await coro
            self.event('stop').fire()

    def info(self):
        sockets = []
        up = int(self._loop.time() - self._started) if self._started else 0
        server = {'uptime_in_seconds': up,
                  'sockets': sockets,
                  'max_requests': self._max_requests,
                  'keep_alive': self._keep_alive}
        clients = {'processed_clients': self.sessions,
                   'connected_clients': len(self._concurrent_connections),
                   'requests_processed': self._requests_processed}
        if self._server:
            for sock in self._server.sockets:
                sockets.append({
                    'address': format_address(sock.getsockname())})
        return {'server': server,
                'clients': clients}

    def create_protocol(self):
        """Override :meth:`Producer.create_protocol`.
        """
        protocol = super().create_protocol(timeout=self._keep_alive)
        protocol.event('connection_made').bind(self._connection_made)
        protocol.event('connection_lost').bind(self._connection_lost)
        protocol.copy_many_times_events(self)
        if (self._server and self._max_requests and
                self._sessions >= self._max_requests):
            self.logger.info('Reached maximum number of connections %s. '
                             'Stop serving.' % self._max_requests)
            self.close()
        return protocol

    #    INTERNALS
    def _connection_made(self, connection, exc=None):
        if not exc:
            self._concurrent_connections.add(connection)

    def _connection_lost(self, connection, exc=None):
        self._concurrent_connections.discard(connection)

    def _close_connections(self, connection=None, timeout=5):
        """Close ``connection`` if specified, otherwise close all connections.

        Return a list of :class:`.Future` called back once the connection/s
        are closed.
        """
        all = []
        if connection:
            waiter = connection.event('connection_lost').waiter()
            if waiter:
                all.append(waiter)
                connection.close()
        else:
            connections = list(self._concurrent_connections)
            self._concurrent_connections = set()
            for connection in connections:
                waiter = connection.event('connection_lost').waiter()
                if waiter:
                    all.append(waiter)
                    connection.close()
        if all:
            self.logger.info('%s closing %d connections', self, len(all))
            return asyncio.wait(all, timeout=timeout, loop=self._loop)


class DatagramServer(Producer):
    """An :class:`.Producer` for serving UDP sockets.

    .. attribute:: _transports

        A list of :class:`.DatagramTransport`.

        Available once the :meth:`create_endpoint` method has returned.
    """
    _transports = None
    _started = None

    ONE_TIME_EVENTS = ('start', 'stop')

    def __init__(self, protocol_factory, loop=None, address=None,
                 name=None, sockets=None, max_requests=None,
                 logger=None):
        super().__init__(loop, protocol_factory, name=name,
                         max_requests=max_requests, logger=logger)
        self._params = {'address': address, 'sockets': sockets}

    @property
    def addresses(self):
        return [sock.getsockname() for sock in self.sockets or ()]

    @property
    def sockets(self):
        sockets = []
        if self._transports is not None:
            for t in self._transports:
                sock = t.get_extra_info('socket')
                if sock:
                    sockets.append(sock)
        return sockets

    async def create_endpoint(self, **kw):
        """create the server endpoint.

        :return: a :class:`~asyncio.Future` called back when the server is
            serving the socket.
        """
        if hasattr(self, '_params'):
            address = self._params['address']
            sockets = self._params['sockets']
            del self._params
            transports = []
            loop = self._loop
            if sockets:
                for sock in sockets:
                    transport, _ = await loop.create_datagram_endpoint(
                        self.create_protocol, sock=sock)
                    transports.append(transport)
            else:
                transport, _ = await loop.create_datagram_endpoint(
                    self.create_protocol, local_addr=address)
                transports.append(transport)
            self._transports = transports
            self._started = loop.time()
            for transport in self._transports:
                address = transport.get_extra_info('sockname')
                self.logger.info('%s serving on %s', self._name,
                                 format_address(address))
            self.event('start').fire()

    async def close(self):
        """Stop serving the :attr:`.Server.sockets` and close all
        concurrent connections.
        """
        transports, self._transports = self._transports, None
        if transports:
            for transport in transports:
                transport.close()
            self.event('stop').fire()

    def info(self):
        sockets = []
        up = int(self._loop.time() - self._started) if self._started else 0
        server = {'uptime_in_seconds': up,
                  'sockets': sockets,
                  'max_requests': self._max_requests}
        clients = {'requests_processed': self._requests_processed}
        if self._transports:
            for transport in self._transports:
                sock = transport.get_extra_info('socket')
                if sock:
                    sockets.append({
                        'address': format_address(sock.getsockname())
                    })
        return {'server': server,
                'clients': clients}
