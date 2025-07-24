# Standard Library Imports
import re
import struct
import time
import traceback
from collections import defaultdict

# Third-Party Library Imports
import psutil
from pymem import Pymem

# Custom Imports
from qmp_proxy import QmpProxy

PROCESS_NAME = "xemu.exe"


class MemoryReader:
    """
    A class to read memory from a process using pymem.
    It supports reading various data types and handles caching and address translation.
    """

    process_id: int
    pymem: Pymem
    qmp_proxy: QmpProxy

    def __init__(self, use_pymem: bool = True):
        self._pymem = pm = Pymem()
        self.qmp_proxy = QmpProxy()
        self.attach_pymem_to_xemu()

        self.use_pymem = use_pymem
        self._pymem_cache = {}
        self.pymem_counter = 0
        self._memory_cache = {}
        self.known_addresses = defaultdict(dict)
        self._memory_functions = {
            "<B": pm.read_uchar,
            "<H": pm.read_ushort,
            "<I": pm.read_uint,
            "<Q": pm.read_ulonglong,
            "<b": pm.read_char,
            "<h": pm.read_short,
            "<i": pm.read_int,
            "<f": pm.read_float,
            "bytes": pm.read_bytes,
            "string": pm.read_string,
        }
        self._struct_objects = {
            "<B": struct.Struct("<B"),
            "<H": struct.Struct("<H"),
            "<I": struct.Struct("<I"),
            "<Q": struct.Struct("<Q"),
            "<c": struct.Struct("<c"),
            "<h": struct.Struct("<h"),
            "<i": struct.Struct("<i"),
            "<f": struct.Struct("<f"),
        }

    def search_for_xemu_process(self):
        """Returns the process id of the first xemu instance that has qmp running"""
        for proc in psutil.process_iter():
            if proc.name() != PROCESS_NAME:
                continue

            info = proc.as_dict()
            cmdline = " ".join(info["cmdline"])
            match = re.search(r"-qmp tcp:(?P<address>.+):(?P<port>\d+),", cmdline)

            if not match:
                continue

            self.process_id = proc.pid
            return self.process_id
        return None

    def attach_pymem_to_xemu(self):
        interval = 1  # seconds
        elapsed = 0
        # initial message
        print("Waiting for Xemu to start... 0 s", end="", flush=True)

        while not self.search_for_xemu_process():
            elapsed += interval
            # '\r' returns to start of line; end="" prevents newline; flush=True forces immediate print
            print(f"\rWaiting for Xemu to start... {elapsed} s", end="", flush=True)
            time.sleep(interval)

        # once done, print a newline so next prints aren’t on the same line
        print()
        print(f"Xemu pid is {self.process_id} ({hex(self.process_id)})")

        self.pymem = Pymem()
        self.pymem.open_process_from_id(process_id=self.process_id)

    def populate_memory_cache(self):
        """
        Cache snapshots of large segments of contiguous memory for future lookups.
        This cache should be invalidated and repopulated every tick by calling invalidate_memory_cache().
        """

        def add_range_to_cache(base_address, size, description=None):
            """Helper function to add a memory range to the cache."""
            if base_address and size > 0:
                self.add_to_cache(base_address, size)
            else:
                if description:
                    print(
                        f"Warning: Skipping caching {description} due to invalid address or size."
                    )

        # Game state
        add_range_to_cache(
            self.read_u32(0x2E2D14), self.read_u32(0x32E4A), "game state"
        )

        # Spawns from tags cache
        global_scenario_address = self.read_u32(0x39BE5C)
        first_spawn_address = self.read_s32(global_scenario_address + 856)
        if first_spawn_address:
            spawn_count = self.read_s32(global_scenario_address + 852)
            add_range_to_cache(
                first_spawn_address, 52 * spawn_count, "spawns from tags cache"
            )

        # Observer camera
        add_range_to_cache(0x271550, 688 * 4, "observer camera")

        # Object type definitions
        # FIXME: Adjust size calculation for accuracy
        add_range_to_cache(
            0x1FC0D0, (0x1FCBA4 - 0x1FC0D0) * 2, "object type definitions"
        )

    def invalidate_memory_cache(self):
        self._memory_cache.clear()

    def add_to_cache(self, address, size):
        self._memory_cache[
            (address, address + size, self.get_host_address(address))
        ] = self.read_bytes(address, size, keep_value=False)

    def read_from_cache(self, address, fmt, length=128, **kwargs):
        """
        Returns an empty dict if the address is not found in the cache.
        If the address is found, returns a dict with 'value' and 'host_address'.
        """
        for (start, end, host_address), cached_bytes in self._memory_cache.items():
            if not (start <= address <= end):
                continue

            # Address is found in the cache
            offset = address - start
            host_addr_offset = self.get_host_address(start) + offset
            result = {"host_address": host_addr_offset}

            # Determine the format and extract the value
            if fmt in self._struct_objects:
                # Use precompiled struct for performance
                result["value"] = self._struct_objects[fmt].unpack_from(
                    cached_bytes, offset
                )[0]
            elif fmt == "bytes":
                result["value"] = cached_bytes[offset : offset + length]
            elif fmt == "string":
                # Extract a string up to the specified length or until a null terminator
                buff = cached_bytes[offset : offset + (length if length else 128)]
                null_terminator_index = buff.find(b"\x00")
                result["value"] = (
                    buff[:null_terminator_index].decode()
                    if null_terminator_index != -1
                    else buff.decode()
                )
            else:
                # Fallback for other formats using struct unpacking
                result["value"] = struct.unpack_from(fmt, cached_bytes, offset)[0]

            return result

        # Address not found in cache
        return {}

    def get_host_address_from_cache(self, address):
        for (start, end, host_address), cached_bytes in self._memory_cache.items():
            if start <= address <= end:
                return self.get_host_address(start) + (address - start)
        return -1

    # FIXME: avoid the forced qmp lookup in get_host_address
    def get_host_address(self, address):
        # Check if the address is already known
        if address in self.known_addresses:
            return self.known_addresses[address]["host_address"]

        # Attempt to retrieve the host address from the cache
        host_address = self.get_host_address_from_cache(address)
        if host_address >= 0:
            self.known_addresses[address] = {"host_address": host_address}
        else:
            # Fallback to translating the address if not found in cache
            host_address = self.qmp_proxy.translate(address)
            self.known_addresses[address] = {"host_address": host_address}

        return host_address

    def read_memory(
        self,
        address,
        fn,
        retry_on_value_change=False,
        is_host_address=False,
        keep_value=True,
        watch=False,
        return_host_address=False,
        assume_contiguous_ram=True,
        **kwargs,
    ):
        """
        Reads memory from either the cache or the live memory using different methods depending on the address.
        """

        def update_known_address(addr, val, host_addr):
            """Helper function to update the known_addresses dictionary."""
            self.known_addresses[addr] = {
                "host_address": host_addr,
                "value": val if keep_value else 0,
                "type": self._memory_functions[fn].__name__,
            }

        # Read directly if address is a host address
        if is_host_address:
            value = self._memory_functions[fn](address, **kwargs)
            self.pymem_counter += 1
            return value

        # Check memory cache
        cached_value = self.read_from_cache(address, fn, **kwargs)
        if cached_value:
            update_known_address(
                address, cached_value["value"], cached_value["host_address"]
            )
            return cached_value["value"]

        # Check known addresses
        if address in self.known_addresses:
            host_address = self.known_addresses[address]["host_address"]
            value = self._memory_functions[fn](host_address, **kwargs)
            self.pymem_counter += 1

            # Retry if the value changes unexpectedly
            if (
                retry_on_value_change
                and value != self.known_addresses[address]["value"]
            ):
                print(
                    f'WARNING: value for {hex(address)} changed from {hex(self.known_addresses[address]["value"])} to {hex(value)}'
                )
                host_address = self.qmp_proxy.gva2hva(address)
                value = self._memory_functions[fn](host_address, **kwargs)
                self.pymem_counter += 1

            update_known_address(address, value, host_address)
            return value

        # Handle contiguous RAM assumption
        if assume_contiguous_ram and address > 0x80000000:
            base_address = self.qmp_proxy.gva2hva(0x80000000)
            offset = address - 0x80000000
            host_address = base_address + offset
            value = self._memory_functions[fn](host_address, **kwargs)
            self.pymem_counter += 1
            update_known_address(address, value, host_address)
            return value

        # Translate guest address to host address (fallback)
        host_address = self.qmp_proxy.gva2hva(address)
        value = self._memory_functions[fn](host_address, **kwargs)
        self.pymem_counter += 1
        update_known_address(address, value, host_address)

        # Debugging information
        self.known_addresses[address]["qmp"] = True  # Marking as a QMP translation
        self.known_addresses[address]["qmp_traceback"] = traceback.extract_stack()[
            -3
        ].line

        return value

    def read_u8(self, address, *args, **kwargs):
        return self.read_memory(address, "<B", *args, **kwargs)

    def read_u16(self, address, *args, **kwargs):
        return self.read_memory(address, "<H", *args, **kwargs)

    def read_u32(self, address, *args, **kwargs):
        return self.read_memory(address, "<I", *args, **kwargs)

    def read_u64(self, address, *args, **kwargs):
        return self.read_memory(address, "<Q", *args, **kwargs)

    def read_s8(self, address, *args, **kwargs):
        return self.read_memory(address, "<b", *args, **kwargs)

    def read_s16(self, address, *args, **kwargs):
        return self.read_memory(address, "<h", *args, **kwargs)

    def read_s32(self, address, *args, **kwargs):
        return self.read_memory(address, "<i", *args, **kwargs)

    def read_float(self, address, *args, **kwargs):
        return self.read_memory(address, "<f", *args, **kwargs)

    def read_bytes(self, address, length, *args, **kwargs):
        return self.read_memory(address, "bytes", length=length, *args, **kwargs)

    def read_string(self, address, length=128, *args, **kwargs):
        return self.read_memory(address, "string", byte=length, *args, **kwargs)

    def read_wchar(self, address, length=128, *args, **kwargs):
        return (
            self.read_bytes(address, length, *args, **kwargs)
            .decode("utf-16")
            .split("\x00", 1)[0]
        )

    def write_bytes(
        self, address, value, length, is_guest_address=True, *args, **kwargs
    ):
        if is_guest_address:
            address = self.get_host_address(address)
        return self._pymem.write_bytes(address, value, length)

    def get_formatted_bytes(self, address, length, columns=32):

        data = self.read_bytes(address, length)
        data_string = [
            data.hex(" ")[i : i + 3 * columns].strip()
            for i in range(0, len(data.hex(" ")), 3 * columns)
        ]
        return data_string
