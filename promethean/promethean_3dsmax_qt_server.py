import os
import sys
import traceback


try:
    from PySide2.QtCore import QObject
    from PySide2.QtWidgets import QApplication
    from PySide2.QtNetwork import QTcpServer, QHostAddress, QTcpSocket
except ImportError:
    from PySide.QtCore import QObject
    from PySide.QtGui import QApplication
    from PySide.QtNetwork import QTcpServer, QHostAddress, QTcpSocket

# For autocompletion
if False:
    from PyQt5 import QtCore
    from PyQt5 import QtWidgets
    from PyQt5 import QtNetwork

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
import promethean_3dsmax
import promethean_3dsmax_version


class MaxServer(QObject, object):
    PORT = 1314

    def __init__(self, parent=None):
        parent = parent or QApplication.activeWindow()
        super(MaxServer, self).__init__(parent)

        self._socket = None
        self._server = None
        self._port = self.__class__.PORT

        self.connect()

    def connect(self, try_disconnect=True):
        self._server = QTcpServer(self)
        self._server.newConnection.connect(self._on_established_connection)

        if self._listen():
            promethean_3dsmax.dump(
                'PrometheanAI {}: 3ds max server listening on port: {}'.format(
                    promethean_3dsmax_version.version, self._port))
        else:
            if try_disconnect:
                self.disconnect()
                self.connect(try_disconnect=False)
                return
            promethean_3dsmax.dump('PrometheanAI: 3ds max initialization failed. If the problem persists, restart 3ds Max please.')

    def disconnect(self):
        if self._socket:
            # self._socket.disconnected.disconnect()
            self._socket.readyRead.disconnect()
            self._socket.close()
            self._socket.deleteLater()
            self._socket = None

        self._server.close()
        promethean_3dsmax.dump('PrometheanAI: 3ds Max server connection disconnected')

    def _listen(self):
        if not self._server.isListening():
            return self._server.listen(QHostAddress.LocalHost, self._port)

        return False

    def _read(self):

        bytes_remaining = -1

        while self._socket.bytesAvailable():
            if bytes_remaining <= 0:
                byte_array = self._socket.read(131072)
                data = str(byte_array) if byte_array else ''
                # Moving all the commands that we send in one batch to the undo queue
                # This won't work for all the commands that add assets as references
                with promethean_3dsmax.undo(True, 'Promethean Command'):
                    self._process_data(data)

    def _process_data(self, data_str):
        data_commands = data_str.split('\n')
        res = None
        while data_commands:
            data = data_commands.pop(0)
            if not data:
                continue
            promethean_3dsmax.dump('PrometheanAI: New network 3ds Max command: {}'.format(data))

            try:
                res = promethean_3dsmax.command_switch(data)
            except Exception as exc:
                promethean_3dsmax.dump('PrometheanAI: 3dsMax command switch failed with this message:')
                traceback.print_exc()
                # make sure to send the reply back to Promethean as otherwise it may freeze
                res = 'None'
        # we return just the result of last command, cause that's what Promethean may wait from the plugin
        if res:
            promethean_3dsmax.dump('PrometheanAI: Sending message back to Promethean: {}'.format(res.encode('utf-8')))
            self._write(res)
        promethean_3dsmax.update_viewports()

    def _write(self, reply_str):
        if self._socket and self._socket.state() == QTcpSocket.ConnectedState:
            data = reply_str.encode()
            self._socket.write(data)

        return reply_str

    def _write_error(self, error_msg):
        print(error_msg)

    def _on_established_connection(self):
        self._socket = self._server.nextPendingConnection()
        if self._socket.state() == QTcpSocket.ConnectedState:
            # self._socket.disconnected.connect(self._on_disconnected)
            self._socket.readyRead.connect(self._read)
            print('[LOG] Connection established')

    def _on_disconnected(self):
        self.disconnect()


if __name__ == '__main__':

    try:
        server.disconnect()
    except Exception:
        pass

    server = MaxServer()
