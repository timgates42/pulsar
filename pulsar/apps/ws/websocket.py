import base64
import hashlib
from functools import partial

from pulsar import HttpException, ProtocolError, ProtocolConsumer
from pulsar.utils.httpurl import to_bytes, native_str
from pulsar.utils.websocket import FrameParser, Frame
from pulsar.apps import wsgi

from . import extensions

WEBSOCKET_GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'

TRANSPORTS = {}

def register_transport(klass):
    TRANSPORTS[klass.name] = klass
    return klass
        

@register_transport
class WebSocket(wsgi.Router):
    """A :ref:`Router <wsgi-router>` for a websocket handshake.

    Once the handshake is succesful, the protocol consumer
    is upgraded to :class:`WebSocketProtocol` and messages are handled by
    the :attr:`handle` attribute, an instance of :class:`WS`.

    See http://tools.ietf.org/html/rfc6455 for the websocket server protocol and
    http://www.w3.org/TR/websockets/ for details on the JavaScript interface.

    .. attribute:: parser_factory

        A factory of websocket frame parsers
    """
    parser_factory = FrameParser
    _name = 'websocket'
    
    def __init__(self, route, handle, parser_factory=None, **kwargs):
        super(WebSocket, self).__init__(route, **kwargs)
        self.handle = handle
        if parser_factory:
            self.parser_factory = parser_factory
    
    @property
    def name(self):
        return self._name
    
    def get(self, request):
        headers_parser = self.handle_handshake(request.environ)
        if not headers_parser:
            raise HttpException(status=404)
        headers, parser = headers_parser
        request.response.status_code = 101
        request.response.content = b''
        request.response.headers.update(headers)
        upgrade = request.environ['pulsar.connection'].upgrade
        upgrade(partial(WebSocketProtocol, request, self.handle, parser))
        return request.response
        
    def handle_handshake(self, environ):
        connections = environ.get("HTTP_CONNECTION", '').lower()\
                                    .replace(' ','').split(',')
        if environ.get("HTTP_UPGRADE", '').lower() != "websocket" or \
           'upgrade' not in connections:
            raise HttpException(status=400)
        if environ['REQUEST_METHOD'].upper() != 'GET':
            raise HttpException(msg='Method is not GET', status=400)
        key = environ.get('HTTP_SEC_WEBSOCKET_KEY')
        if key:
            try:
                ws_key = base64.b64decode(key.encode('latin-1'))
            except Exception:
                ws_key = ''
            if len(ws_key) != 16:
                raise HttpException(msg="WebSocket key's length is invalid",
                                    status=400)
        else:
            raise HttpException(msg='Not a valid HyBi WebSocket request. '
                                    'Missing Sec-Websocket-Key header.',
                                status=400)
        # Collect supported subprotocols
        subprotocols = environ.get('HTTP_SEC_WEBSOCKET_PROTOCOL')
        ws_protocols = []
        if subprotocols:
            for s in subprotocols.split(','):
                ws_protocols.append(s.strip())
        # Collect supported extensions
        ws_extensions = []
        extensions = environ.get('HTTP_SEC_WEBSOCKET_EXTENSIONS')
        if extensions:
            for ext in extensions.split(','):
                ws_extensions.append(ext.strip())
        # Build the frame parser
        version = environ.get('HTTP_SEC_WEBSOCKET_VERSION')
        try:
            parser = self.parser_factory(version=version, protocols=ws_protocols,
                                         extensions=ws_extensions)
        except ProtocolError as e:
            raise HttpException(str(e), status=400)
        headers = [('Sec-WebSocket-Accept', self.challenge_response(key))]
        if parser.protocols:
            headers.append(('Sec-WebSocket-Protocol',
                            ', '.join(parser.protocols)))
        if parser.extensions:
            headers.append(('Sec-WebSocket-Extensions',
                            ','.join(parser.extensions)))
        return headers, parser
        
    def challenge_response(self, key):
        sha1 = hashlib.sha1(to_bytes(key+WEBSOCKET_GUID))
        return native_str(base64.b64encode(sha1.digest()))


class WebSocketProtocol(ProtocolConsumer):
    '''WebSocket protocol for servers and clients.

    .. attribute:: handshake

    The handshake response/request

    .. attribute:: handler

    A websocket handler :class:`WS`.

    .. attribute:: parser

    A websocket parser.

    '''
    request = None
    _started = False
    
    def __init__(self, handshake, handler, parser, connection=None):
        super(WebSocketProtocol, self).__init__(connection)
        self.bind_event('finish', self._shut_down)
        self.handshake = handshake
        self.handler = handler
        self.parser = parser
        
    def connection_made(self, connection):
        connection.set_timeout(0)

    def data_received(self, data):
        frame = self.parser.decode(data)
        async = self.event_loop.maybe_async
        while frame:
            if frame.is_close:
                # done with this, call finished method.
                self.finished()
                break
            elif not self._started:
                self._started = True
                async(self.handler.on_open(self))
            if frame.is_message:
                async(self.handler.on_message(self, frame.body))
            elif frame.is_bytes:
                async(self.handler.on_bytes(self, frame.body))
            elif frame.is_ping:
                async(self.handler.on_ping(self, frame.body))
            elif frame.is_pong:
                async(self.handler.on_pong(self, frame.body))
            frame = self.parser.decode()
            
    def write(self, frame):
        '''Write a new ``frame`` into the wire.

        A ``frame`` can be:

        * ``bytes`` - converted to a byte Frame
        * ``string`` - converted to a string Frame
        * a :class:`pulsar.utils.websocket.Frame`
         '''
        if not isinstance(frame, Frame):
            frame = self.parser.encode(frame)
        self.transport.write(frame.msg)
        if frame.is_close:
            self.finish()

    def _shut_down(self, res):
        self.handler.on_close(self)
        if not self.closed:
            self.connection.close()