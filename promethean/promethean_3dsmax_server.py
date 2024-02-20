import os
import sys
import time
import socket
import threading
from functools import partial

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
import promethean_3dsmax

# =====================================================================
# +++ GLOBALS
# =====================================================================
server_socket = None
command_stack = []
host = "127.0.0.1"
port = 1314

# =====================================================================
# +++ 3DS MAX TCP SERVER SETUP
# =====================================================================
def start_server():
    global server_socket

    # If server already is running, we close it
    if server_socket:
        try:
            close_server()
        except Exception:
            pass

    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_socket.bind((host, port))
    server_socket.listen(5)

    threading.Thread(target=server_thread).start()
    threading.Thread(target=execute_command_stack).start()


def server_thread():
    global command_stack

    def connection_thread(connection_):
        while True:
            try:
                data = connection_.recv(131072)  # 4096
                if data:
                    command_stack.append((data, connection_))  # bytes to unicode to string
                else:
                    break
            except:
                pass

    while server_socket:
        connection, addr = server_socket.accept()  # will wait to get the connection so we are not constantly looping
        promethean_3dsmax.dump("PrometheanAI: Got connection from " + str(addr))
        threading.Thread(target=partial(connection_thread, connection)).start()


def close_server():
    if server_socket:
        server_socket.close()


def execute_command_stack():
    time.sleep(0.1)  # waiting for socket to set up
    while server_socket:
        if command_stack:
            data, connection = command_stack.pop(0)
            promethean_3dsmax.dump('PrometheanAI: New network command stack item: %s' % data)
            try:  # make sure a crash doesn't stop the stack from being read
                promethean_3dsmax.command_switch(connection, str(data.decode()))  # bytes to string
            except Exception as e:
                promethean_3dsmax.dump('PrometheanAI: 3dsmax command switch failed with this message: ' + e)

            # connection.close()
        else:
            time.sleep(0.1)
