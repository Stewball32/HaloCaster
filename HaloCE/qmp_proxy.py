# Standard Library Imports
import datetime
import time
from qmp import QEMUMonitorProtocol

QMP_ADDRESS = "localhost"
QMP_PORT = 4444


class QmpProxy:
    """
    Interacts with QEMU via QEMU Monitor Protocol (QMP)
    Primarily used here for translating guest addresses to host addresses
    QMP is extremely slow compared to direct memory reads
    """

    last_request_time = datetime.datetime.now()
    rate_limit_enabled = False
    request_rate_seconds = 0.005  # minimum seconds between requests
    # request_rate_seconds = 0.000
    cmd_counter = 0
    cmd_counter_reset = datetime.datetime.now()
    _qmp: QEMUMonitorProtocol

    def __init__(self):
        self.connect()

    def connect(self):
        i = 0
        print(f"Connect to QEMU at {QMP_ADDRESS}:{QMP_PORT}", end=".")
        while True:
            try:
                self._qmp = QEMUMonitorProtocol((QMP_ADDRESS, QMP_PORT))
                self._qmp.connect()
                self._qmp.settimeout(0.5)
            except Exception as e:
                if i < 5:
                    print(".", end="")
                    i += 1
                    time.sleep(1)
                    continue
                raise Exception(f"Failed to connect to QEMU after {i} attempts: {e}")
            break

    def run_cmd(self, cmd):
        # print(f'running command: {cmd}')
        now = datetime.datetime.now()
        delta = (now - self.last_request_time).total_seconds()
        if self.rate_limit_enabled and delta < self.request_rate_seconds:
            # print(f'waiting {self.request_rate_seconds - delta}s')
            time.sleep(self.request_rate_seconds - delta)
        self.last_request_time = now
        if type(cmd) is str:
            cmd = {"execute": cmd, "arguments": {}}
        self.cmd_counter += 1
        if (datetime.datetime.now() - self.cmd_counter_reset).total_seconds() > 1.0:
            print(
                f"qmp commands in last {(datetime.datetime.now() - self.cmd_counter_reset).total_seconds()} seconds: {self.cmd_counter}"
            )
            self.cmd_counter = 0
            self.cmd_counter_reset = datetime.datetime.now()
            import traceback

            print(cmd)
            traceback.print_stack()
        resp = self._qmp.cmd_obj(cmd)  # type: ignore
        if resp is None:
            raise Exception("Disconnected!")
        # print(cmd, resp)
        # traceback.print_stack()
        return resp

    def pause(self):
        return self.run_cmd("stop")

    def cont(self):
        return self.run_cmd("cont")

    def restart(self):
        return self.run_cmd("system_reset")

    def screenshot(self):
        cmd = {"execute": "screendump", "arguments": {"filename": "screenshot.ppm"}}
        return self.run_cmd(cmd)

    def is_paused(self):
        resp = self.run_cmd("query-status")
        return resp["return"]["status"] == "paused"

    def read(self, addr, size):
        """
        See https://github.com/qemu/qemu/blob/5e05c40ced78ed9a3c25a82ec1f144bb7baffe3f/monitor/misc.c#L615
        :param addr:
        :param size:
        :return:
        """
        cmd = {
            "execute": "human-monitor-command",
            "arguments": {"command-line": "x /%dxb %d" % (size, addr)},
        }
        response = self.run_cmd(cmd)
        r = response["return"].replace("\r", "")
        # print(f"response: {r}")
        lines = response["return"].replace("\r", "").split("\n")
        data_string = " ".join(l.partition(": ")[2] for l in lines).strip()
        data = bytes(int(b, 16) for b in data_string.split(" "))
        return data

        # 'Cannot access memory'

    def gpa2hva(self, addr):
        """
        See https://github.com/qemu/qemu/blob/5e05c40ced78ed9a3c25a82ec1f144bb7baffe3f/monitor/misc.c#L664
            https://github.com/qemu/qemu/blob/5e05c40ced78ed9a3c25a82ec1f144bb7baffe3f/monitor/misc.c#L635
        :param addr:
        :return:
        """
        cmd = {
            "execute": "human-monitor-command",
            "arguments": {"command-line": "gpa2hva {}".format(addr)},
        }
        response = self.run_cmd(cmd)
        lines = response["return"].replace("\r", "").split("\n")
        data_string = " ".join(l.partition(" is ")[2] for l in lines).strip()
        data = int(data_string, 16)
        return data

    def gva2gpa(self, addr):
        """
        See https://github.com/qemu/qemu/blob/5e05c40ced78ed9a3c25a82ec1f144bb7baffe3f/monitor/misc.c#L684
        :param addr:
        :return:
        """
        cmd = {
            "execute": "human-monitor-command",
            "arguments": {"command-line": "gva2gpa {}".format(addr)},
        }
        # print('Getting guest physical address of guest virtual address {}'.format(hex(addr)))
        response = self.run_cmd(cmd)
        # print(cmd, response)
        lines = response["return"].replace("\r", "").split("\n")
        data_string = " ".join(l.partition("gpa: ")[2] for l in lines).strip()
        try:
            data = int(data_string, 16)
        except ValueError:
            print(f"Error converting gpa {hex(addr)} to gva (got {response})")
            raise
        return data

    def gva2hva(self, addr):
        return self.gpa2hva(self.gva2gpa(addr))

    def translate(self, addr):
        return self.gva2hva(addr)
