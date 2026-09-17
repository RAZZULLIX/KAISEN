"""A forked worker must not keep the parent's live LLM sockets open.

The symptom this defends (measured live): a generation is abandoned
(retry, nodata timeout, pause/cancel), the parent closes its response, but
a worker forked while that stream was live still holds an inherited copy
of the socket.  llama.cpp only aborts a decode when the connection is
really gone, so the box kept generating into a socket nobody read: it held
its slot (single-slot servers queue the next request) and the next
generation sat at zero tokens — "stuck in prefill" — while the box's tps
counter showed the abandoned request's tokens.

The test drives the real client path (`Server.request_stream` + a cancel
event) against a socket-level fake box that reports the moment the client
connection is truly gone, and forks a child while the stream is live.
"""
import multiprocessing
import select
import socket
import threading
import time

from kaisen import llm as L


class StreamingBox:
    """Socket-level llama.cpp stand-in.

    Streams SSE tokens and watches the connection for EOF (peek, so the
    tokens themselves stay in the buffer): EOF means every process holding
    the connection let go, which is what makes a real box abort the decode.
    """

    def __init__(self):
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}/completion"
        self.first_token = threading.Event()
        self.client_gone = threading.Event()
        self._conn = None
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        for closer in (self._conn, self.sock):
            try:
                if closer is not None:
                    closer.close()
            except OSError:
                pass

    def wait_first_token(self, timeout=5.0) -> bool:
        return self.first_token.wait(timeout)

    def _serve(self):
        conn, _ = self.sock.accept()
        self._conn = conn
        conn.settimeout(10.0)
        head = b""
        while b"\r\n\r\n" not in head:            # request head
            chunk = conn.recv(4096)
            if not chunk:
                return
            head += chunk
        conn.sendall(b"HTTP/1.1 200 OK\r\n"
                     b"Content-Type: text/event-stream\r\n"
                     b"Transfer-Encoding: chunked\r\n\r\n")
        i = 0
        while True:
            try:
                ready, _, _ = select.select([conn], [], [], 0)
            except (OSError, ValueError):         # fd closed by the teardown
                return
            if ready:                             # EOF, or leftover request bytes
                try:
                    if conn.recv(1, socket.MSG_PEEK) == b"":
                        self.client_gone.set()
                        return
                except OSError:
                    self.client_gone.set()
                    return
            body = ('data: {"content": "tok%d", "stop": false}\n\n' % i).encode()
            try:
                conn.sendall(b"%x\r\n" % len(body) + body + b"\r\n")
            except OSError:
                self.client_gone.set()
                return
            if i == 0:
                self.first_token.set()
            i += 1
            time.sleep(0.02)


def test_forked_worker_does_not_keep_an_abandoned_stream_alive(tmp_cfg):
    ctx = multiprocessing.get_context("fork")
    with StreamingBox() as box:
        server = L.Server({"id": "isolation-box", "type": "llama", "url": box.url,
                           "max_concurrent": 1}, tmp_cfg)
        cancel = threading.Event()
        raised = []

        def consume():
            try:
                server.request_stream("hello", on_token=lambda tok, n=1: None,
                                      cancel_event=cancel)
            except Exception as exc:              # noqa: BLE001 - the cancel path
                raised.append(exc)

        reader = threading.Thread(target=consume, daemon=True)
        reader.start()
        assert box.wait_first_token(), "the fake box never streamed a token"

        # A worker forked WHILE the stream is live inherits the socket...
        child = ctx.Process(target=time.sleep, args=(30,))
        child.start()
        try:
            # ...and then the generation is abandoned, exactly as a retry,
            # nodata timeout or pause does it in the engine.
            cancel.set()
            reader.join(5.0)
            assert raised, "the abandoned stream should raise (cancel/retry path)"
            assert box.client_gone.wait(3.0), (
                "the box still sees an open connection after the parent "
                "abandoned the stream: a forked worker kept an inherited copy "
                "of the socket, so the box keeps decoding a request nobody "
                "reads and its slot stays busy for the next generation")
        finally:
            child.terminate()
            child.join()
