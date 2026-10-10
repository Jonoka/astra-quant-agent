"""Runner isolation regressions, not application or Linux acceptance.

The transport tripwire refuses unknown destinations before any OS connect even
when testing the old broken guard. Only test-owned port-zero listeners are real.
"""
from contextlib import contextmanager
import http.server
import ipaddress
from pathlib import Path
import socket
import ssl
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch
import urllib.request

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / '.github/scripts'))
from run_actual_integration import isolated


@contextmanager
def guarded_transport(*, simulated=True, resolver=None):
    original = socket.socket
    owned, calls = set(), []
    class Tripwire(original):
        def bind(self, address):
            result = super().bind(address)
            actual = self.getsockname()
            owned.add((self.family, actual[0], actual[1]))
            return result
        def connect(self, address):
            calls.append(address)
            try: host = str(ipaddress.ip_address(address[0]))
            except ValueError: host = address[0]
            if (self.family, host, address[1]) not in owned:
                raise AssertionError('raw transport tripwire reached an unowned endpoint')
            self.peer = address
            return None if simulated else super().connect(address)
        def connect_ex(self, address):
            self.connect(address)
            return 0
        def getpeername(self):
            return self.peer if simulated else super().getpeername()
    with tempfile.TemporaryDirectory(prefix='endpoint-guard-') as temporary:
        with patch.object(socket, 'socket', Tripwire):
            with patch.object(socket, 'getaddrinfo', resolver or socket.getaddrinfo):
                with isolated(temporary):
                    yield calls


class EndpointIsolationTests(unittest.TestCase):
    def assert_refused(self, callable):
        with self.assertRaisesRegex(AssertionError, 'integration '): callable()

    def test_exact_owned_ipv4_and_connect_ex_transport_are_allowed(self):
        with guarded_transport() as calls, socket.socket() as server, socket.socket() as client:
            server.bind(('127.0.0.1', 0)); address = server.getsockname()
            client.connect(address)
            self.assertEqual(client.connect_ex(address), 0)
            self.assertEqual(calls, [address, address])
            with patch.object(client, 'getpeername', return_value=('127.0.0.2', address[1])):
                self.assert_refused(lambda: client.connect(address))
            self.assertEqual(client.fileno(), -1)

    def test_same_port_other_ipv6_address_is_refused_before_transport(self):
        with guarded_transport() as calls, socket.socket() as server:
            server.bind(('127.0.0.1', 0)); port = server.getsockname()[1]
            with socket.socket(socket.AF_INET6) as client:
                self.assert_refused(lambda: client.connect(('::1', port)))
                self.assert_refused(lambda: client.connect_ex(('::1', port)))
            self.assertEqual(calls, [])

    def test_remote_same_port_is_refused_before_transport(self):
        with guarded_transport() as calls, socket.socket() as server, socket.socket() as client:
            server.bind(('127.0.0.1', 0)); port = server.getsockname()[1]
            self.assert_refused(lambda: client.connect(('203.0.113.1', port)))
            self.assert_refused(lambda: socket.getaddrinfo('203.0.113.1', port))
            self.assertEqual(calls, [])

    def test_localhost_ambiguity_never_reaches_dns_or_transport(self):
        dns = unittest.mock.Mock(side_effect=AssertionError('raw resolver reached'))
        with guarded_transport(resolver=dns) as calls, socket.socket() as server, socket.socket() as client:
            server.bind(('127.0.0.1', 0)); port = server.getsockname()[1]
            self.assert_refused(lambda: client.connect(('localhost', port)))
            self.assert_refused(lambda: socket.getaddrinfo('localhost', port))
            self.assertEqual(calls, []); dns.assert_not_called()

    def test_mapped_scoped_and_family_aliases_are_refused_before_transport(self):
        with guarded_transport() as calls, socket.socket() as server, socket.socket() as v4:
            server.bind(('127.0.0.1', 0)); port = server.getsockname()[1]
            with socket.socket(socket.AF_INET6) as v6:
                for address in (('::ffff:127.0.0.1', port), ('::1%1', port), ('::1', port, 0, 1), ('127.0.0.1', port)):
                    self.assert_refused(lambda address=address: v6.connect(address))
            self.assert_refused(lambda: v4.connect(('::1', port)))
            self.assert_refused(lambda: ssl.SSLSocket.connect(SimpleNamespace(family=socket.AF_INET6), ('::1', port)))
            self.assert_refused(lambda: ssl.SSLSocket.connect_ex(SimpleNamespace(family=socket.AF_INET6), ('::1', port)))
            self.assertEqual(calls, [])
        with guarded_transport() as calls, socket.socket(socket.AF_INET6) as server, socket.socket(socket.AF_INET6) as client:
            server.bind(('::1', 0)); port = server.getsockname()[1]
            client.connect(('0:0:0:0:0:0:0:1', port))
            self.assertEqual(calls, [('::1', port, 0, 0)])

    def test_numeric_resolver_results_require_exact_registered_endpoint(self):
        responses = []
        def resolver(*args, **kwargs): return responses
        with guarded_transport(resolver=resolver) as calls, socket.socket() as server:
            server.bind(('127.0.0.1', 0)); address = server.getsockname(); port = address[1]
            for other in ((socket.AF_INET6, socket.SOCK_STREAM, 6, '', ('::1', port, 0, 0)),
                          (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.2', port))):
                responses[:] = [other]
                self.assert_refused(lambda: socket.getaddrinfo(*address))
            responses[:] = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', address)]
            self.assertEqual(socket.getaddrinfo(*address), responses)
            self.assertEqual(calls, [])

    @contextmanager
    def listener(self, redirect=False):
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(owner):
                owner.send_response(302 if redirect else 200)
                if redirect: owner.send_header('Location', 'http://[::1]:' + str(owner.server.server_port) + '/other')
                owner.send_header('Content-Length', '0'); owner.end_headers()
            def log_message(self, *args): pass
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01})
        thread.start()
        try: yield 'http://127.0.0.1:' + str(server.server_port) + '/owned'
        finally: server.shutdown(); server.server_close(); thread.join(5)

    def test_http_redirect_cannot_reach_same_port_unowned_address(self):
        with guarded_transport(simulated=False) as calls, self.listener(redirect=True) as url:
            with patch.object(urllib.request, '_opener', None):
                self.assert_refused(lambda: urllib.request.urlopen(url, timeout=3))
            self.assertEqual(len(calls), 1); self.assertEqual(calls[0][0], '127.0.0.1')

    def test_http_proxy_discovery_is_disabled_for_owned_listener(self):
        with guarded_transport(simulated=False) as calls, self.listener() as url:
            with patch.object(urllib.request, '_opener', None):
                with patch.object(urllib.request, 'getproxies', return_value={'http': 'http://203.0.113.1:1'}) as discovery:
                    with urllib.request.urlopen(url, timeout=3) as response: self.assertEqual(response.status, 200)
                    discovery.assert_not_called()
            self.assertEqual(len(calls), 1); self.assertEqual(calls[0][0], '127.0.0.1')


if __name__ == '__main__': unittest.main(verbosity=2)
