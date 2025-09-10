# Standard Library Imports
import copy
import datetime
import gc
import json
import os
import socket
import threading
from pprint import pprint

# Third-Party Library Imports
import psutil
from pymem.exception import MemoryReadError
from SimpleWebSocketServer import SimpleWebSocketServer, WebSocket

# Custom Imports
import ui  # Consider renaming the alias if 'ui#2' is necessary

# from read_memory import MemoryReader
from read_mem_linux import MemoryReader
from read_game_linux import GameReader
import websocket_server  # Importing the websocket server module

# from database import DBConnector
# from memory_mappings_and_offsets import *

memory_reader = MemoryReader()
game_reader = GameReader(memory_reader=memory_reader)


clients = []
server = None


class SimpleWSServer(WebSocket):
    def handleConnected(self):
        print("Websocket client connected", self.client, self.address)
        clients.append(self)

    def handleClose(self):
        print("Websocket client disconnected", self.client, self.address)
        clients.remove(self)


def run_websocket_server():
    global server
    server = SimpleWebSocketServer(
        "0.0.0.0", 9000, SimpleWSServer, selectInterval=(1000.0 / 60) / 1000
    )
    print("Websocket server started", server.serversocket)
    server.serveforever()


server_thread = threading.Thread(
    target=run_websocket_server, daemon=True, name="websocket_server_thread"
)
server_thread.start()


database_worker_thread = threading.Thread(
    target=game_reader.handle_game_info_loop, daemon=True, name="database_thread"
)
database_worker_thread.start()


def main_loop():
    """
    Basic flow:
    - Read game_time as quickly as possible, looking for a change.
    - If game_time changes:
        - Read game info from memory.
        - Offload game info to background handler threads (websockets, database, local file, etc).
    """
    counter = 0
    last_game_time = 0
    last_real_time = datetime.datetime.now()
    last_post_steps = 0
    benchmark_tick_count = 0
    benchmark_loop_count = 0
    last_game_info = {}
    events = []
    duration_total = 0

    while True:
        try:
            game_time = (
                memory_reader.read_u32(game_reader.game_time_address) - 1
            )  # game_time is incremented after the tick, so we want time-1
            benchmark_loop_count += 1
            counter += 1

            if game_time != last_game_time:
                benchmark_tick_count += 1
                real_time = datetime.datetime.now()
                counter = 0
                memory_reader.pymem_counter = 0

                # Handle memory and processing
                memory_reader.populate_memory_cache()
                game_reader.process_write_queue()
                game_info = game_reader.get_game_info()
                memory_reader.invalidate_memory_cache()

                # Ensure game time consistency
                if game_info["game_time_info"]["game_time"] != game_time:
                    print(
                        f"  WARNING: mismatched game time (expected {game_time}, got {game_info['game_time_info']['game_time']})"
                    )

                # Performance warning for slow updates
                current = datetime.datetime.now()
                duration = (current - real_time).microseconds
                if duration > 33000:
                    print(
                        f"  WARNING: this update took longer than one tick: {duration / 1000:.2f}ms"
                    )

                # Missed ticks warning
                if game_time > last_game_time + 1:
                    print(
                        f"  WARNING: missed {game_time - last_game_time - 1} ticks between {last_game_time} and {game_time}"
                    )

                # Extract events if the game is ongoing
                if last_game_info:
                    if (
                        last_game_info["game_engine_running"]
                        and not game_info["game_engine_running"]
                    ):
                        events = []
                    else:
                        events += game_reader.extract_events(last_game_info, game_info)
                game_info["events"] = events
                last_game_info = game_info

                # Collect performance metrics
                game_info["performance"] = {
                    "game_info_time": duration / 1000,
                    "loop_time": (real_time - last_real_time).microseconds / 1000,
                    "post_steps_ms": last_post_steps,
                    "memory_mbytes": psutil.Process(os.getpid()).memory_info().vms
                    / 1024**2,
                }

                last_real_time = real_time
                post_steps_start = datetime.datetime.now()

                # Use deep copy to avoid modifying game_info in other threads
                game_reader.game_info_queue.put(copy.deepcopy(game_info))
                game_reader.game_info_queue_for_ui.put(game_info)

                # Send data to clients
                if clients:
                    data = json.dumps(game_info, default=str)
                    for client in clients:
                        client.sendMessage(data)

                last_post_steps = (
                    datetime.datetime.now() - post_steps_start
                ).microseconds / 1000

            last_game_time = game_time

        except (ValueError, MemoryReadError) as e:
            # Handle memory reading errors and reset the state
            pprint(e)
            game_reader.clear_caches()
            memory_reader.attach_pymem_to_xemu()

        except KeyError as e:
            # Handle key errors explicitly
            pprint(e)
            raise

        except socket.timeout as e:
            # Handle socket timeout errors
            print("DROPPED FRAME DUE TO SOCKET TIMEOUT")
            memory_reader.qmp_proxy._qmp.close()
            memory_reader.qmp_proxy.connect()

        except Exception as e:
            # Catch-all for any unexpected exceptions to prevent crashing
            pprint(e)
            game_reader.clear_caches()
            memory_reader.attach_pymem_to_xemu()


if __name__ == "__main__":
    gc.disable()

    # Start the WebSocket server in a separate thread
    websocket_thread = threading.Thread(
        target=websocket_server.start_websocket_server,
        args=(game_reader.game_info_queue,),
    )
    websocket_thread.daemon = True
    websocket_thread.start()

    ui_thread = threading.Thread(
        target=ui.start_ui,
        args=(
            game_reader.game_info_queue_for_ui,
            game_reader.write_queue_from_ui,
        ),
        daemon=True,
        name="ui_thread",
    )
    ui_thread.start()

    main_loop()
