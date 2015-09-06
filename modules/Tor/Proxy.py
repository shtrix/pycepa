from core.TLSClient import TLSClient
from core.Module import Module
from core.LocalModule import LocalModule
from Crypto.Cipher import AES
from modules.Tor.DirServ import authorities
from modules.Tor.cell import cell
from modules.Tor.cell import parser as cell_parser
from hashlib import sha1
from hashlib import sha256
import hmac
import ssl
import random
import struct
import logging
import curve25519
import hkdf

log = logging.getLogger(__name__)

class TorStream(LocalModule):
    """
    A Tor stream in a circuit.
    """

    def __init__(self, circuit, stream_id=None):
        """
        Circuit local events registered:
            * <circuit_id>_<stream_id>_got_relay <circuit_id> <stream_id> <cell>
                - got a relay cell.
            * <circuit_id>_<stream_id>_init_directory_stream <circuit_id> <stream_id>
                - initialize the directory stream.
        """
        super(TorStream, self).__init__()

        self.closed  = False
        self.connected = False
        self.circuit = circuit
        self.data    = ''
        self.stream_id = stream_id or random.randint(1, 65535)

        self.circuit.register_local('%d_%d_got_relay' % (self.circuit.circuit_id, 
            self.stream_id), self.got_relay)
        self.circuit.register_local('%d_%d_init_directory_stream' % 
            (self.circuit.circuit_id, self.stream_id), self.directory_stream)

        self.counter = 0

    def connect(self, addr, port):
        """
        Iniatializes a remote TCP connection.
        """
        data = '%s:%d\0%s' % (addr, port, struct.pack('>I', 0))
        self.circuit.trigger_local('%d_send_relay_cell' % self.circuit.circuit_id,
            'RELAY_BEGIN', self.stream_id, data)

    def got_relay(self, circuit_id, stream_id, _cell):
        """
        Handles received relay cells.

        Events registered:
            * tor_directory_stream_<stream_id>_send <data> - send data through a stream.

        Events raised:
            * tor_directory_stream_<stream_id>_connected <stream_id> - stream connected.
            * tor_directory_stream_<stream_id>_closed                - stream closed.
            * tor_directory_stream_<stream_id>_recv <data>           - data received from
                                                                       stream.
        """
        command = _cell.data['command_text']
        log.debug('Got relay cell: %s' % command)

        if command == 'RELAY_CONNECTED':
            self.connected = True
            self.register('tor_directory_stream_%s_send' % self.stream_id, self.send)
            self.trigger('tor_directory_stream_%s_connected' % self.stream_id,
                self.stream_id)
        elif command == 'RELAY_END':
            self.connected = False
            self.closed = True
            self.trigger('tor_directory_stream_%s_closed' % self.stream_id)
        elif command == 'RELAY_DATA':
            self.counter += 1
            if self.counter == 50:
                self.counter = 0
                self.circuit.trigger_local('%d_send_relay_cell' %
                    self.circuit.circuit_id, 'RELAY_SENDME', stream_id=self.stream_id)

            self.trigger('tor_directory_stream_%s_recv' % self.stream_id,
                _cell.data['data'])

    def send(self, data):
        """
        Send data down a stream, breaks into PAYLOAD_LEN - 11 byte chunks.
        """
        while data:
            self.circuit.trigger_local('%d_send_relay_cell' % self.circuit.circuit_id,
                'RELAY_DATA', self.stream_id, data[:509-11])
            data = data[509-11:]

    def directory_stream(self, circuit_id, stream_id):
        """
        Sends a RELAY_BEGIN_DIR cell.
        """
        self.circuit.trigger_local('%d_send_relay_cell' % circuit_id,
            'RELAY_BEGIN_DIR', self.stream_id)

class Circuit(LocalModule):
    """
    Tor circuit.
    """

    def __init__(self, proxy, circuit_id=None):
        """
        Local events registered:
            * <circuit_id>_got_cell_CreatedFast <circuit_id> <cell> - CreatedFast cell
                                                                      received in circuit.
            * <circuit_id>_got_cell_Relay <circuit_id> <cell>       - Relay cell received
                                                                      in circuit.
        """
        super(Circuit, self).__init__()
        self._events = proxy._events
        self.node = proxy.node
        self.node_id = (self.node['identity'] + '=').decode('base64')
        self.counter = 0

        self.circuit_id = circuit_id or random.randint(1<<31, 1<<32)

        self.established = False
        self.streams     = {}

        self.register_local('%d_got_cell_Created2' % self.circuit_id, self.crypt_init_ntor)
        self.register_local('%d_got_cell_CreatedFast' % self.circuit_id, self.crypt_init_tap)
        self.register_local('%d_got_cell_Relay' % self.circuit_id, self.recv_cell)
        self.register_local('%d_do_ntor_handshake' % self.circuit_id, self.do_ntor)
        self.register_local('%d_do_tap_handshake' % self.circuit_id, self.do_tap)

        log.info('initializing circuit id %d' % self.circuit_id)

    def aes_init(self, Df, Db, Kf, Kb):
        """
        Initializes the AES encryption / decryption objects.
        """
        self.Kfctr = 0
        def fctr():
            self.Kfctr += 1
            return ('%032x' % (self.Kfctr - 1)).decode('hex')

        self.Kbctr = 0
        def bctr():
            self.Kbctr += 1
            return ('%032x' % (self.Kbctr - 1)).decode('hex')

        self.send_digest = sha1(Df)
        self.recv_digest = sha1(Db)

        self.encrypt = AES.new(Kf, AES.MODE_CTR, counter=fctr).encrypt
        self.decrypt = AES.new(Kb, AES.MODE_CTR, counter=bctr).decrypt

    def do_tap(self):
        """
        Create our CreateFast cell. Initializes the key material that we will be using.

        Local events raised:
            * send_cell <cell> [data] - sends a cell.
        """
        c = cell.CreateFast(self.circuit_id)
        self.Y = c.key_material
        self.trigger_local('send_cell', c)

    def do_ntor(self):
        """
        Initializes the ntor handshake.

        Local events raised:
            * send_cell <cell> [data] - sends a cell.
        """
        self.x = curve25519.Private()
        self.X = self.x.get_public()
        self.B = curve25519.Public(self.node['ntor-onion-key'].decode('base64'))

        handshake  = self.node_id
        handshake += self.B.public
        handshake += self.X.public
        self.trigger_local('send_cell', cell.Create2(self.circuit_id), handshake)

    def crypt_init_tap(self, circuit_id, c):
        """
        Finish the TAP handshake after receiving CreatedFast (tor-spec.txt,
        section 5.1.3).
        """
        self.X = c.key_material
        k0 = self.Y + self.X

        K, i = '', 0
        while len(K) < 2*16 + 3*20:
            K += sha1(k0 + chr(i)).digest()
            i += 1

        Kh = K[:20]
        Df = K[20:40]
        Db = K[40:60]
        Kf = K[60:76]
        Kb = K[76:92]

        self.aes_init(Df, Db, Kf, Kb)
        self.circuit_initialized()

    def crypt_init_ntor(self, circuit_id, c):
        """
        Finish the ntor handshake once we receive the Created2
        """
        self.Y = curve25519.Public(c.Y)
        self.auth = c.auth

        def hash_func(shared):
            return shared

        si  = self.x.get_shared_key(self.Y, hash_func)
        si += self.x.get_shared_key(self.B, hash_func)
        si += self.node_id
        si += self.B.public
        si += self.X.public
        si += self.Y.public
        si += 'ntor-curve25519-sha256-1'

        key_seed = hmac.new('ntor-curve25519-sha256-1:key_extract', si, sha256).digest()
        verify = hmac.new('ntor-curve25519-sha256-1:verify', si, sha256).digest()

        ai  = verify
        ai += self.node_id
        ai += self.B.public
        ai += self.Y.public
        ai += self.X.public
        ai += 'ntor-curve25519-sha256-1'
        ai += 'Server'
        
        auth = hmac.new('ntor-curve25519-sha256-1:mac', ai, sha256).digest()
        if self.auth != auth:
            log.error('bad ntor handshake.') 
            self.trigger_local('die')
            return

        keys = hkdf.hkdf_expand(key_seed, 'ntor-curve25519-sha256-1:key_expand', 72, sha256)
        Df, Db, Kf, Kb = struct.unpack('>20s20s16s16s', keys)

        self.aes_init(Df, Db, Kf, Kb)
        self.circuit_initialized()

    def connect(self, stream_id):
        """
        Initialize a stream on the circuit.
        """
        stream = TorStream(self, stream_id)
        self.streams[stream.stream_id] = stream

    def init_directory_stream(self, stream_id):
        """
        Initiate a directory stream.

        Local events raised:
            * <circuit_id>_<stream_id>_init_directory_stream <circuit_id> <stream_id> -
                initiate a directory stream.
            * <circuit_id>_<stream_id>_stream_initialized <circuit_id> <stream_id> -
                stream initialized (but not connected).
        """
        self.connect(stream_id)
        self.trigger_local('%d_%d_init_directory_stream' % (self.circuit_id,
            stream_id), self.circuit_id, stream_id)
        self.trigger_local('%d_%d_stream_initialized' % (self.circuit_id,
            stream_id), self.circuit_id, stream_id)

    def recv_cell(self, circuit_id, c):
        """
        Cell received. If it's a CreatedFast this is a fresh circuit that is ready to use.
        If it's a relay cell, then it gets forwarded to the appropriate stream.
        
        Local events registered:
            * <circuit_id>_init_directory_stream <stream_id> - create a new directory stream
                                                               on this circuit with the 
                                                               given stream id.
            * <circuit_id>_send_relay_cell <cell>            - send a relay cell upstream.

        Local events raised:
            * <circuit_id>_circuit_initialized <circuit_id> 
                - indicates that the circuit is ready to use.
            * <circuit_id>_<stream_id>_got_relay <circuit_id> <stream_id> <cell>
                - cell received on this circuit for the given stream.

        """
        if isinstance(c, cell.Relay):
            c.data = self.decrypt(c.data)
            c.parse()

            if c.data['command_text'] == 'RELAY_DATA':
                self.counter += 1
                if self.counter == 100:
                    self.counter = 0
                    self.trigger_local('%d_send_relay_cell' % self.circuit_id,
                        'RELAY_SENDME')

            if c.data['stream_id'] in self.streams:
                self.trigger_local('%d_%d_got_relay' % (self.circuit_id,
                    c.data['stream_id']), self.circuit_id, c.data['stream_id'], c)

    def send_relay_cell(self, command, stream_id=None, data=None):
        """
        Generate, encrypt, and send a relay cell with the given command. If the
        command is a string, it will be converted to the command id.

        Circuit local events raised:
            * send_cell <cell> [stream_id] [data] - sends a cell.
        """
        c = cell.Relay(self.circuit_id)

        if isinstance(command, str):
            command = cell.relay_name_to_command(command)

        c.init_relay({
            'command': command,
            'stream_id': stream_id or 0,
            'digest': '\x00' * 4,
            'data': data
        })

        self.send_digest.update(c.get_str())
        c.data['digest'] = self.send_digest.digest()[:4]
        c.data = self.encrypt(c.get_str())
        self.trigger_local('send_cell', c)

    def circuit_initialized(self):
        """
        Called after negotiating the key exchange to initialize the circuit.

        Local events registered:
            * <circuit_id>_init_directory_stream <stream_id> - create a new directory stream
                                                               on this circuit with the 
                                                               given stream id.
            * <circuit_id>_send_relay_cell <cell>            - send a relay cell upstream.

        Local events raised:
            * <circuit_id>_circuit_initialized <circuit_id> 
                - indicates that the circuit is ready to use.
        """
        self.established = True
        log.info('established circuit id %d.' % self.circuit_id)
        self.register_local('%d_init_directory_stream' % self.circuit_id, 
            self.init_directory_stream)
        self.register_local('%d_send_relay_cell' % self.circuit_id, self.send_relay_cell)
        self.trigger_local('%d_circuit_initialized' % self.circuit_id, self.circuit_id)

class TorConnection(TLSClient):
    """
    Connection to a Tor router.
    """
    def __init__(self, node):
        """
        Local events registered:
            * handshook                                       - TLS handshake completed.
            * received <data>                                 - data received from socket.
            * send_cell <cell> [data]                         - send a cell.
            * 0_got_cell_Versions <circuit_id> <cell>         - got the version cell.
            * 0_got_cell_Certs <circuit_id> <cell>            - got the certs cell.
            * 0_got_cell_AuthChallenge <circuit_id> <cell>    - got the authchallenge cell.
            * 0_got_cell_Netinfo <circuit_id> <cell>          - got the netinfo cell.

        Events registered:
            * tor_<or_name>_init_directory_stream <stream_id> - initiate a directory stream
                                                                with the given stream id.
        """
        self.node = node
        self.circuits = []
        self.cell = None
        self.in_buffer = ''
        self.name = node['name']

        super(TorConnection, self).__init__(node['ip'], node['or_port'])

        self.context = ssl.SSLContext(ssl.PROTOCOL_TLSv1_2)
        self.context.set_ciphers(
            'ECDHE-ECDSA-AES256-SHA:ECDHE-RSA-AES256-SHA:DHE-RSA-AES256-SHA:'
            'DHE-DSS-AES256-SHA:ECDH-RSA-AES256-SHA:ECDH-ECDSA-AES256-SHA:'
            'ECDHE-ECDSA-RC4-SHA:ECDHE-ECDSA-AES128-SHA:'
            'ECDHE-RSA-RC4-SHA:ECDHE-RSA-AES128-SHA:DHE-RSA-AES128-SHA:'
            'DHE-DSS-AES128-SHA:ECDH-RSA-RC4-SHA:ECDH-RSA-AES128-SHA:'
            'ECDH-ECDSA-RC4-SHA:ECDH-ECDSA-AES128-SHA:RSA-RC4-MD5:RSA-RC4-SHA:'
            'RSA-AES128-SHA:ECDHE-ECDSA-DES192-SHA:ECDHE-RSA-DES192-SHA:'
            'EDH-RSA-DES192-SHA:EDH-DSS-DES192-SHA:ECDH-RSA-DES192-SHA:'
            'ECDH-ECDSA-DES192-SHA:RSA-FIPS-3DES-EDE-SHA:RSA-DES192-SHA'
        )

        log.info('initiating connection to guard node.')
        self.register_local('handshook', self.initial_handshake)
        self.register_local('received', self.received)
        self.register_local('send_cell', self.send_cell)
        self.register_local('0_got_cell_Versions', self.got_versions)
        self.register_local('0_got_cell_Certs', self.got_certs)
        self.register_local('0_got_cell_AuthChallenge', self.got_authchallenge)
        self.register_local('0_got_cell_Netinfo', self.got_netinfo)
        self.register('tor_%s_init_directory_stream' % self.name,
            self.init_directory_stream)

        self.init()
        self.waiting = {}

    def initial_handshake(self):
        """
        Start the initial handshake by sending a versions sell.
        """
        log.info('connected to guard node established.')
        self.send_cell(cell.Versions())

    def received(self, data):
        """
        Received some data, parse it out and handle accordingly.

        Local events raised:
            * <circuit_id>_got_cell_<cell_type> <circuit_id> <cell> - got a cell of the
                                                                      given type.
        """
        self.in_buffer += data

        while self.in_buffer:
            try:
                log.debug('received data: %s' % self.in_buffer.encode('hex'))
                self.in_buffer, self.cell, ready, cont = cell_parser.parse_cell(
                    self.in_buffer, self.cell)
            except cell.CellError as e:
                log.error('invalid cell received: %s' % e)
                self.die()
                break

            if ready:
                self.trigger_local('%d_got_cell_%s' %
                    (self.cell.circuit_id, cell.cell_type_to_name(self.cell.cell_type)),
                    self.cell.circuit_id, self.cell)
                self.cell = None

            if not cont:
                log.debug("don't have enough bytes to read the entire cell, waiting.")
                break

    def send_cell(self, c, data=None):
        """
        Send a cell down the wire.
        
        Local events raised:
            * send <data> - sends data on the socket.
        """
        if not data:
            data = ''
        log.info('sending cell type %s' % cell.cell_type_to_name(c.cell_type))
        log.debug('sending cell: ' + c.pack(data).encode('hex'))
        self.trigger_local('send', c.pack(data))

    def init_circuit(self):
        """
        Initialize a circuit.
        """
        circuit = Circuit(self)
        self.register_local('%d_circuit_initialized' % circuit.circuit_id,
            self.circuit_initialized)
        self.trigger_local('%d_do_ntor_handshake' % circuit.circuit_id)
        return circuit.circuit_id

    def circuit_initialized(self, circuit_id):
        self.circuits.append(circuit_id)

    def got_versions(self, circuit_id, versions):
        """
        Got versions cell, sets version to the highest that we share.
        """
        cell.proto_version = max(versions.versions)

    def got_certs(self, circuit_id, certs):
        """
        Got certs cell, currently does nothing.
        """
        self.certs = certs
        log.debug(self.certs.certs)

    def got_authchallenge(self, circuit_id, authchallenge):
        """
        Got authchallenge. Currently does nothing.
        """
        self.authchallenge = authchallenge

    def got_netinfo(self, circuit_id, netinfo):
        """
        Got the netinfo cell, send our own.

        Events raied:
            * tor_<or_name>_proxy_initialized <or_name> - tor connection is ready to use.

        Local events raised:
            * send_cell <cell> <data> - sends a cell down the wire.
        """
        self.netinfo = netinfo

        self.trigger_local('send_cell', cell.Netinfo(), {
            'me': netinfo.our_address,
            'other': netinfo.router_addresses[0]
        })

        self.trigger('tor_%s_proxy_initialized' % self.name, self.name)

    def init_directory_stream(self, stream):
        """
        Finds or creates a circuit for a directory stream.

        Local events registered:
            * <circuit_id>_circuit_initialized <circuit_id>  - circuit has been initialized.
            * <circuit_id>_init_directory_stream <stream_id> - initialize a directory
                                                               stream on a circuit with the
                                                               given stream id.
        """
        if not self.circuits:
            circuit_id = self.init_circuit()
            self.waiting[circuit_id] = stream
            self.register_once_local('%d_circuit_initialized' % circuit_id,
                self.do_directory_stream)
        else:
            self.trigger_local('%d_init_directory_stream' % self.circuits[0], stream)

    def do_directory_stream(self, circuit_id):
        """
        Initialize a directory stream on a circuit.

        Local events raised:
            * <circuit_id>_init_directory_stream <stream_id> - initialize a directory
                                                               stream on a circuit with the
                                                               given stream id.
        """
        stream_id = self.waiting[circuit_id]
        del self.waiting[circuit_id]
        self.trigger_local('%d_init_directory_stream' % circuit_id, stream_id)

class Proxy(Module):
    """
    Tor proxy handler. Handles creation of tor connections.
    """
    def module_load(self):
        """
        Events registered:
            * tor_init_directory_stream <stream_id> - create a directory stream
        """
        self.register('tor_init_directory_stream', self.init_directory_stream)
        self.connections_pending = []
        self.connections = []
        self.streams = []

    def init_directory_stream(self, stream_id):
        """
        Initialize a directory stream. Creates a new tor connection, if necessary.

        Events registered:
            * tor_<or_name>_proxy_initialized <or_name>      - indicates that the OR
                                                               connection is ready for
                                                               use.
            * tor_<or_name>_init_directory_stream <stream_id - initialize a directory
                                                               stream, will create
                                                               circuits as necessary.
        """
        self.streams.append(stream_id)

        if not self.connections and not self.connections_pending:
            self.register('tor_%s_proxy_initialized' % authorities[0]['name'],
                self.proxy_initialized)
            self.connections_pending.append(TorConnection(authorities[0]))
        elif self.connections:
            self.trigger('tor_%s_init_directory_stream' % self.connections[0].name,
                stream_id)

    def proxy_initialized(self, name):
        """
        Proxy is initialized, so we begin creating a stream.

        Events raised:
            * tor_<or_name>_init_directory_stream <stream_id> - initialize a directory
                                                                stream, will create
                                                                circuits as necessary.
        """
        conn = None
        for c in self.connections_pending:
            if name != c.name:
                continue

            conn = c

        if not conn:
            log.error('something went terribly wrong here...')
            return

        self.connections_pending.remove(conn)
        self.connections.append(conn)

        for stream in self.streams:
            self.trigger('tor_%s_init_directory_stream' % conn.name, stream)
        self.streams = []
