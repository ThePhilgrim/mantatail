import pytest
import socket
import threading
from mantatail import Server

motd_dict_test = {
    "motd": [
        "- Hello {user_nick}, this is a test MOTD!",
        "-",
        "- Foo",
        "- Bar",
        "- Baz",
        "-",
        "- End test MOTD",
    ]
}


def test_run_server():
    server = Server(6667, motd_dict_test)

    def run_server(server):
        try:
            server.run_server_forever()
        except OSError:
            return

    threading.Thread(target=run_server, args=[server]).start()

    client_socket = socket.socket()
    client_socket.connect(("localhost", 6667))
    client_socket.sendall(b"NICK foo\r\n")

    # Receiving everything the server is going to send helps prevent errors.
    # Otherwise it might not be fully started yet when the client quits.
    received = b""
    while not received.endswith(b"- End test MOTD\r\n"):
        received += client_socket.recv(4096)

    client_socket.sendall(b"QUIT\r\n")
    client_socket.close()

    server.listener_socket.shutdown(socket.SHUT_RDWR)
    server.listener_socket.close()
