# Standard Library Imports
import datetime
import gc
import gzip
import json
import lzma
import os
import queue
from collections import defaultdict
from pprint import pprint

# Third-Party Library Imports
import brotli
import zstandard as zstd

# Custom Imports
from read_memory import MemoryReader

REPLAY_FILE_PATH = "C:/xemu/replays/"


class GameReader:
    """
    Reads game data from a file.
    """

    def __init__(self, mem_reader: MemoryReader):
        self.mem_reader = mem_reader
        self.mem_reader.read_u32 = mem_reader.read_u32
        self.mem_reader.read_u16 = mem_reader.read_u16
        self.mem_reader.read_u8 = mem_reader.read_u8

        # FIXME: qmp lookups outside functions
        self.player_datum_array = self.mem_reader.read_u32(0x2FAD28)
        self.player_datum_array_max_count = self.mem_reader.read_u16(
            self.player_datum_array + 0x20
        )
        self.player_datum_array_element_size = self.mem_reader.read_u16(
            self.player_datum_array + 0x22
        )
        self.player_datum_array_first_element_address = self.mem_reader.read_u32(
            self.player_datum_array + 0x34
        )
        self.players_globals_address = self.mem_reader.read_u32(0x2FAD20)
        self.teams_address = self.mem_reader.read_u32(0x2FAD24)
        self.game_globals_address = self.mem_reader.read_u32(0x27629C)
        self.global_game_globals_address = self.mem_reader.read_u32(0x39BE4C)
        self.game_server_address = self.mem_reader.read_u32(0x2E3628)
        self.game_client_address = self.mem_reader.read_u32(0x2E362C)
        self.game_connection_address = 0x2E3684
        self.is_team_game_address = self.mem_reader.read_u8(0x2F90C4)
        self.game_time_globals_address = self.mem_reader.read_u32(0x2F8CA0)
        self.global_tag_instances_address = self.mem_reader.read_u32(0x39CE24)
        self.hud_messages_pointer = self.mem_reader.read_u32(0x276B40)
        self.something_saying_main_menu = self.mem_reader.read_u32(0x2E4000 + 4)

        self.game_meta = {}
        self.spawns_cache = []
        self.items_cache = []

        self.last_game_connection = ""
        self.last_game_in_progress = (0, 0, 0)

        self.team_score_addresses_by_gametype = {
            1: 0x2762B4,  # ctf
            2: 0x276710,  # slayer
            3: 0x27653C,  # oddball
            4: 0x2762D8,  # king
            5: 0x2766C8,  # race
        }

        self.player_score_addresses_by_gametype = {
            # 1: 0x2762B4,  # ctf player scores are stored in static player object
            2: self.team_score_addresses_by_gametype[2] + 64,  # slayer
            3: self.team_score_addresses_by_gametype[3] + 64,  # oddball
            4: self.team_score_addresses_by_gametype[4] + 64,  # king
            5: self.team_score_addresses_by_gametype[5] + 64,  # race
        }

        self.game_info_queue = queue.Queue()
        self.game_info_queue_for_ui = queue.Queue()
        self.write_queue_from_ui = queue.Queue()

        self.game_time_address = self.game_time_globals_address + 12

    def get_spawns(self, cache_results=True):
        # Return cached spawns if available
        if cache_results and self.spawns_cache:
            return self.spawns_cache

        global_scenario_address = self.mem_reader.read_u32(0x39BE5C)
        spawn_count = self.mem_reader.read_u32(global_scenario_address + 852)
        first_spawn_address = self.mem_reader.read_u32(global_scenario_address + 856)

        # Return empty list early if no spawns
        if spawn_count <= 0:
            return []

        # Generate spawns using list comprehension for efficiency
        spawns = [
            {
                "address": f"{hex(spawn_address)} -> {hex(self.mem_reader.get_host_address(spawn_address))}",
                "spawn_id": spawn_index,
                "x": self.mem_reader.read_float(spawn_address),
                "y": self.mem_reader.read_float(spawn_address + 4),
                "z": self.mem_reader.read_float(spawn_address + 8),
                "facing": self.mem_reader.read_float(spawn_address + 12),
                "team_index": self.mem_reader.read_u8(spawn_address + 16),
                "bsp_index": self.mem_reader.read_u8(spawn_address + 17),
                "unk0": hex(self.mem_reader.read_u16(spawn_address + 18)),
                "gametypes": [
                    self.mem_reader.read_u8(spawn_address + 20),
                    self.mem_reader.read_u8(spawn_address + 21),
                    self.mem_reader.read_u8(spawn_address + 22),
                    self.mem_reader.read_u8(spawn_address + 23),
                ],
            }
            for spawn_index in range(spawn_count)
            if (
                spawn_address := first_spawn_address + 52 * spawn_index
            )  # Calculate spawn_address inline
        ]

        # Cache the results if needed
        if cache_results:
            self.spawns_cache[:] = spawns

        return spawns

    def get_items(self, cache_results=True):
        # Return cached items if available
        if cache_results and self.items_cache:
            return self.items_cache

        global_scenario_address = self.mem_reader.read_u32(0x39BE5C)
        item_count = self.mem_reader.read_s32(global_scenario_address + 900)
        first_item_address = self.mem_reader.read_u32(global_scenario_address + 904)

        # Return empty list early if no items
        if item_count <= 0:
            return []

        # Generate items list
        items = []
        for item_index in range(item_count):
            item_address = first_item_address + 144 * item_index
            unknown_item_attribute = self.mem_reader.read_s16(item_address + 0xE)

            # Filter items based on unknown_item_attribute if needed
            if unknown_item_attribute is None:
                continue

            tag_index = self.mem_reader.read_s32(item_address + 0x5C)
            if tag_index == -1:
                continue

            tag_index_short = tag_index & 0xFFFF
            tag_name_address = self.global_tag_instances_address + 32 * tag_index_short
            tag_name = self.mem_reader.read_string(
                self.mem_reader.read_s32(tag_name_address + 0x10)
            )
            item_spawn_interval = self.mem_reader.read_s16(
                self.mem_reader.read_s32(tag_name_address + 0x14) + 0xC
            )

            # Create item dictionary and append to the list
            item = {
                "address": f"{hex(item_address)} -> {hex(self.mem_reader.get_host_address(item_address))}",
                "tag_id": tag_index_short,
                "tag_name": tag_name,
                "item_spawn_interval": item_spawn_interval,
                "item_game_type": self.mem_reader.read_u8(item_address + 0x4),
                "item_x": self.mem_reader.read_float(item_address + 0x40),
                "item_y": self.mem_reader.read_float(item_address + 0x44),
                "item_z": self.mem_reader.read_float(item_address + 0x48),
            }
            items.append(item)

        # Cache the results if needed
        if cache_results:
            self.items_cache[:] = items

        return items

    def clear_caches(self):
        self.spawns_cache.clear()
        self.items_cache.clear()

    def get_game_time_info(self):
        """
        Players first spawn in on tick 0.
        The game logic for the nth tick happens while game_time is set to n, and game_time is only increased at the end of
        the tick (before rendering starts).
        :return:
        """

        # TODO: use this as a test for struct unpack (read these 32 bytes all at once instead of multiple memory reads)
        #       or ctypes.LittleEndianStructure with from_buffer_copy()
        #       see https://github.com/mborgerson/pyxbe/blob/master/xbe/__init__.py
        gtga = self.game_time_globals_address  # simlified variable name
        game_time_info = dict(
            game_time_globals_address=gtga,
            game_time_initialized=self.mem_reader.read_u8(gtga),
            game_time_active=self.mem_reader.read_u8(gtga + 1),
            game_time_paused=self.mem_reader.read_u8(gtga + 2),
            game_time_monitor_state=self.mem_reader.read_s16(gtga + 4),
            game_time_monitor_counter=self.mem_reader.read_s16(gtga + 6),
            game_time_monitor_latency=self.mem_reader.read_s16(gtga + 8),
            game_time=self.mem_reader.read_u32(gtga + 12)
            - 1,  # gets incremented after game engine is done, so we really want game_time-1
            game_time_elapsed=self.mem_reader.read_u32(
                gtga + 16
            ),  # looks like elapsed time in last drawn frame (dropped frame count)
            game_time_speed=self.mem_reader.read_float(
                gtga + 24
            ),  # 1.0 is normal speed
            game_time_leftover_dt=self.mem_reader.read_float(gtga + 28),
            update_client_maximum_actions=self.mem_reader.read_u32(0x2E87E8)
            - self.mem_reader.read_u32(0x2E87E4)
            + 1,  # typically gets set to 1 then decremented back to 0
            game_time_globals_address_hex=f'{gtga:#x} -> {self.mem_reader.known_addresses[gtga]["host_address"]:#x}',
            real_time_elapsed=str(
                datetime.timedelta(seconds=self.mem_reader.read_u32(gtga + 12) / 30)
            ).split(".")[
                0
            ],  # FIXME duplicated read
        )

        return game_time_info

    def get_key_data(self):

        return dict(
            kernel_header=self.mem_reader.get_formatted_bytes(0x80010000, 200),
            # data=self.mem_reader.get_formatted_bytes(0x80060220, 0x80060380 - 0x80060220)
        )

    def object_string_from_type(self, object_type):

        object_type_definitions_array = 0x1FCB78
        type_def_addr = self.mem_reader.read_u32(
            object_type_definitions_array + 4 * object_type
        )
        type_string = self.mem_reader.read_string(
            self.mem_reader.read_u32(type_def_addr)
        )
        return type_string

    def get_objects(self):
        """
        Every 30 seconds, the object header table gets rearranged.
        Retrieves objects and their details.
        """

        objects = []
        object_header_datum_array = self.mem_reader.read_u32(0x2FC6AC)
        object_header_datum_array_total_count = self.mem_reader.read_u16(
            object_header_datum_array + 0x2E
        )
        object_header_datum_array_first_element_address = self.mem_reader.read_u32(
            object_header_datum_array + 0x34
        )

        # Early exit if no objects
        if object_header_datum_array_total_count <= 0:
            return []

        # Read datum sizes once, store them for use in the loop
        object_datum_size = self.mem_reader.read_u16(0x1FC0E0)
        unit_datum_size = self.mem_reader.read_u16(0x1FC188)
        item_datum_size = self.mem_reader.read_u16(0x1FC380)

        for i in range(object_header_datum_array_total_count):
            base_address = object_header_datum_array_first_element_address + 12 * i
            object_address = self.mem_reader.read_u32(base_address + 8)
            if object_address == 0x0:
                continue

            # Gather basic object information
            tag_index = self.mem_reader.read_s16(object_address)
            tag_name = self.mem_reader.read_string(
                self.mem_reader.read_u32(
                    32 * tag_index + self.global_tag_instances_address + 0x10
                )
            )
            object_type = self.mem_reader.read_u8(object_address + 0x64)
            object_type_string = self.object_string_from_type(object_type)

            # Object details
            obj_details = {
                "object_id": i,
                "address": f'{hex(object_address)} -> {hex(self.mem_reader.known_addresses[object_address]["host_address"])}',
                "header_data": self.mem_reader.get_formatted_bytes(base_address, 12),
                "flags": hex(self.mem_reader.read_u32(object_address + 0x4)),
                "x": self.mem_reader.read_float(object_address + 0xC),
                "y": self.mem_reader.read_float(object_address + 0x10),
                "z": self.mem_reader.read_float(object_address + 0x14),
                "vel_x": self.mem_reader.read_float(object_address + 0x18),
                "vel_y": self.mem_reader.read_float(object_address + 0x1C),
                "vel_z": self.mem_reader.read_float(object_address + 0x20),
                "ang_vel_x": self.mem_reader.read_float(object_address + 0x3C),
                "ang_vel_y": self.mem_reader.read_float(object_address + 0x40),
                "ang_vel_z": self.mem_reader.read_float(object_address + 0x44),
                "time_existing": self.mem_reader.read_s16(object_address + 0x6C),
                "unk_damage_1": self.mem_reader.read_s16(object_address + 0x68),
                "owner_unit_ref": hex(self.mem_reader.read_u32(object_address + 0x70)),
                "owner_object_ref": hex(
                    self.mem_reader.read_u32(object_address + 0x74)
                ),
                "parent_ref": hex(self.mem_reader.read_u32(object_address + 0xCC)),
                "ultimate_parent": hex(
                    self.mem_reader.read_u32(object_address + 0x1E4)
                ),
                "state_flags": self.mem_reader.read_u8(object_address + 0x1A4),
                "drop_time": self.mem_reader.read_u32(object_address + 0x1B4),
                "object_type": object_type,
                "object_type_string": object_type_string,
                "tag_name": tag_name,
            }

            # Handle projectile-specific data
            if object_type_string == "projectile":
                projectile_address = object_address + item_datum_size
                obj_details["type_specific_data"] = {
                    "flags": self.mem_reader.read_u32(projectile_address),
                    "address": f'{hex(projectile_address)} -> {hex(self.mem_reader.known_addresses[projectile_address]["host_address"])}',
                    "action": self.mem_reader.read_s16(projectile_address + 0x4),
                    "hit_material_type": self.mem_reader.read_s16(
                        projectile_address + 0x6
                    ),
                    "ignore_object_index": self.mem_reader.read_s32(
                        projectile_address + 0x8
                    ),
                    "target_object_index": self.mem_reader.read_s32(
                        projectile_address + 0x1C
                    ),
                    "detonation_timer": self.mem_reader.read_float(
                        projectile_address + 0x14
                    ),
                    "detonation_timer_delta": self.mem_reader.read_float(
                        projectile_address + 0x18
                    ),
                    "arming_time": self.mem_reader.read_float(
                        projectile_address + 0x1C
                    ),
                    "arming_time_delta": self.mem_reader.read_float(
                        projectile_address + 0x20
                    ),
                    "distance_traveled": self.mem_reader.read_float(
                        projectile_address + 0x24
                    ),
                    "deceleration_timer": self.mem_reader.read_float(
                        projectile_address + 0x28
                    ),
                    "deceleration_timer_delta": self.mem_reader.read_float(
                        projectile_address + 0x2C
                    ),
                    "deceleration": self.mem_reader.read_float(
                        projectile_address + 0x30
                    ),
                    "maximum_damage_distance": self.mem_reader.read_float(
                        projectile_address + 0x34
                    ),
                    "rotation_axis_x": self.mem_reader.read_float(
                        projectile_address + 0x3C
                    ),
                    "rotation_axis_y": self.mem_reader.read_float(
                        projectile_address + 0x40
                    ),
                    "rotation_axis_z": self.mem_reader.read_float(
                        projectile_address + 0x44
                    ),
                    "rotation_sine": self.mem_reader.read_float(
                        projectile_address + 0x48
                    ),
                    "rotation_cosine": self.mem_reader.read_float(
                        projectile_address + 0x4C
                    ),
                }

            # Append the object to the list
            objects.append(obj_details)

        return objects

    def get_flag_data(self):
        game_engine_globals_address = self.mem_reader.read_u32(0x2F9110)
        if (
            game_engine_globals_address
            and self.mem_reader.read_u32(game_engine_globals_address + 0x4) == 1
        ):
            flag_0 = self.mem_reader.read_u32(0x2762A4)
            flag_1 = self.mem_reader.read_u32(0x2762A4 + 4)
            return dict(
                flag_base_0=dict(
                    x=self.mem_reader.read_float(flag_0),
                    y=self.mem_reader.read_float(flag_0 + 4),
                    z=self.mem_reader.read_float(flag_0 + 8),
                ),
                flag_base_1=dict(
                    x=self.mem_reader.read_float(flag_1),
                    y=self.mem_reader.read_float(flag_1 + 4),
                    z=self.mem_reader.read_float(flag_1 + 8),
                ),
            )
        return {}

    def get_fog(self):

        fog_params_address = 0x2FC8A8

        fog_params = dict(
            fog_params_address=f"{hex(0x2FC8A8)} -> {hex(self.mem_reader.get_host_address(0x2FC8A8))}",
            fog_color_r=self.mem_reader.read_float(fog_params_address + 0x4),
            fog_color_g=self.mem_reader.read_float(fog_params_address + 0x8),
            fog_color_b=self.mem_reader.read_float(fog_params_address + 0xC),
            fog_max_density=self.mem_reader.read_float(fog_params_address + 0x10),
            fog_atmo_min_dist=self.mem_reader.read_float(
                fog_params_address + 0x14
            ),  # defaults to 1024?
            fog_atmo_max_dist=self.mem_reader.read_float(
                fog_params_address + 0x18
            ),  # defaults to 2048?
        )

        return fog_params

    def get_memory_info(self):

        memory_info = dict(
            game_state_base_address=f"{hex(self.mem_reader.read_u32(0x2E2D14))} -> {hex(self.mem_reader.get_host_address(self.mem_reader.read_u32(0x2E2D14)))}",
            tag_cache_base_address=f"{hex(self.mem_reader.read_u32(0x2E2D18))} -> {hex(self.mem_reader.get_host_address(self.mem_reader.read_u32(0x2E2D18)))}",
            texture_cache_base_address=f"{hex(self.mem_reader.read_u32(0x2E2D1C))} -> {hex(self.mem_reader.get_host_address(self.mem_reader.read_u32(0x2E2D1C)))}",
            sound_cache_base_address=f"{hex(self.mem_reader.read_u32(0x2E2D20))} -> {hex(self.mem_reader.get_host_address(self.mem_reader.read_u32(0x2E2D20)))}",
            game_state_size=f"{hex(self.mem_reader.read_u32(0x32E4A))}",
            tag_cache_size=f"{hex(self.mem_reader.read_u32(0x32E5D))}",
            texture_cache_size=f"{hex(self.mem_reader.read_u32(0x32E75))}",
            sound_cache_size=f"{hex(self.mem_reader.read_u32(0x32E8A))}",
        )

        return memory_info

    def get_player_ui_globals(self, local_player):

        if local_player == -1:
            return {}

        player_ui_globals_address = 0x2E40D0
        return dict(
            address=hex(
                self.mem_reader.get_host_address(
                    player_ui_globals_address + local_player * 56
                )
            ),
            # TODO: profile name is at +0 widechar
            color=self.mem_reader.read_u8(
                player_ui_globals_address + local_player * 56 + 24
            ),
            button_config=self.mem_reader.read_u8(
                player_ui_globals_address + local_player * 56 + 40
            ),
            joystick_config=self.mem_reader.read_u8(
                player_ui_globals_address + local_player * 56 + 41
            ),
            sensitivity=self.mem_reader.read_u8(
                player_ui_globals_address + local_player * 56 + 42
            ),
            joystick_inverted=self.mem_reader.read_u8(
                player_ui_globals_address + local_player * 56 + 43
            ),
            rumble_enabled=self.mem_reader.read_u8(
                player_ui_globals_address + local_player * 56 + 44
            ),
            flight_inverted=self.mem_reader.read_u8(
                player_ui_globals_address + local_player * 56 + 45
            ),
            autocenter_enabled=self.mem_reader.read_u8(
                player_ui_globals_address + local_player * 56 + 46
            ),
            active_player_profile_index=f"{hex(self.mem_reader.read_u32(player_ui_globals_address + local_player * 56 + 48))}",  # used for saving profile data
            joined_multiplayer_game=self.mem_reader.read_u8(
                player_ui_globals_address + local_player * 56 + 52
            ),
        )

    def get_input_data(self, local_player_index, player_id):
        """
        Retrieves input data for the specified player, including control states and raw gamepad input.
        """

        player_control_address = self.mem_reader.read_u32(0x276794)
        update_client_player_base = self.mem_reader.read_u32(0x2E8870)
        update_client_player_address = self.mem_reader.read_u32(
            update_client_player_base + 0x34
        )
        player_offset = 0x28 * player_id
        local_player_offset = 0x1C * local_player_index
        button_field = self.mem_reader.read_u8(
            update_client_player_address + player_offset + 0x4
        )
        action_field = self.mem_reader.read_u8(
            update_client_player_address + player_offset + 0x5
        )

        # Define dictionaries for different input states
        player_control_state = (
            {
                "player_desired_yaw": self.mem_reader.read_float(
                    (local_player_index << 6) + player_control_address + 0x1C
                ),
                "player_desired_pitch": self.mem_reader.read_float(
                    (local_player_index << 6) + player_control_address + 0x20
                ),
                "player_zoom_level": self.mem_reader.read_s16(
                    (local_player_index << 6) + player_control_address + 16 + 0x24
                ),
                "player_aim_assist_target": hex(
                    self.mem_reader.read_u32(
                        (local_player_index << 6) + player_control_address + 16 + 0x28
                    )
                ),
                "player_aim_assist_near": self.mem_reader.read_float(
                    (local_player_index << 6) + player_control_address + 16 + 0x2C
                ),
                "player_aim_assist_far": self.mem_reader.read_float(
                    (local_player_index << 6) + player_control_address + 16 + 0x30
                ),
            }
            if local_player_index != -1
            else {}
        )

        input_abstraction_input_state = (
            {
                "address": f"{hex(self.mem_reader.get_host_address(0x2E4600))}",
                "a": self.mem_reader.read_u8(0x2E4600 + local_player_offset + 0x0),
                "black": self.mem_reader.read_u8(0x2E4600 + local_player_offset + 0x1),
                "x": self.mem_reader.read_u8(0x2E4600 + local_player_offset + 0x2),
                "y": self.mem_reader.read_u8(0x2E4600 + local_player_offset + 0x3),
                "b": self.mem_reader.read_u8(0x2E4600 + local_player_offset + 0x4),
                "white": self.mem_reader.read_u8(0x2E4600 + local_player_offset + 0x5),
                "left_trigger": self.mem_reader.read_u8(
                    0x2E4600 + local_player_offset + 0x6
                ),
                "right_trigger": self.mem_reader.read_u8(
                    0x2E4600 + local_player_offset + 0x7
                ),
                "start": self.mem_reader.read_u8(0x2E4600 + local_player_offset + 0x8),
                "back": self.mem_reader.read_u8(0x2E4600 + local_player_offset + 0x9),
                "left_stick_button": self.mem_reader.read_u8(
                    0x2E4600 + local_player_offset + 0xA
                ),
                "right_stick_button": self.mem_reader.read_u8(
                    0x2E4600 + local_player_offset + 0xB
                ),
                "left_stick_vertical": self.mem_reader.read_float(
                    0x2E4600 + local_player_offset + 0xC
                ),
                "left_stick_horizontal": self.mem_reader.read_float(
                    0x2E4600 + local_player_offset + 0x10
                ),
                "right_stick_horizontal": self.mem_reader.read_float(
                    0x2E4600 + local_player_offset + 0x14
                ),
                "right_stick_vertical": self.mem_reader.read_float(
                    0x2E4600 + local_player_offset + 0x18
                ),
            }
            if local_player_index != -1
            else {}
        )

        input_gamepad_state = (
            {
                "address": f"{hex(self.mem_reader.get_host_address(0x276AFC + player_offset))}",
                "address2": f"{hex(self.mem_reader.get_host_address(0x276A5C + player_offset))}",
                "a": self.mem_reader.read_u8(0x276A5C + player_offset + 0x0),
                "b": self.mem_reader.read_u8(0x276A5C + player_offset + 0x1),
                "x": self.mem_reader.read_u8(0x276A5C + player_offset + 0x2),
                "y": self.mem_reader.read_u8(0x276A5C + player_offset + 0x3),
                "black": self.mem_reader.read_u8(0x276A5C + player_offset + 0x4),
                "white": self.mem_reader.read_u8(0x276A5C + player_offset + 0x5),
                "left_trigger": self.mem_reader.read_u8(0x276A5C + player_offset + 0x6),
                "right_trigger": self.mem_reader.read_u8(
                    0x276A5C + player_offset + 0x7
                ),
                "a_duration": self.mem_reader.read_u8(0x276A5C + player_offset + 0x10),
                "b_duration": self.mem_reader.read_u8(0x276A5C + player_offset + 0x11),
                "x_duration": self.mem_reader.read_u8(0x276A5C + player_offset + 0x12),
                "y_duration": self.mem_reader.read_u8(0x276A5C + player_offset + 0x13),
                "black_duration": self.mem_reader.read_u8(
                    0x276A5C + player_offset + 0x14
                ),
                "white_duration": self.mem_reader.read_u8(
                    0x276A5C + player_offset + 0x15
                ),
                "left_trigger_duration": self.mem_reader.read_u8(
                    0x276A5C + player_offset + 0x16
                ),
                "right_trigger_duration": self.mem_reader.read_u8(
                    0x276A5C + player_offset + 0x17
                ),
                "dpad_up_duration": self.mem_reader.read_u8(
                    0x276A5C + player_offset + 0x18
                ),
                "dpad_down_duration": self.mem_reader.read_u8(
                    0x276A5C + player_offset + 0x19
                ),
                "dpad_left_duration": self.mem_reader.read_u8(
                    0x276A5C + player_offset + 0x1A
                ),
                "dpad_right_duration": self.mem_reader.read_u8(
                    0x276A5C + player_offset + 0x1B
                ),
                "left_stick_duration": self.mem_reader.read_u8(
                    0x276A5C + player_offset + 0x1E
                ),
                "right_stick_duration": self.mem_reader.read_u8(
                    0x276A5C + player_offset + 0x1F
                ),
                "left_stick_horizontal": self.mem_reader.read_s16(
                    0x276A5C + player_offset + 0x20
                ),
                "left_stick_vertical": self.mem_reader.read_s16(
                    0x276A5C + player_offset + 0x22
                ),
                "right_stick_horizontal": self.mem_reader.read_s16(
                    0x276A5C + player_offset + 0x24
                ),
                "right_stick_vertical": self.mem_reader.read_s16(
                    0x276A5C + player_offset + 0x26
                ),
            }
            if local_player_index != -1
            else {}
        )

        update_queue_values = {
            "address": f"{hex(self.mem_reader.get_host_address(update_client_player_address + player_offset))}",
            "unit_ref": f"{hex(self.mem_reader.read_u16(update_client_player_address + player_offset))}",
            "button_field": f"{hex(button_field)}",
            "button_crouch": button_field & 0x1,
            "button_jump": button_field & 0x2,
            "button_fire": button_field & 0x8,
            "button_flashlight": button_field & 0x10,
            "button_reload": button_field & 0x40,
            "button_melee": button_field & 0x80,
            "action_field": f"{hex(action_field)}",
            "button_throw_grenade": action_field & 0x30,
            "button_action": action_field & 0x40,
            "desired_yaw": self.mem_reader.read_float(
                update_client_player_address + player_offset + 0xC
            ),
            "desired_pitch": self.mem_reader.read_float(
                update_client_player_address + player_offset + 0x10
            ),
            "forward": self.mem_reader.read_float(
                update_client_player_address + player_offset + 0x14
            ),
            "left": self.mem_reader.read_float(
                update_client_player_address + player_offset + 0x18
            ),
            "right_trigger_held": self.mem_reader.read_float(
                update_client_player_address + player_offset + 0x1C
            ),
            "desired_weapon": self.mem_reader.read_u16(
                update_client_player_address + player_offset + 0x20
            ),
            "desired_grenades": self.mem_reader.read_u16(
                update_client_player_address + player_offset + 0x22
            ),
            "zoom_level": self.mem_reader.read_s16(
                update_client_player_address + player_offset + 0x24
            ),
        }

        # Return the final dictionary
        return {
            "local_player_index": local_player_index,
            "look_yaw_rate": self.mem_reader.read_float(
                0x2E4684 + 4 * local_player_index
            ),
            "look_pitch_rate": self.mem_reader.read_float(
                0x2E4694 + 4 * local_player_index
            ),
            "input_abstraction_globals": f"{hex(self.mem_reader.read_u32(0x2E45A0))} @ {hex(self.mem_reader.get_host_address(0x2E45A0))}",
            "player_control_pointer": f"{hex(player_control_address)} @ {hex(self.mem_reader.get_host_address(0x276794))}",
            "player_control": f"{hex(self.mem_reader.read_u32(player_control_address))} @ {hex(self.mem_reader.get_host_address(player_control_address))}",
            "player_control_state": player_control_state,
            "input_abstraction_input_state": input_abstraction_input_state,
            "input_gamepad_state": input_gamepad_state,
            "update_queue_values": update_queue_values,
            "player_ui_globals": self.get_player_ui_globals(local_player_index),
        }

    def get_first_person_weapon(self, local_player_index):
        """
        Weapon states:
            0   idle
            5   idle animation
            6   firing
            10  meleeing
            14  reloading
            19  readying (switching)
            20  grenading
        :param local_player_index:
        :return:
        """

        weapon_address = self.mem_reader.read_u32(0x276B48) + 7840 * local_player_index

        return dict(
            address=f"{weapon_address:#x} -> {self.mem_reader.get_host_address(weapon_address):#x}",
            weapon_rendered=self.mem_reader.read_u32(
                weapon_address
            ),  # TODO: confirm if this is actually weapon_rendered
            player_object=f"{self.mem_reader.read_u32(weapon_address + 4):#x}",  # player object id?
            weapon_object=f"{self.mem_reader.read_u32(weapon_address + 8):#x}",  # weapon object id?
            state=self.mem_reader.read_s16(weapon_address + 12),
            idle_animation_threshold=self.mem_reader.read_s16(weapon_address + 14),
            idle_animation_counter=self.mem_reader.read_s16(weapon_address + 16),
            animation_id=self.mem_reader.read_s16(
                weapon_address + 22
            ),  # TODO: not sure if this is animation id or something else
            animation_tick=self.mem_reader.read_s16(weapon_address + 24),
        )

    def get_observer_camera_info(self, local_player_index):

        if local_player_index == -1:
            return {}

        observer_camera_address = (
            0x271550 + 167 * 4 * local_player_index
        )  # 668 * player

        return dict(
            address=f"{observer_camera_address:#x} -> {self.mem_reader.get_host_address(observer_camera_address):#x}",
            x=self.mem_reader.read_float(observer_camera_address),
            y=self.mem_reader.read_float(observer_camera_address + 4),
            z=self.mem_reader.read_float(observer_camera_address + 8),
            x_vel=self.mem_reader.read_float(
                observer_camera_address + 20
            ),  # NOTE: these are different than player velocities (roughly player_vel * pi?)
            y_vel=self.mem_reader.read_float(observer_camera_address + 24),
            z_vel=self.mem_reader.read_float(observer_camera_address + 28),
            x_aim=self.mem_reader.read_float(observer_camera_address + 32),
            y_aim=self.mem_reader.read_float(observer_camera_address + 36),
            z_aim=self.mem_reader.read_float(observer_camera_address + 40),
            fov=self.mem_reader.read_float(
                observer_camera_address + 56
            ),  # vertical fov in radians
        )

    def get_model_nodes(self, base_address):

        model_node_offsets = [
            # 0x438,  # player location
            0x4A8,
            0x4DC,
            0x510,
            0x544,
            0x578,
            0x5AC,
            0x5E0,
            0x614,
            0x648,
            0x67C,
            0x6B0,
            0x6E4,
            0x718,
            0x74C,
            0x780,
            0x7B4,
            0x7E8,
            0x81C,
            0x850,
        ]

        model_nodes = []

        for offset in model_node_offsets:
            model_nodes.append(
                (
                    self.mem_reader.read_float(base_address + offset),
                    self.mem_reader.read_float(base_address + offset + 4),
                    self.mem_reader.read_float(base_address + offset + 8),
                )
            )

        return model_nodes

    def get_game_variant_global(self):

        game_variant_global_address = 0x2FAB60
        return dict(
            address=hex(self.mem_reader.get_host_address(game_variant_global_address)),
            values=self.mem_reader.get_formatted_bytes(
                game_variant_global_address, 0x68
            ),
        )

    def player_score_by_player_id(self, player_id, gametype):

        # ctf player scores are stored in static player object
        if gametype == 1:
            return 0

        return self.mem_reader.read_s32(
            self.player_score_addresses_by_gametype[gametype] + 4 * player_id
        )

    def get_network_game_data(self, network_game_data_address):

        machine_count = self.mem_reader.read_s16(network_game_data_address + 274)
        network_machines_address = network_game_data_address + 276
        player_count = self.mem_reader.read_s16(
            network_game_data_address + 548
        )  # from network_game_add_player
        network_players_address = (
            network_game_data_address + 550
        )  # from netgame_unjoin_player

        return dict(
            player_count=player_count,
            maximum_player_count=self.mem_reader.read_u8(
                network_game_data_address + 270
            ),
            machine_count=machine_count,
            network_machines=[
                dict(
                    name=self.mem_reader.read_wchar(network_machines_address + 68 * i),
                    machine_index=self.mem_reader.read_u8(
                        network_machines_address + 68 * i + 64
                    ),
                )
                for i in range(machine_count)
            ],
            network_players=[
                dict(
                    name=self.mem_reader.read_wchar(
                        network_players_address + 32 * i, 24
                    ),
                    color=self.mem_reader.read_s16(
                        network_players_address + 32 * i + 24
                    ),
                    unused=self.mem_reader.read_s16(
                        network_players_address + 32 * i + 26
                    ),
                    machine_index=self.mem_reader.read_u8(
                        network_players_address + 32 * i + 28
                    ),
                    controller_index=self.mem_reader.read_u8(
                        network_players_address + 32 * i + 29
                    ),
                    team=self.mem_reader.read_u8(network_players_address + 32 * i + 30),
                    player_list_index=self.mem_reader.read_u8(
                        network_players_address + 32 * i + 31
                    ),
                )
                for i in range(player_count)
            ],
        )

    def get_network_game_client(self):

        network_game_client_address = 0x2FB180

        return dict(
            machine_index=self.mem_reader.read_u16(network_game_client_address),
            advertised_games=dict(),  # 9 games
            ping_target_ip=hex(
                self.mem_reader.read_s32(network_game_client_address + 2056)
            ),
            packets_sent=self.mem_reader.read_s16(network_game_client_address + 2084),
            packets_received=self.mem_reader.read_s16(
                network_game_client_address + 2086
            ),
            average_ping=self.mem_reader.read_s16(network_game_client_address + 2088),
            ping_active=self.mem_reader.read_u8(network_game_client_address + 2090),
            seconds_to_game_start=self.mem_reader.read_s16(
                network_game_client_address + 3236
            ),
            # TODO: this should be dynamic depending on whether we're a client or server
            network_game_data=self.get_network_game_data(
                network_game_client_address + 2140
            ),  # from network_game_client_add_player_to_game
        )

    def get_network_game_server(self):

        # from network_game_server_create(), network_game_server_memory_do_not_use_directly
        network_game_server_address = 0x2FBE40

        return dict(
            address=hex(self.mem_reader.get_host_address(network_game_server_address)),
            values=self.mem_reader.get_formatted_bytes(
                network_game_server_address, 1212
            ),
            countdown_active=self.mem_reader.read_u8(
                network_game_server_address + 1172
            ),
            countdown_paused=self.mem_reader.read_u8(
                network_game_server_address + 1173
            ),
            countdown_adjusted_time=self.mem_reader.read_u8(
                network_game_server_address + 1174
            ),
        )

    def dump_game_update_contents(self):
        # FIXME: this whole section is only valid if you're breaking in network_game_client_handle_game_update()

        network_game_client = 0x2FB180
        data_queue = 0x2E87E4
        packet_data_address = 0xD00E82D0  # TODO: find this dynamically -- this was pulled directly from IDA/gdb and changes on restart

        # Cache network data and data queue
        self.mem_reader.get_host_address(packet_data_address)
        self.mem_reader.add_to_cache(packet_data_address, 5000)
        self.mem_reader.get_host_address(data_queue)
        self.mem_reader.add_to_cache(data_queue, 520)

        # Read update queue address
        update_queue_address = self.mem_reader.read_u32(0x2E8870)
        update_client_player_address = self.mem_reader.read_u32(
            update_queue_address + 0x34
        )
        update_client_blind_first_element_address = update_queue_address + 0x38

        # Warning if there's a mismatch in addresses
        if update_client_player_address != update_client_blind_first_element_address:
            print(
                f"==> WARNING: update client queue address mismatch: {update_client_player_address} != {update_client_blind_first_element_address}"
            )

        # Use local variables to avoid repeated reads
        u32_2E8870 = self.mem_reader.read_u32(0x2E8870)
        host_u32_2E8870 = self.mem_reader.get_host_address(u32_2E8870)
        u32_2E87E4 = self.mem_reader.read_u32(0x2E87E4)
        u32_2E87E8 = self.mem_reader.read_u32(0x2E87E8)

        return {
            "packet_data_address": f"{packet_data_address:#x} -> {self.mem_reader.get_host_address(packet_data_address):#x}",
            "network_game_client": f"{network_game_client:#x} -> {self.mem_reader.get_host_address(network_game_client):#x}",
            "data_queue_address": f"{data_queue:#x} -> {self.mem_reader.get_host_address(data_queue):#x}",
            "dword_2E87E4": f"{hex(u32_2E87E4)} & 0x7F = {u32_2E87E4 & 0x74}",
            "dword_2E87E8": hex(u32_2E87E8),
            "dword_2E8870": f"{hex(u32_2E8870)} -> {hex(host_u32_2E8870)}",
            "dword_2E8874": hex(self.mem_reader.read_u32(0x2E8874)),
            "dword_2E8870_plus46": hex(self.mem_reader.read_s16(u32_2E8870 + 46)),
            "dword_2E8870_plus52": f"{hex(self.mem_reader.read_u32(u32_2E8870 + 52))} -> {hex(self.mem_reader.get_host_address(self.mem_reader.read_u32(u32_2E8870 + 52)))}",
            "header": {
                "tick": self.mem_reader.read_s32(data_queue),
                "global_random": hex(self.mem_reader.read_u32(data_queue + 4)),
                "tick_2": self.mem_reader.read_s32(data_queue + 8),
                "unk_1": self.mem_reader.read_u16(data_queue + 12),  # player index?
                "player_count": self.mem_reader.read_s16(data_queue + 14),
            },
            "data": {
                # Placeholder for additional data elements (uncomment if needed)
            },
            "update_queue_header": self.mem_reader.get_formatted_bytes(
                u32_2E8870, 0x34
            ),
            "update_queue_values": {
                "unk_1": self.mem_reader.read_s16(
                    update_queue_address + 0x20
                ),  # max element count?
                "unk_2": self.mem_reader.read_s16(
                    update_queue_address + 0x22
                ),  # element length?
                "unk_3": self.mem_reader.read_s16(
                    update_queue_address + 0x24
                ),  # not sure
                "unk_4": self.mem_reader.read_s16(
                    update_queue_address + 0x2E
                ),  # element count?
                "unk_5": self.mem_reader.read_s16(
                    update_queue_address + 0x30
                ),  # also element count?
                "unk_6": f"{hex(self.mem_reader.read_u16(update_queue_address + 0x32))} ({self.mem_reader.read_u16(update_queue_address + 0x32)})",
            },
            "queue_ids": " ".join(
                hex(
                    self.mem_reader.read_u16(
                        update_client_blind_first_element_address + 0x28 * i
                    )
                )[2:]
                for i in range(20)
            ),
        }

    def get_animation_debug_info(self, unk_handle, animation_id, animation_tick):
        """
        from animation_update_internal()

        :param unk_handle:
        :param animation_id:
        :param animation_tick:
        :return:
        """

        tag_address = self.mem_reader.read_u32(
            32 * (unk_handle & 0xFFFF) + self.global_tag_instances_address + 20
        )
        animation_address = (
            self.mem_reader.read_u32(tag_address + 120) + 180 * animation_id
        )

        animation_length = self.mem_reader.read_s16(animation_address + 34)
        unk_46 = self.mem_reader.read_s16(animation_address + 46)
        unk_52 = self.mem_reader.read_s16(animation_address + 52)
        unk_54 = self.mem_reader.read_s16(animation_address + 54)

        if animation_tick < animation_length:
            if animation_tick != animation_length or unk_46 != 0:
                result = int(animation_tick + 1 == unk_52 or animation_tick == unk_54)
            else:
                result = 2
        else:
            if unk_46 <= 0:
                result = 3
            else:
                result = 4

        return dict(
            tag_address=hex(tag_address),
            animation_address=hex(animation_address),
            animation_length=animation_length,
            unk_handle=hex(unk_handle),
            animation_id=animation_id,
            animation_tick=animation_tick,
            unk_46=unk_46,
            unk_52=unk_52,
            unk_54=unk_54,
            result=result,
        )

    def arrange_objects_by_type(self, objects):
        """
        :param objects:
        :return:
        """

        objects_meta = dict(
            object_indexes_by_type=defaultdict(list),
            object_ids_by_type=defaultdict(list),
            projectiles_by_unit_id=defaultdict(list),
        )

        for i, o in enumerate(objects):
            object_type = o["object_type_string"]
            objects_meta["object_indexes_by_type"][object_type].append(i)
            objects_meta["object_ids_by_type"][object_type].append(o["object_id"])
            if object_type == "projectile":
                # player_id = int(o['ultimate_parent'], 16) & 0xFFFF
                objects_meta["projectiles_by_unit_id"][o["owner_unit_ref"]].append(i)

        # if objects_by_type['projectiles_by_unit_id']:
        #     print(objects_by_type['projectiles_by_unit_id'])

        return objects_meta

    def get_game_info(self) -> dict:

        # FIXME: also support campaign (e.g. prisoner bots)
        #        currently fails when getting gametype for score

        player_count = self.mem_reader.read_u16(self.player_datum_array + 0x2E)
        player_stat_array = []

        # dict of dicts of the form {<player index dealing damage>: {<player index taking damage>: <damage amount>}}
        damage_counts = defaultdict(dict)

        game_time = self.mem_reader.read_u32(self.game_time_globals_address + 12)
        game_time_elapsed = self.mem_reader.read_u32(
            self.game_time_globals_address + 16
        )
        # print(game_time, game_time_elapsed)

        game_time_initialized = self.mem_reader.read_u8(self.game_time_globals_address)
        game_time_active = self.mem_reader.read_u8(self.game_time_globals_address + 1)
        game_time_paused = self.mem_reader.read_u8(self.game_time_globals_address + 2)
        game_time_speed = self.mem_reader.read_float(
            self.game_time_globals_address + 24
        )  # 1.0 is normal speed
        game_time_leftover_dt = self.mem_reader.read_float(
            self.game_time_globals_address + 28
        )

        game_globals_map_loaded = self.mem_reader.read_u8(self.game_globals_address)
        game_globals_active = self.mem_reader.read_u8(self.game_globals_address + 1)

        main_menu_is_active = self.mem_reader.read_u8(0x2E4068)

        game_engine_globals_address = self.mem_reader.read_u32(0x2F9110)

        # game_in_progress
        #   splitscreen
        #   1 1 0 = ingame/postgame/mainmenu
        #   1 0 1 = choose map / pregame / singleplayer paused
        #   1 0 0 = briefly while loading game or changing from postgame to choose map screen
        #   0 0 0 = briefly after singleplayer save and quit (between 110 ingame and 110 main menu)
        if self.last_game_in_progress != (
            game_time_initialized,
            game_time_active,
            game_time_paused,
        ):
            print(
                f"game in progress changed to {game_time_initialized=} {game_time_active=} {game_time_paused=}"
            )
            last_game_in_progress = (
                game_time_initialized,
                game_time_active,
                game_time_paused,
            )

        # game_connection
        #   0 = menus or singleplayer
        #   1 = system link -- looking for games / joined in network pregame
        #   2 = splitscreen -- hosting pregame lobby waiting for players
        #       system link -- hosting pregame (starts when pressing A on 'looking for games' screen)
        #   3 = watching 'saved film'
        game_connection = self.mem_reader.read_u16(self.game_connection_address)
        if self.last_game_connection != game_connection:
            print("game_connection changed to {}".format(hex(game_connection)))
            self.last_game_connection = game_connection

        object_header_datum_array = self.mem_reader.read_u32(0x2FC6AC)
        object_header_datum_array_max_elements = self.mem_reader.read_u16(
            object_header_datum_array + 0x20
        )
        object_header_datum_array_element_size = self.mem_reader.read_u16(
            object_header_datum_array + 0x22
        )
        object_header_datum_array_allocated_object_count = self.mem_reader.read_u16(
            object_header_datum_array + 0x2E
        )
        object_header_datum_array_element_count = self.mem_reader.read_u16(
            object_header_datum_array + 0x30
        )
        object_header_datum_array_first_element_address = self.mem_reader.read_u32(
            object_header_datum_array + 0x34
        )

        # TODO: also check if this is a multiplayer game or campaign
        if game_time_initialized and game_time_active and not main_menu_is_active:

            for player_index in range(player_count):

                # looks like this in IDA: *(_DWORD *)(player_data + 52) + 212 * a1;
                static_player_address = (
                    self.player_datum_array_first_element_address
                    + player_index * self.player_datum_array_element_size
                )

                player_object_handle = self.mem_reader.read_s32(
                    static_player_address + 0x34
                )
                previous_player_object_handle = self.mem_reader.read_s32(
                    static_player_address + 0x38
                )
                player_object_id = player_object_handle & 0xFFFF

                # *(_DWORD *)(*(_DWORD *)(object_header_data + 52) + 12 * (unsigned __int16)v3 + 8);
                dynamic_player_address = self.mem_reader.read_u32(
                    object_header_datum_array_first_element_address
                    + (player_object_handle & 0xFFFF)
                    * object_header_datum_array_element_size
                    + 8
                )

                previous_dynamic_player_address = self.mem_reader.read_u32(
                    object_header_datum_array_first_element_address
                    + (previous_player_object_handle & 0xFFFF)
                    * object_header_datum_array_element_size
                    + 8
                )

                # print('dynamic player address: {} | {}'.format(hex(dynamic_player_address), dynamic_player_address))
                # print('player_object_handle: {} | {}'.format(hex(player_object_handle), player_object_handle))

                player_object_debug = dict(
                    player_object_handle=hex(player_object_handle),
                    # player_object_handle_u32=hex(self.mem_reader.read_u32(static_player_address + 0x34)),
                    object_header_datum_array=f'{hex(self.mem_reader.read_u32(object_header_datum_array))} @ {hex(object_header_datum_array)} -> {hex(self.mem_reader.known_addresses[object_header_datum_array]["host_address"])}',
                    object_header_datum_array_first_element_address=hex(
                        object_header_datum_array_first_element_address
                    ),
                    dynamic_player_address=(
                        f"{hex(dynamic_player_address)} -> {hex(self.mem_reader.get_host_address(dynamic_player_address))}"
                        if player_object_handle != -1
                        else ""
                    ),
                    player_object_id=player_object_id,
                    static_player_address=f"{hex(static_player_address)} -> {hex(self.mem_reader.get_host_address(static_player_address))}",
                    # object_header_datum_array_max_elements=object_header_datum_array_max_elements,
                    # object_header_datum_array_element_size=object_header_datum_array_element_size,
                    # object_header_datum_array_allocated_object_count=object_header_datum_array_allocated_object_count,
                    # object_header_datum_array_element_count=object_header_datum_array_element_count,
                )

                # see game_statistics_record_kill() for assist logic
                #   track the last 4 damagers
                #   on death, find the max total damage for the damagers who damaged in the past 6 seconds
                #   the assist damage threshold is 40% of that max damage amount
                #
                # NOTE: dynamic player object is unassigned on the same tick as death, so we need to look at the old object
                #       to see the final damage that killed them.
                # FIXME: if saving full game replay takes too long, this will return 0x0 + 0x3E0
                if player_object_handle == -1:
                    damage_table_address = (
                        self.mem_reader.read_u32(
                            object_header_datum_array_first_element_address
                            + (previous_player_object_handle & 0xFFFF)
                            * object_header_datum_array_element_size
                            + 8
                        )
                        + 0x3E0
                    )
                else:
                    damage_table_address = dynamic_player_address + 0x3E0
                player_object_debug["damage_table_address"] = (
                    f"{hex(damage_table_address)} -> {hex(self.mem_reader.get_host_address(damage_table_address))}"
                )
                damage_table = []
                for i in range(4):
                    damage_time = self.mem_reader.read_u32(
                        damage_table_address + 16 * i
                    )
                    if damage_time != 0xFFFFFFFF:
                        damage_amount = self.mem_reader.read_float(
                            damage_table_address + 16 * i + 4
                        )
                        static_player = self.mem_reader.read_u32(
                            damage_table_address + 16 * i + 12
                        )
                        damage_table.append(
                            dict(
                                damage_time=damage_time,
                                damage_amount=damage_amount,
                                # note: dynamic object id doesn't change if the player dies and re-damages with a new object id
                                dynamic_player=self.mem_reader.read_u32(
                                    damage_table_address + 16 * i + 8
                                ),
                                static_player=static_player,
                            )
                        )
                        # FIXME: temporary for debug purposes, remove
                        damage_table[-1].update(
                            dict(
                                dynamic_player_hex=hex(
                                    damage_table[-1]["dynamic_player"]
                                ),
                                static_player_hex=hex(
                                    damage_table[-1]["static_player"]
                                ),
                            )
                        )
                        # FIXME: should we exclude overkill damage? (e.g. shooting a rocket at someone with 5 health)
                        last_death = self.mem_reader.read_u32(
                            static_player_address + 0x84
                        )
                        if player_object_handle != -1 or last_death == game_time - 1:
                            damage_counts[static_player & 0xFFFF][
                                player_index
                            ] = damage_amount

                if player_object_handle != -1:

                    # FIXME: avoid the forced qmp lookup inself.mem_reader.get_host_address
                    # player_object_debug.update(dynamic_player_address_hex=f'{hex(dynamic_player_address)} -> {hex(self.mem_reader.get_host_address(dynamic_player_address))}')

                    # selected_weapon_handle = self.mem_reader.read_u32(dynamic_player_address + 4 * self.mem_reader.read_u16(dynamic_player_address + 0x2A2) + 0x2A8)
                    # selected_weapon_address = self.mem_reader.read_u32(self.mem_reader.read_u32(object_header_datum_array + 52) + 12 * (selected_weapon_handle & 0xFFFF) + 8)

                    r"""
                    v6 = *(_DWORD *)(32
                        * (**(_DWORD **)(*(_DWORD *)(object_header_data + 52) + 12 * (unsigned __int16)v5 + 8) & 0xFFFF)
                        + global_tag_instances
                        + 20);

                        70 61 65 77 6D 65 74 69 65 6A 62 6F 6B 01 DF E2 B4 71 3B 80 B4 7B 81 80 00 00 00 00 00 00 00 00
                        \___________________,________________/          |           |
                                      paewmetiejbo                     +16         +20
                    """
                    # selected_weapon_tag_address = 32 * self.mem_reader.read_s16(selected_weapon_address) + global_tag_instances_address# + 20
                    # tag_plus_16 = self.mem_reader.read_u32(selected_weapon_tag_address + 16)
                    # tag_plus_20 = self.mem_reader.read_u32(selected_weapon_tag_address + 20)

                    # selected_weapon_tag_address = self.mem_reader.read_u32(32 * self.mem_reader.read_s16(selected_weapon_address) + global_tag_instances_address + 20)

                    def get_weapon(weapon_object_handle):
                        """
                        starting weapons owned by players appear to have object ids adjacent to their owners
                            if player is id 28, his weapons are 29 and 30
                            player object ids appear to go 28, 31, 34, ... not sure if this is a strict rule
                            (probably just because they get allocated right after their player is allocated.)
                        :param weapon_object_handle:
                        :return:
                        """

                        # TODO: don't even call get_weapon if we have a 0xFFFFFFFF handle
                        if weapon_object_handle == 0xFFFFFFFF:
                            return {}

                        weapon_object_address = self.mem_reader.read_u32(
                            self.mem_reader.read_u32(object_header_datum_array + 52)
                            + 12 * (weapon_object_handle & 0xFFFF)
                            + 8
                        )
                        # TODO: better early exit logic
                        if weapon_object_address == 0x0:
                            return {}
                        tag_address = (
                            32 * self.mem_reader.read_s16(weapon_object_address)
                            + self.global_tag_instances_address
                        )
                        weapon_type = self.mem_reader.read_u8(
                            self.mem_reader.read_u32(tag_address + 20) + 0x309
                        )
                        is_energy_weapon = bool(weapon_type & 8)

                        return dict(
                            # tag_object_id=self.mem_reader.read_s16(weapon_object_address),
                            # x=self.mem_reader.read_float(weapon_object_address + 0x50),
                            # y=self.mem_reader.read_float(weapon_object_address + 0x54),
                            # z=self.mem_reader.read_float(weapon_object_address + 0x58),
                            heat_meter=self.mem_reader.read_float(
                                weapon_object_address + 0xD4
                            ),  # FIXME: seems to also be used for human weapons, need to figure out what
                            used_energy=self.mem_reader.read_float(
                                weapon_object_address + 0xE0
                            ),  # only if energy weapon
                            charge_amount=self.mem_reader.read_float(
                                weapon_object_address + 0xF0
                            ),  # remaining energy for PR, current overcharge for PP
                            reloading=self.mem_reader.read_u8(
                                weapon_object_address + 0x258
                            ),  # 1 while reloading until reload_time hits 2
                            can_fire=self.mem_reader.read_u8(
                                weapon_object_address + 0x259
                            ),
                            reload_time=self.mem_reader.read_s16(
                                weapon_object_address + 0x25A
                            ),
                            backpack_ammo_count=self.mem_reader.read_s16(
                                weapon_object_address + 0x25E
                            ),
                            magazine_ammo_count=self.mem_reader.read_s16(
                                weapon_object_address + 0x260
                            ),
                            weapon_tag_address=f'{self.mem_reader.read_u32(tag_address)} @ {hex(tag_address)} -> {hex(self.mem_reader.known_addresses[tag_address]["host_address"])}',
                            # owner=self.mem_reader.read_u32(weapon_object_address + 0x1E0),  # TODO: this isn't really owner, seems to correlate to current action
                            # owner_hex=hex(self.mem_reader.read_u32(weapon_object_address + 0x1E0)),
                            energy_used=self.mem_reader.read_float(
                                weapon_object_address + 0x1F0
                            ),  # used for whether to delete dropped energy weapon (if == 1.0)
                            weapon_type=weapon_type,  # from weapon_trigger_fire()
                            is_energy_weapon=is_energy_weapon,
                            zoom_levels=self.mem_reader.read_s16(
                                self.mem_reader.read_u32(tag_address + 20) + 986
                            ),
                            zoom_min=self.mem_reader.read_float(
                                self.mem_reader.read_u32(tag_address + 20) + 988
                            ),
                            zoom_max=self.mem_reader.read_float(
                                self.mem_reader.read_u32(tag_address + 20) + 992
                            ),
                            autoaim_angle=self.mem_reader.read_float(
                                self.mem_reader.read_u32(tag_address + 20) + 996
                            ),  # radians, from unit_get_aim_assist_parameters()
                            autoaim_range=self.mem_reader.read_float(
                                self.mem_reader.read_u32(tag_address + 20) + 1000
                            ),
                            magnetism_angle=self.mem_reader.read_float(
                                self.mem_reader.read_u32(tag_address + 20) + 1004
                            ),
                            magnetism_range=self.mem_reader.read_float(
                                self.mem_reader.read_u32(tag_address + 20) + 1008
                            ),
                            deviation_angle=self.mem_reader.read_float(
                                self.mem_reader.read_u32(tag_address + 20) + 1012
                            ),
                            # tag_plus_16=f'{self.mem_reader.read_u32(tag_plus_16)} :: {hex(tag_plus_16)} -> {hex(self.mem_reader.known_addresses[tag_plus_16]["host_address"])}',
                            # tag_plus_20=f'{self.mem_reader.read_u32(tag_plus_20)} :: {hex(tag_plus_20)} -> {hex(self.mem_reader.known_addresses[tag_plus_20]["host_address"])}',
                            tag_name=self.mem_reader.read_string(
                                self.mem_reader.read_u32(tag_address + 0x10)
                            ),
                            object_id=weapon_object_handle & 0xFFFF,
                        )

                    # TODO: move this out of get_game_info
                    def get_weapons(first_weapon_address):
                        weapons = []
                        for weapon_index in range(4):
                            weapon = get_weapon(
                                self.mem_reader.read_u32(
                                    first_weapon_address + 4 * weapon_index
                                )
                            )
                            if weapon:
                                weapons.append(weapon)
                        return weapons

                    biped_tag_address = self.mem_reader.read_u32(
                        32 * (self.mem_reader.read_u32(dynamic_player_address) & 0xFFFF)
                        + self.global_tag_instances_address
                        + 0x14
                    )
                    biped_camera_height_standing = self.mem_reader.read_float(
                        biped_tag_address + 0x400
                    )
                    biped_camera_height_crouching = self.mem_reader.read_float(
                        biped_tag_address + 0x404
                    )
                    crouchscale = self.mem_reader.read_float(
                        dynamic_player_address + 0x464
                    )

                    player_object_debug["biped_tag_address"] = (
                        f"{hex(biped_tag_address)} -> {hex(self.mem_reader.get_host_address(biped_tag_address))}"
                    )

                    # TODO: change to dataclasses instead of dicts?
                    player_object_data = dict(
                        flags=self.mem_reader.read_u32(
                            dynamic_player_address + 0x4
                        ),  # & 0x10000 is garbage_bit, & 8 is connected_to_map_bit, & 1 is 1 for vehicle weapons (checked in find_aim_assist_targets_recursive())
                        x=self.mem_reader.read_float(dynamic_player_address + 0xC),
                        y=self.mem_reader.read_float(dynamic_player_address + 0x10),
                        z=self.mem_reader.read_float(dynamic_player_address + 0x14),
                        x_vel=self.mem_reader.read_float(
                            dynamic_player_address + 0x18
                        ),  # object.translational_velocity
                        y_vel=self.mem_reader.read_float(dynamic_player_address + 0x1C),
                        z_vel=self.mem_reader.read_float(dynamic_player_address + 0x20),
                        legs_pitch=self.mem_reader.read_float(
                            dynamic_player_address + 0x24
                        ),  # legs? TODO: see end of sub_152E40() in 2276betaP, looks like object.forward and object.up for next 6 floats
                        legs_yaw=self.mem_reader.read_float(
                            dynamic_player_address + 0x28
                        ),  # legs?
                        legs_roll=self.mem_reader.read_float(
                            dynamic_player_address + 0x2C
                        ),  # legs?
                        pitch1=self.mem_reader.read_float(
                            dynamic_player_address + 0x30
                        ),  # these get set in biped_snap_facing(), not sure what it is. (0, 0, 1) in most cases
                        yaw1=self.mem_reader.read_float(dynamic_player_address + 0x34),
                        roll1=self.mem_reader.read_float(dynamic_player_address + 0x38),
                        ang_vel_x=self.mem_reader.read_float(
                            dynamic_player_address + 0x3C
                        ),
                        ang_vel_y=self.mem_reader.read_float(
                            dynamic_player_address + 0x40
                        ),
                        ang_vel_z=self.mem_reader.read_float(
                            dynamic_player_address + 0x44
                        ),
                        aim_assist_sphere_x=self.mem_reader.read_float(
                            dynamic_player_address + 0x50
                        ),  # center point? used in find_aim_assist_targets_recursive()
                        aim_assist_sphere_y=self.mem_reader.read_float(
                            dynamic_player_address + 0x54
                        ),
                        aim_assist_sphere_z=self.mem_reader.read_float(
                            dynamic_player_address + 0x58
                        ),
                        aim_assist_sphere_radius=self.mem_reader.read_float(
                            dynamic_player_address + 0x5C
                        ),  # sphere radius? find_aim_assist_targets_recursive()
                        scale=self.mem_reader.read_float(
                            dynamic_player_address + 0x60
                        ),  # object.scale (items only?)
                        type=self.mem_reader.read_u16(dynamic_player_address + 0x64),
                        render_flags=self.mem_reader.read_u16(
                            dynamic_player_address + 0x66
                        ),
                        weapon_owner_team=self.mem_reader.read_s16(
                            dynamic_player_address + 0x68
                        ),  # weapon.owner_team_index (e.g. ctf) -- also used in find_aim_assist_targets_recursive() for team check
                        powerup_unk2=self.mem_reader.read_s16(
                            dynamic_player_address + 0x6A
                        ),
                        idle_ticks=self.mem_reader.read_s16(
                            dynamic_player_address + 0x6C
                        ),
                        # animation_unk_1=hex(self.mem_reader.read_u32(dynamic_player_address + 0x7C)),
                        # animation_unk_2=hex(self.mem_reader.read_s16(dynamic_player_address + 0x80)),
                        # animation_unk_3=hex(self.mem_reader.read_s16(dynamic_player_address + 0x82)),
                        max_health=self.mem_reader.read_float(
                            dynamic_player_address + 0x88
                        ),
                        max_shields=self.mem_reader.read_float(
                            dynamic_player_address + 0x8C
                        ),
                        health=self.mem_reader.read_float(
                            dynamic_player_address + 0x90
                        ),
                        shields=self.mem_reader.read_float(
                            dynamic_player_address + 0x94
                        ),
                        unk_dmg_countdown_0x98=self.mem_reader.read_float(
                            dynamic_player_address + 0x98
                        ),  # starts counting down immediately
                        unk_dmg_countdown_0x9C=self.mem_reader.read_float(
                            dynamic_player_address + 0x9C
                        ),
                        unk_dmg_countdown_0xA4=self.mem_reader.read_float(
                            dynamic_player_address + 0xA4
                        ),  # starts counting down after 2 second delay (after 0xAC counts up to 60), initial value is higher for higher damage amount?
                        unk_dmg_countdown_0xA8=self.mem_reader.read_float(
                            dynamic_player_address + 0xA8
                        ),
                        unk3=self.mem_reader.read_s32(
                            dynamic_player_address + 0xAC
                        ),  # from object_damage_update(), tied to countdowns 0x98 and 0xA4, -1 normally, counts up to ~75 when damaged
                        unk4=self.mem_reader.read_s32(
                            dynamic_player_address + 0xB0
                        ),  # from object_damage_update(), tied to countdowns 0x9C and 0xA8, -1 normally
                        # shields_status_2=hex(self.mem_reader.read_u16(dynamic_player_address + 0xB2)),
                        shields_charge_delay=self.mem_reader.read_u16(
                            dynamic_player_address + 0xB4
                        ),  # from object_damage_update()
                        # 0x4096 when shields are charging, 0x4112 when overshield charging
                        shields_status=self.mem_reader.read_u16(
                            dynamic_player_address + 0xB6
                        ),  # 0x0 normally, 0x10 while overshield charging, 0x1000 while shields charging, 0x8 while shields are fully depleted
                        shields_status_hex=hex(
                            self.mem_reader.read_u16(dynamic_player_address + 0xB6)
                        ),
                        next_object=self.mem_reader.read_s32(
                            dynamic_player_address + 0xC4
                        ),
                        next_object_2=hex(
                            self.mem_reader.read_u32(dynamic_player_address + 0xC8)
                        ),  # used in find_aim_assist_targets_recursive(), seems to be object handle for next object in object table
                        # seems like normal path for players goes to biped_get_sight_position()
                        parent_object=hex(
                            self.mem_reader.read_s32(dynamic_player_address + 0xCC)
                        ),  # e.g. vehicle
                        # unk_camera_0xB6=self.mem_reader.read_u8(dynamic_player_address + 0xB6),  # both of these are 0 for players, from unit_get_camera_position()
                        # unk_camera_0x64=self.mem_reader.read_s16(dynamic_player_address + 0x64),
                        camo=self.mem_reader.read_u8(
                            dynamic_player_address + 0x1B4
                        ),  # 65=nocamo (01000001), 81=camo (01010001)
                        flashlight=self.mem_reader.read_u8(
                            dynamic_player_address + 0x1B6
                        ),
                        current_action=self.mem_reader.read_u32(
                            dynamic_player_address + 0x1B8
                        ),  # multi bitfield: some functions only check second byte
                        # 0x0000=no_action
                        # 0x0001=crouch
                        # 0x0002=jump
                        # 0x0008=fire
                        # 0x0010=flashlight    immediately goes back to 0x0 even if held
                        # 0x0440=press_action    cycles back to 0x0 before going to 0x4000
                        # 0x0800=shooting
                        # 0x2fc4=grenade
                        # 0x4000=hold_action
                        # stunned=self.mem_reader.read_s32(dynamic_player_address + 0x1CB),  # from biped_jump -- this isn't actually stunned
                        stunned=self.mem_reader.read_float(
                            dynamic_player_address + 0x3D4
                        ),  # from biped_jump -- this isn't actually stunned
                        # maybe_desired_facing_vector_x=self.mem_reader.read_float(dynamic_player_address + 0x1C8),
                        # maybe_desired_facing_vector_y=self.mem_reader.read_float(dynamic_player_address + 0x1CC),  # FIXME: y is null
                        # maybe_desired_facing_vector_z=self.mem_reader.read_float(dynamic_player_address + 0x1D0),
                        xunk0=self.mem_reader.read_float(
                            dynamic_player_address + 0x1D4
                        ),  # unknown, from biped_update_turning(), gets multiplied by leg rotation 24, 28, 2c.
                        yunk0=self.mem_reader.read_float(
                            dynamic_player_address + 0x1D8
                        ),
                        zunk0=self.mem_reader.read_float(
                            dynamic_player_address + 0x1DC
                        ),  # z seems to stay at 0.0, but periodically will briefly flip to same z as others
                        xaima=self.mem_reader.read_float(
                            dynamic_player_address + 0x1E0
                        ),  # unit vectors, -1 to 1 on x y z axes.
                        yaima=self.mem_reader.read_float(
                            dynamic_player_address + 0x1E4
                        ),
                        zaima=self.mem_reader.read_float(
                            dynamic_player_address + 0x1E8
                        ),
                        aiming_vector_x=self.mem_reader.read_float(
                            dynamic_player_address + 0x1EC
                        ),  # used in first_person_camera_deterministic(), which gets used in player_aim_projectile()
                        aiming_vector_y=self.mem_reader.read_float(
                            dynamic_player_address + 0x1F0
                        ),
                        aiming_vector_z=self.mem_reader.read_float(
                            dynamic_player_address + 0x1F4
                        ),
                        xaim0=self.mem_reader.read_float(
                            dynamic_player_address + 0x1F8
                        ),  # these seem to be used for projectiles -- see projectile_update()
                        yaim0=self.mem_reader.read_float(
                            dynamic_player_address + 0x1FC
                        ),
                        zaim0=self.mem_reader.read_float(
                            dynamic_player_address + 0x200
                        ),
                        xaim1=self.mem_reader.read_float(
                            dynamic_player_address + 0x204
                        ),  # look in players_update_before_game() and unit_control()
                        yaim1=self.mem_reader.read_float(
                            dynamic_player_address + 0x208
                        ),
                        zaim1=self.mem_reader.read_float(
                            dynamic_player_address + 0x20C
                        ),
                        looking_vector_x=self.mem_reader.read_float(
                            dynamic_player_address + 0x210
                        ),
                        looking_vector_y=self.mem_reader.read_float(
                            dynamic_player_address + 0x214
                        ),
                        looking_vector_z=self.mem_reader.read_float(
                            dynamic_player_address + 0x218
                        ),
                        move_forward=self.mem_reader.read_float(
                            dynamic_player_address + 0x228
                        ),  # throttle?
                        move_left=self.mem_reader.read_float(
                            dynamic_player_address + 0x22C
                        ),
                        move_up=self.mem_reader.read_float(
                            dynamic_player_address + 0x230
                        ),  # not sure if this is used anywhere? banshee controls? observer?
                        # note: check out search for header->event_type in 2276betaP, animation types? (not sure if these are the same animations, but noting here anyway for later)
                        #       & 0xFC == 8     _playback_animation_state_set
                        #       & 0xFC == 12    _playback_aiming_speed_set
                        #       & 0xFC == 16    _playback_control_flags_set
                        #       & 0xFC == 20    _playback_weapon_index_set
                        #       & 0xFC == 24    _playback_throttle_set
                        melee_damage_type=self.mem_reader.read_u8(
                            dynamic_player_address + 0x239
                        ),  # see unit_cause_continuous_melee_damage(), if =4 then continuous melee damage, if =3 then impact melee damage, players are =0
                        animation_1=self.mem_reader.read_u8(
                            dynamic_player_address + 0x253
                        ),  # see unit_update_animation() and unit_get_custom_animation_time(), 0x253 and 0x254 both seem related to animations (movement, grenade throwing, melee, etc)
                        animation_2=self.mem_reader.read_u8(
                            dynamic_player_address + 0x254
                        ),
                        animation_debug=self.get_animation_debug_info(
                            self.mem_reader.read_u32(dynamic_player_address + 0x7C),
                            self.mem_reader.read_s16(dynamic_player_address + 0x80),
                            self.mem_reader.read_s16(dynamic_player_address + 0x82),
                        ),
                        selected_weapon_index=self.mem_reader.read_s16(
                            dynamic_player_address + 0x2A2
                        ),  # 0 or 1 for primary/secondary, -1 for none, see first_person_weapon_index_from_weapon_index()
                        # selected_weapon_index_2=self.mem_reader.read_s16(dynamic_player_address + 0x2A4),  # seems to only matter if you fully drop a weapon without picking up a replacement
                        # primary_weapon_object=self.mem_reader.read_u32(dynamic_player_address + 0x2A8),
                        # secondary_weapon_object=self.mem_reader.read_u32(dynamic_player_address + 0x2AC),
                        # selected_weapon_object=self.mem_reader.read_u32(dynamic_player_address + 4 * self.mem_reader.read_u16(dynamic_player_address + 0x2A2) + 0x2A8),
                        # selected_weapon_object_hex=f'{hex(selected_weapon_handle)} -> {hex(selected_weapon_handle & 0xFFFF)=}',
                        # selected_weapon_address=selected_weapon_address,
                        # selected_weapon_address_hex=f'{self.mem_reader.read_u32(selected_weapon_address)} @ {hex(selected_weapon_address)} -> {hex(self.mem_reader.known_addresses[selected_weapon_address]["host_address"])}',
                        # weapons=[get_weapon(self.mem_reader.read_u32(dynamic_player_address + 0x2A8 + 4 * weapon_index)) for weapon_index in range(4)],
                        weapons=get_weapons(dynamic_player_address + 0x2A8),
                        # weapon_0=get_weapon(self.mem_reader.read_u32(dynamic_player_address + 0x2A8)),
                        # weapon_1=get_weapon(self.mem_reader.read_u32(dynamic_player_address + 0x2AC)),
                        # weapon_2=get_weapon(self.mem_reader.read_u32(dynamic_player_address + 0x2B0)),
                        # weapon_3=get_weapon(self.mem_reader.read_u32(dynamic_player_address + 0x2B4)),
                        # selected_weapon=get_weapon(self.mem_reader.read_u32(dynamic_player_address + 4 * self.mem_reader.read_u16(dynamic_player_address + 0x2A2) + 0x2A8)),
                        current_equipment=hex(
                            self.mem_reader.read_u32(dynamic_player_address + 0x2C8)
                        ),
                        primary_nades=self.mem_reader.read_u8(
                            dynamic_player_address + 0x2CE
                        ),
                        secondary_nades=self.mem_reader.read_u8(
                            dynamic_player_address + 0x2CF
                        ),
                        zoom_level=self.mem_reader.read_s8(
                            dynamic_player_address + 0x2D0
                        ),
                        camo_amount=self.mem_reader.read_float(
                            dynamic_player_address + 0x32C
                        ),  # 0=nocamo, 1=fullcamo, from game_engine_player_depower_active_camo(), also see unit_update()
                        # camo_thing2=self.mem_reader.read_float(dynamic_player_address + 0x330),  # from first_person_weapon_draw() and unit_update()
                        # 0 normally, 1 when player has camo and is revealed by shooting (but not being shot at)
                        camo_self_revealed=self.mem_reader.read_u16(
                            dynamic_player_address + 0x3D2
                        ),  # from player_powerup_on(), not sure when this actually gets set
                        # see game_statistics_record_kill() and unit_record_damage()
                        damagers_list_address=hex(
                            self.mem_reader.get_host_address(
                                dynamic_player_address + 0x3E0
                            )
                        ),
                        crouchscale=crouchscale,
                        # seems like if x or y is greater than z, you start sliding or falling? you can watch it change when slowly walking off a ledge
                        facing1=self.mem_reader.read_float(
                            dynamic_player_address + 0x46C
                        ),  # used in biped_snap_facing, not sure purpose (usually 0,0,1 on flat ground)
                        facing2=self.mem_reader.read_float(
                            dynamic_player_address + 0x470
                        ),  # except when on small ledges? e.g. on flat part of zyos ledge x increases as you get farther from wall
                        facing3=self.mem_reader.read_float(
                            dynamic_player_address + 0x474
                        ),  # on zyos ledge diagonal part the z value starts decreasing from 1. also changes on small depressions in priz floor and ramps
                        # from biped_get_sight_position()
                        camera_x=self.mem_reader.read_float(
                            dynamic_player_address + 0xC
                        ),
                        camera_y=self.mem_reader.read_float(
                            dynamic_player_address + 0x10
                        ),
                        camera_z=(1 - crouchscale) * biped_camera_height_standing
                        + crouchscale * biped_camera_height_crouching
                        + self.mem_reader.read_float(dynamic_player_address + 0x14),
                        air_1_0x64=self.mem_reader.read_s16(
                            dynamic_player_address + 0x64
                        ),  # any_player_is_in_the_air() and unit_get_camera_position()
                        airborne=self.mem_reader.read_u8(
                            dynamic_player_address + 0x424
                        ),  # &1 = airborne, &2 = slipping, 0 = standing, from biped_update()
                        landing_stun_current_duration=self.mem_reader.read_u8(
                            dynamic_player_address + 0x428
                        ),  # any_player_is_in_the_air(), when you land from a jump, seems to be impact intensity (1 or 2 being flat ground jump, 30 for jumping off top priz fall damage). slowly ramps up to value of 0x429
                        landing_stun_target_duration=self.mem_reader.read_u8(
                            dynamic_player_address + 0x429
                        ),  # biped_start_landing(), looks like the target for 0x428, max of 30?
                        airborne_ticks=self.mem_reader.read_u8(
                            dynamic_player_address + 0x459
                        ),  # biped_flying_through_air(), seems to be number of ticks since leaving ground
                        # TODO: need to verify padding on these. crouchscale doesn't line up with the end of `short landing`
                        slipping_ticks=self.mem_reader.read_u8(
                            dynamic_player_address + 0x45A
                        ),
                        stop_ticks=self.mem_reader.read_u8(
                            dynamic_player_address + 0x45B
                        ),
                        jump_recovery_timer=self.mem_reader.read_u8(
                            dynamic_player_address + 0x45C
                        ),
                        melee_animation_remaining=self.mem_reader.read_u8(
                            dynamic_player_address + 0x45D
                        ),
                        melee_animation_damage_tick=self.mem_reader.read_u8(
                            dynamic_player_address + 0x45E
                        ),  # from biped_update() and unit_cause_player_melee_damage()
                        melee_impact_this_tick=self.mem_reader.read_u8(
                            dynamic_player_address + 0x45D
                        )
                        == self.mem_reader.read_u8(
                            dynamic_player_address + 0x45E
                        ),  # TODO: move to computed?
                        landing=self.mem_reader.read_u16(
                            dynamic_player_address + 0x45F
                        ),
                        air_3_0x460=self.mem_reader.read_s16(
                            dynamic_player_address + 0x460
                        ),  # biped_update(), if -1 check for slipping. stays -1 while walking, briefly 0 when landing, 1 if damaged from fall? stays at 0 or 1 until 0x428 reaches 0x429
                        # 0x4096 when shields are charging, 0x4112 when overshield charging
                        air_4_0xB6=self.mem_reader.read_s16(
                            dynamic_player_address + 0xB6
                        ),  # biped_flying_through_air() and unit_get_camera_position(), 8 while shields are damaged from falling or nade, 4096 while shields recharging (from any damage)
                        biped_flags=self.mem_reader.read_u32(biped_tag_address + 0x2F4),
                        autoaim_pill_radius=self.mem_reader.read_float(
                            biped_tag_address + 0x458
                        ),  # from biped_get_autoaim_pill()
                    )

                    model_nodes = self.get_model_nodes(dynamic_player_address)

                else:

                    if previous_player_object_handle != -1:
                        # body of dead player
                        model_nodes = self.get_model_nodes(
                            previous_dynamic_player_address
                        )
                    else:
                        model_nodes = []

                    player_object_data = {}
                    # print('player respawns in {} ticks'.format(self.mem_reader.read_u32(static_player_address + 0x2C)))

                # print(player_object_data['xaim2'], player_object_data['yaim2'], player_object_data['zaim2'])

                # TODO: game_engine_get_state_message()

                local_player = self.mem_reader.read_s16(static_player_address + 0x2)

                player_stats = dict(
                    player_index=player_index,  # index in the player datum array
                    local_player=local_player,  # 0 to 3 if local (controller port), -1 if not local
                    name=(
                        self.mem_reader.read_bytes(static_player_address + 0x4, 24)
                        .decode("utf-16")
                        .split("\x00", 1)[0]
                        if self.mem_reader.use_pymem
                        else b"".join(
                            [
                                int.to_bytes(i, signed=True)
                                for i in self.mem_reader.read_bytes(
                                    static_player_address + 0x4, 24
                                )
                            ]
                        )
                        .decode("utf-16")
                        .split("\x00", 1)[0]
                    ),
                    # is_dead=hex(self.mem_reader.read_s32(static_player_address + 0xD)),  # from any_player_is_dead() -- value does not change when dead
                    # name=t.read(static_player_address + 0x4, 24).decode('utf-16').split('\x00', 1)[0],
                    team=self.mem_reader.read_u32(
                        static_player_address + 0x20
                    ),  # red=0, blue=1, ffa=0-15
                    action_target=hex(
                        self.mem_reader.read_u32(static_player_address + 0x24)
                    ),  # looks like the object you'll interact with if you press action, set to -1 on spawn
                    action=self.mem_reader.read_u16(
                        static_player_address + 0x28
                    ),  # 6 if standing over weapon (7 if only 1 weapon held), 8 if next to vehicle, 0 otherwise, set to 0 on spawn
                    action_seat=self.mem_reader.read_u16(static_player_address + 0x2A),
                    respawn_timer=self.mem_reader.read_u32(
                        static_player_address + 0x2C
                    ),
                    respawn_penalty=self.mem_reader.read_u32(
                        static_player_address + 0x30
                    ),
                    object_ref=hex(
                        self.mem_reader.read_u32(static_player_address + 0x34)
                    ),  # -1 when player is dead
                    object_index=self.mem_reader.read_u16(static_player_address + 0x34),
                    object_id=self.mem_reader.read_u16(static_player_address + 0x36),
                    previous_object_ref=hex(
                        self.mem_reader.read_u32(static_player_address + 0x38)
                    ),  #  0x34 gets copied here when player dies
                    last_target_object_ref=hex(
                        self.mem_reader.read_u32(static_player_address + 0x40)
                    ),  # set to same as copy above if no target
                    time_of_last_shot=self.mem_reader.read_u32(
                        static_player_address + 0x44
                    ),
                    player_speed=self.mem_reader.read_float(
                        static_player_address + 0x6C
                    ),
                    camo_timer=self.mem_reader.read_u32(static_player_address + 0x68),
                    time_of_last_death=self.mem_reader.read_u32(
                        static_player_address + 0x84
                    ),  # 0 at start of game
                    target_player_index=self.mem_reader.read_u32(
                        static_player_address + 0x88
                    ),
                    kill_streak=self.mem_reader.read_u16(
                        static_player_address + 0x92
                    ),  # resets to 0 on death
                    multikill=self.mem_reader.read_u16(
                        static_player_address + 0x94
                    ),  # resets to 0 on death
                    time_of_last_kill=self.mem_reader.read_s16(
                        static_player_address + 0x96
                    ),  # in ticks, resets to -1 on death
                    kills=self.mem_reader.read_s16(static_player_address + 0x98),
                    assists=self.mem_reader.read_s16(static_player_address + 0xA0),
                    team_kills=self.mem_reader.read_s16(static_player_address + 0xA8),
                    deaths=self.mem_reader.read_s16(static_player_address + 0xAA),
                    suicides=self.mem_reader.read_s16(static_player_address + 0xAC),
                    shots_fired=self.mem_reader.read_s32(static_player_address + 0xAE),
                    shots_hit=self.mem_reader.read_s16(static_player_address + 0xB2),
                    score=self.player_score_by_player_id(
                        player_index,
                        (
                            self.mem_reader.read_u32(game_engine_globals_address + 0x4)
                            if game_engine_globals_address
                            else 0
                        ),
                    ),
                    ctf_score=self.mem_reader.read_s16(static_player_address + 0xC4),
                    player_quit=self.mem_reader.read_u8(
                        static_player_address + 0xD1
                    ),  # 1 if player quit, not sure what else
                    damage_table=damage_table,
                    observer_camera_info=self.get_observer_camera_info(
                        local_player
                    ),  # TODO: duplicate lookup
                    input_data=self.get_input_data(local_player, player_index),
                    player_object_debug=player_object_debug,
                    player_object_data=player_object_data,
                    model_nodes=model_nodes,  # also includes dead body while respawning
                )

                derived_stats = dict(
                    # has_camo=player_stats['camo_timer'] > 0,
                    has_camo=bool(player_object_data)
                    and player_object_data["camo"] == 0x51,
                    has_overshield=bool(player_object_data)
                    and (
                        player_object_data["shields_status"] == 0x10
                        or player_object_data["shields"] > 1  # type: ignore
                    ),  # FIXME: replace int conversion
                )
                player_stats.update(derived_stats=derived_stats)

                # get data that depends on players being local
                if local_player != -1:
                    # player_stats.update(input_data=get_input_data(local_player))
                    player_stats.update(
                        first_person_weapon=self.get_first_person_weapon(local_player)
                    )

                player_stat_array.append(player_stats)

        game_info = dict(
            process_id=f"{self.mem_reader.process_id} - {hex(self.mem_reader.process_id)}",
            # pgcr_debug=dict(
            #     arg_0_address=f'{self.mem_reader.read_u8(0x106536)} @ {0x106536:#x} -> {self.mem_reader.get_host_address(0x106536):#x}',
            #     arg_1_address=f'{self.mem_reader.read_u8(0x10653E)} @ {0x10653E:#x} -> {self.mem_reader.get_host_address(0x10653E):#x}',
            #     maybe_font_size=f'{self.mem_reader.read_u8(0x10721B + 1)} @ {0x10721B + 1:#x} -> {self.mem_reader.get_host_address(0x10721B + 1):#x}',
            #     color1=f'{hex(self.mem_reader.read_u32(0x106F3F + 4))} @ {0x106F3F + 4:#x} -> {self.mem_reader.get_host_address(0x106F3F + 4):#x}',
            #     color2=f'{hex(self.mem_reader.read_u32(0x106F51 + 4))} @ {0x106F51 + 4:#x} -> {self.mem_reader.get_host_address(0x106F51 + 4):#x}',
            #     color3=f'{hex(self.mem_reader.read_u32(0x106F5D + 4))} @ {0x106F5D + 4:#x} -> {self.mem_reader.get_host_address(0x106F5D + 4):#x}',
            #     color4=f'{hex(self.mem_reader.read_u32(0x106F69 + 4))} @ {0x106F69 + 4:#x} -> {self.mem_reader.get_host_address(0x106F69 + 4):#x}',
            #     color5=f'{hex(self.mem_reader.read_u32(0x106FFB + 4))} @ {0x106FFB + 4:#x} -> {self.mem_reader.get_host_address(0x106FFB + 4):#x}',
            #     color6=f'{hex(self.mem_reader.read_u32(0x106FFB + 12))} @ {0x106FFB + 12:#x} -> {self.mem_reader.get_host_address(0x106FFB + 12):#x}',
            #     color7=f'{hex(self.mem_reader.read_u32(0x106FFB + 20))} @ {0x106FFB + 20:#x} -> {self.mem_reader.get_host_address(0x106FFB + 20):#x}',
            # ),
            game_type=(
                self.mem_reader.read_u32(game_engine_globals_address + 0x4)
                if game_engine_globals_address
                else ""
            ),
            variant=self.mem_reader.read_u8(0x2F90F4),
            global_stage=self.mem_reader.read_string(
                0x2FAC20, length=63
            ),  # only populated for hostbox
            multiplayer_map_name=self.mem_reader.read_string(
                0x2E37CD
            ),  # populated for host and join boxes
            # network_game_server=f'{hex(self.mem_reader.read_u32(self.mem_reader.read_u32(0x2E3628)))}: {hex(self.mem_reader.read_u32(self.mem_reader.read_u32(0x2E3628)))} -> {hex(self.mem_reader.get_host_address(self.mem_reader.read_u32(self.mem_reader.read_u32(0x2E3628))))}',
            # network_game_server_state=self.mem_reader.read_s16(self.mem_reader.read_u32(0x2E3628) + 0x4),  # 1 = ingame
            # 2 = postgame
            # 0 = picking map?
            game_connection=self.mem_reader.read_s16(0x2E3684),
            # network_game_client=self.mem_reader.read_u8(self.mem_reader.read_u32(0x2E362C)),
            game_engine_has_teams=self.mem_reader.read_u8(0x2F90C4),
            game_engine_running=game_engine_globals_address
            != 0,  # true in game and postgame carnage report, false in pregame lobby
            game_engine_can_score=self.mem_reader.read_u32(0x2FABF0) == 0
            and game_engine_globals_address
            != 0,  # false as soon as you hear "game over"
            # TODO: only look up scores for current gametype
            # input_data=get_input_data(),
            flag_data=self.get_flag_data(),
            local_player_count=self.mem_reader.read_u16(
                self.players_globals_address + 0x24
            ),
            key_data=self.get_key_data(),
            # flag_base_locations=f'{self.mem_reader.read_float(0x2762A4)} {hex(self.mem_reader.known_addresses[0x2762A4]["host_address"])}',
            game_time_info=self.get_game_time_info(),
            # game_variant=get_game_variant_global(),
            network_game_server=self.get_network_game_server(),
            network_game_client=self.get_network_game_client(),
            # game_update_data=dump_game_update_contents(),
            # fog_data=get_fog(),
            observer_cameras_address=f"{self.mem_reader.get_host_address(0x271550):#x}",
            game_globals_address=f"{hex(self.game_globals_address)} -> {hex(self.mem_reader.get_host_address(self.game_globals_address))}",
            game_globals_map_loaded=game_globals_map_loaded,
            players_are_double_speed=self.mem_reader.read_u8(
                self.game_globals_address + 0x2
            ),
            game_loading_in_progress=self.mem_reader.read_u8(
                self.game_globals_address + 0x3
            ),
            precache_map_status=self.mem_reader.read_float(
                self.game_globals_address + 0x4
            ),
            game_difficulty_level=self.mem_reader.read_u8(
                self.game_globals_address + 0xE
            ),
            # FIXME: self.mem_reader.read_s32(global_game_globals_address + 372) doesn't seem to be valid on first tick?
            #        Error converting gpa 0x3e590bb7 to gva (got {'return': 'Unmapped\r\n'})
            # idle_time_debug_addr=hex(self.mem_reader.read_s32(global_game_globals_address + 372)),
            # idle_time_debug_addr2=hex(self.mem_reader.read_s32(global_game_globals_address + 372) + 156),
            # idle_time_lower_bound=self.mem_reader.read_float(self.mem_reader.read_s32(global_game_globals_address + 372) + 156),  # in seconds
            # idle_time_upper_bound=self.mem_reader.read_float(self.mem_reader.read_s32(global_game_globals_address + 372) + 160),  # in seconds
            # idle_time_skip_fraction=self.mem_reader.read_float(self.mem_reader.read_s32(global_game_globals_address + 372) + 164),
            # stun_movement_penalty=self.mem_reader.read_float(self.mem_reader.read_s32(global_game_globals_address + 372) + 128),
            # stun_jumping_penalty=self.mem_reader.read_float(self.mem_reader.read_s32(global_game_globals_address + 372) + 132),
            game_globals_active=game_globals_active,
            global_random_seed=hex(self.mem_reader.read_u32(0x2E3648)),
            stored_global_random=hex(
                self.mem_reader.read_u32(self.game_globals_address + 16)
            ),  # gets set to 0xdeadbeef during pregame/mapselect
            main_menu_is_active=main_menu_is_active,
            last_game_in_progress=last_game_in_progress,
            last_game_connection=self.last_game_connection,
            memory_info=self.get_memory_info(),
            events=[],
            damage_counts=damage_counts,
            players=player_stat_array,
            # TODO: try asyncio or multiprocessing for large blobs like this
            #       (note: tried asyncio.run/await/async and it ran half as fast)
            # TODO: see https://github.com/StarrFox/wizwalker for possible implementation
            #       make the individual pymem calls async?
            objects=self.get_objects(),
            items=self.get_items(),
            spawns=self.get_spawns(),
            game_ended_this_tick=False,  # this gets set in extract_events()
            current_time=datetime.datetime.now(),
        )

        current_time = game_info["current_time"]
        elapsed_time = game_info["game_time_info"]["game_time"] + 1  # type: ignore
        start_time = current_time - datetime.timedelta(seconds=elapsed_time / 30)  # type: ignore
        game_info.update(
            dict(
                start_time=start_time,
                objects_meta=self.arrange_objects_by_type(game_info["objects"]),
            )
        )

        if (
            "start_time" not in self.game_meta or self.game_meta["start_time"] is None
        ) and game_info["game_engine_can_score"]:
            self.game_meta["start_time"] = start_time

        # FIXME: need a better game id, start_game can shift if the game runs slowly
        if game_info["game_engine_can_score"]:
            game_id = f'{self.game_meta["start_time"].strftime("%Y-%m-%d_%H-%M-%S")}'
        else:
            game_id = ""
        game_info["game_id"] = game_id

        return game_info

    def send_to_file(self, data, outfile, compression=""):
        # Create directories if they don't exist
        os.makedirs(os.path.dirname(outfile), exist_ok=True)

        # Serialize data to bytes
        data_bytes = json.dumps(data, default=str).encode()

        # Handle compression if specified
        if compression:
            with open(outfile, "wb") as f:
                if compression == "gz":
                    with gzip.open(f, "wb") as gz_file:
                        gz_file.write(data_bytes)
                elif compression == "lz":
                    with lzma.open(f, "wb") as lz_file:
                        lz_file.write(data_bytes)
                elif compression == "br":
                    f.write(brotli.compress(data_bytes, quality=6))
                elif compression == "zstd":
                    compressor = zstd.ZstdCompressor(level=11)
                    f.write(compressor.compress(data_bytes))
        else:
            # No compression; write as plain JSON
            with open(outfile, "a") as f:
                json.dump(data, f, default=str)
                f.write("\n")

    def handle_game_info_loop(self):
        """
        Continuous loop waiting for new ticks in game_info_queue.
        """
        game_ticks = []
        store_all_ticks = True

        while True:
            # Check queue size and print if it contains items
            if (queue_size := self.game_info_queue.qsize()) > 0:
                print(f"queue size: {queue_size}")

            game_info = self.game_info_queue.get()
            game_id = game_info.get("game_id")

            # If there's an active game, process it
            if game_id:
                # Remove large, repeated elements from game_info to avoid duplication
                events = game_info.pop("events", [])
                spawns = game_info.pop("spawns", [])
                items = game_info.pop("items", [])
                meta = game_info.pop("game_meta", [])

                # Store all game ticks if enabled
                if store_all_ticks:
                    game_ticks.append(game_info)

                # Prepare game summary if all ticks are stored
                game_summary = {}
                if store_all_ticks and game_ticks:
                    start_time = game_ticks[0]["current_time"]
                    end_time = game_ticks[-1]["current_time"]
                    start_game_time = game_ticks[0]["game_time_info"]["game_time"]
                    end_game_time = game_ticks[-1]["game_time_info"]["game_time"]

                    game_summary = {
                        "game_id": game_id,
                        "is_full_game": start_game_time == 0,
                        "recording_started": start_time,
                        "recording_ended": end_time,
                        "game_duration_ingame": str(
                            datetime.timedelta(seconds=end_game_time / 30)
                        ).split(".")[0],
                        "recording_duration": str(end_time - start_time).split(".")[0],
                        "ticks_elapsed": end_game_time - start_game_time + 1,
                        "ticks_recorded": len(game_ticks),
                        "ticks_dropped": end_game_time
                        - start_game_time
                        + 1
                        - len(game_ticks),
                    }

                # Create the game data dictionary
                game = {
                    "summary": game_summary,
                    "game_meta": meta,
                    "events": events,
                    "spawns": spawns,
                    "items": items,
                    "ticks": game_ticks,
                }

                # If the game has ended on this tick, process and save it
                if game_info.get("game_ended_this_tick"):
                    pprint(game_summary)

                    # Save the game data to a file (using gzip compression)
                    filename = f"{REPLAY_FILE_PATH}{game_id}_final.json.gz"
                    self.send_to_file(game, filename, compression="gz")

                    # Clear the stored ticks and run garbage collection
                    game_ticks.clear()
                    gc.collect()

    def matches_gametype(self, current_gametype: int, gametype_list: list[int]) -> bool:
        """
        Returns True if current_gametype matches any gametypes in gametype_list
            0: none
            1: ctf
            2: slayer
            3: oddball
            4: king
            5: race
            6: terminator
            7: stub
            12: all games
            13: all games except ctf
            14: all games except ctf and race
        :param current_gametype:
        :param gametype_list:
        :return:
        """
        for gametype in gametype_list:
            if (
                current_gametype == gametype
                or gametype == 12
                or (gametype == 13 and current_gametype != 1)
                or (gametype == 14 and current_gametype not in (1, 5))
            ):
                return True
        return False  # for type safety

    def distance(self, p1: tuple[int, int, int], p2: tuple[int, int, int]) -> float:
        x1, y1, z1 = p1
        x2, y2, z2 = p2
        return (((x2 - x1) ** 2) + ((y2 - y1) ** 2) + ((z2 - z1) ** 2)) ** (1 / 2)

    def get_empty_player_meta(self):

        return dict(
            shots_by_weapon=defaultdict(int),
            damage_to_player=defaultdict(int),
            damage_from_player=defaultdict(int),
            kills_by_player=defaultdict(int),
            deaths_by_player=defaultdict(int),
            shots_by_tick=defaultdict(int),
            kills_by_tick=defaultdict(int),
            deaths_by_tick=defaultdict(int),
            assists_by_tick=defaultdict(int),
            damage_dealt_by_tick=defaultdict(int),
            damage_dealt=0,
            damage_received_by_tick=defaultdict(int),
            damage_received=0,
            camo_by_tick=defaultdict(int),
            camo_count=0,
            overshield_by_tick=defaultdict(int),
            overshield_count=0,
            active_projectiles=[],
        )

    def initialize_meta_players(self, game_info):

        # TODO: time spent blocking ports, movement traveled, times ported

        # TODO: separate counters (value at current tick) and timelines (all historical values by tick)

        self.game_meta["players"] = {}

        for player in game_info["players"]:
            self.game_meta["players"][
                player["player_index"]
            ] = self.get_empty_player_meta()

    def extract_events(self, old_game_info: dict, new_game_info: dict) -> list:
        events = []
        game_time = new_game_info["game_time_info"]["game_time"]

        if "players" not in self.game_meta:
            self.initialize_meta_players(new_game_info)

        # Handle new game initialization
        if (
            not old_game_info["game_engine_running"]
            and new_game_info["game_engine_running"]
        ):
            events.append(
                f'{game_time}: New game started on {new_game_info["multiplayer_map_name"]}'
            )
            self.game_meta["start_time"] = new_game_info["current_time"]
            self.initialize_meta_players(new_game_info)
            self.clear_caches()

        # Projectiles
        if new_game_info["game_engine_can_score"] and "objects" in new_game_info:
            new_projectiles = set(
                new_game_info["objects_meta"]["object_ids_by_type"]["projectile"]
            )
            old_projectiles = set(
                old_game_info["objects_meta"]["object_ids_by_type"]["projectile"]
            )
            new_projectile_ids_by_player = new_projectiles - old_projectiles
            deleted_projectile_ids_by_player = old_projectiles - new_projectiles
            # Further processing can be added here if needed

        # Shots fired, melees, and grenades
        if new_game_info["game_engine_can_score"] and len(
            old_game_info["players"]
        ) == len(new_game_info["players"]):
            for old_player, new_player in zip(
                old_game_info["players"], new_game_info["players"]
            ):
                old_data = old_player["player_object_data"]
                new_data = new_player["player_object_data"]

                if old_data and new_data:
                    # Weapons usage
                    for old_weapon, new_weapon in zip(
                        old_data["weapons"], new_data["weapons"]
                    ):
                        if old_weapon["object_id"] == new_weapon["object_id"]:
                            old_ammo = (
                                old_weapon["charge_amount"]
                                if new_weapon["is_energy_weapon"]
                                else old_weapon["magazine_ammo_count"]
                            )
                            new_ammo = (
                                new_weapon["charge_amount"]
                                if new_weapon["is_energy_weapon"]
                                else new_weapon["magazine_ammo_count"]
                            )
                            if old_ammo > new_ammo:
                                self.game_meta["players"][new_player["player_index"]][
                                    "shots_by_weapon"
                                ][new_weapon["tag_name"]] += (
                                    1
                                    if new_weapon["is_energy_weapon"]
                                    else old_ammo - new_ammo
                                )
                                self.game_meta["players"][new_player["player_index"]][
                                    "shots_by_tick"
                                ][game_time] += (
                                    1
                                    if new_weapon["is_energy_weapon"]
                                    else old_ammo - new_ammo
                                )

                    # Grenade throws
                    if old_data["primary_nades"] > new_data["primary_nades"]:
                        events.append(
                            f'{game_time}: {new_player["name"]} threw frag grenade ({old_data["primary_nades"]} -> {new_data["primary_nades"]})'
                        )
                    if old_data["secondary_nades"] > new_data["secondary_nades"]:
                        events.append(
                            f'{game_time}: {new_player["name"]} threw plasma grenade ({old_data["secondary_nades"]} -> {new_data["secondary_nades"]})'
                        )

                    # Melees
                    if (
                        not old_data["melee_impact_this_tick"]
                        and new_data["melee_impact_this_tick"]
                    ):
                        # Melee event can be logged here
                        pass

        # New damage
        if new_game_info["game_engine_can_score"]:
            for damage_dealer, damage_receivers in new_game_info[
                "damage_counts"
            ].items():
                damage_dealer_name = new_game_info["players"][damage_dealer]["name"]
                old_damage_receivers = old_game_info["damage_counts"].get(
                    damage_dealer, {}
                )

                for damage_receiver, new_amount in damage_receivers.items():
                    old_amount = old_damage_receivers.get(damage_receiver, 0)
                    if new_amount > old_amount:
                        damage_receiver_name = new_game_info["players"][
                            damage_receiver
                        ]["name"]
                        damage_diff = new_amount - old_amount
                        events.append(
                            f"{game_time}: {damage_dealer_name} damaged {damage_receiver_name} for {damage_diff}"
                        )
                        self.game_meta["players"][damage_dealer][
                            "damage_dealt_by_tick"
                        ][game_time] += damage_diff
                        self.game_meta["players"][damage_receiver][
                            "damage_received_by_tick"
                        ][game_time] += damage_diff
                        self.game_meta["players"][damage_dealer]["damage_to_player"][
                            damage_receiver
                        ] += damage_diff
                        self.game_meta["players"][damage_receiver][
                            "damage_from_player"
                        ][damage_dealer] += damage_diff
                        self.game_meta["players"][damage_dealer][
                            "damage_dealt"
                        ] += damage_diff
                        self.game_meta["players"][damage_receiver][
                            "damage_received"
                        ] += damage_diff

        # Kills, deaths, assists, powerups
        if (
            old_game_info["game_engine_running"]
            and new_game_info["game_engine_running"]
            and len(old_game_info["players"]) == len(new_game_info["players"])
        ):
            for old_player, new_player in zip(
                old_game_info["players"], new_game_info["players"]
            ):
                player_index = new_player["player_index"]

                # Kills
                if (kills := new_player["kills"]) > old_player["kills"]:
                    events.append(
                        f'{game_time}: {new_player["name"]} got a kill ({kills})'
                    )
                    self.game_meta["players"][player_index]["kills_by_tick"][
                        game_time
                    ] += (kills - old_player["kills"])

                # Deaths
                if (deaths := new_player["deaths"]) > old_player["deaths"]:
                    events.append(f'{game_time}: {new_player["name"]} died ({deaths})')
                    self.game_meta["players"][player_index]["deaths_by_tick"][
                        game_time
                    ] += (deaths - old_player["deaths"])

                # Assists
                if (assists := new_player["assists"]) > old_player["assists"]:
                    events.append(
                        f'{game_time}: {new_player["name"]} got an assist ({assists})'
                    )
                    self.game_meta["players"][player_index]["assists_by_tick"][
                        game_time
                    ] += (assists - old_player["assists"])

                # Camo and Overshield
                self.handle_powerup_events(
                    events, game_time, old_player, new_player, player_index
                )

        # Spawns
        if (
            new_game_info["players"]
            and new_game_info["game_engine_can_score"]
            and "spawns" in new_game_info
            and new_game_info["spawns"]
        ):
            self.handle_player_spawns(events, game_time, old_game_info, new_game_info)

        # Game Over
        if (
            old_game_info["game_engine_can_score"]
            and not new_game_info["game_engine_can_score"]
        ):
            events.append(
                f'{game_time}: Game ended on {new_game_info["multiplayer_map_name"]}'
            )
            self.game_meta["start_time"] = None
            new_game_info["game_ended_this_tick"] = True
            new_game_info["game_id"] = old_game_info["game_id"]

        new_game_info["game_meta"] = self.game_meta
        return events

    def handle_powerup_events(
        self, events, game_time, old_player, new_player, player_index
    ):
        """Handles camo and overshield events."""
        if (
            new_player["derived_stats"]["has_camo"]
            and not old_player["derived_stats"]["has_camo"]
        ):
            events.append(f'{game_time}: {new_player["name"]} picked up camo')
            self.game_meta["players"][player_index]["camo_by_tick"][game_time] += 1
            self.game_meta["players"][player_index]["camo_count"] += 1
        if (
            not new_player["derived_stats"]["has_camo"]
            and old_player["derived_stats"]["has_camo"]
        ):
            events.append(f'{game_time}: {new_player["name"]} lost camo')

        if (
            new_player["derived_stats"]["has_overshield"]
            and not old_player["derived_stats"]["has_overshield"]
        ):
            events.append(f'{game_time}: {new_player["name"]} picked up overshield')
            self.game_meta["players"][player_index]["overshield_by_tick"][
                game_time
            ] += 1
            self.game_meta["players"][player_index]["overshield_count"] += 1
        if (
            not new_player["derived_stats"]["has_overshield"]
            and old_player["derived_stats"]["has_overshield"]
        ):
            events.append(f'{game_time}: {new_player["name"]} lost overshield')

    def handle_player_spawns(self, events, game_time, old_game_info, new_game_info):
        """Handles player spawns events."""
        for old_player, new_player in zip(
            (
                old_game_info["players"]
                if old_game_info["players"]
                else [None] * len(new_game_info["players"])
            ),
            new_game_info["players"],
        ):
            if not old_player or (
                not old_player["player_object_data"]
                and new_player["player_object_data"]
            ):
                player_x, player_y, player_z = (
                    new_player["player_object_data"]["x"],
                    new_player["player_object_data"]["y"],
                    new_player["player_object_data"]["z"],
                )
                spawn_found = False
                for spawn in new_game_info["spawns"]:
                    d = self.distance(
                        (player_x, player_y, player_z),
                        (spawn["x"], spawn["y"], spawn["z"]),
                    )
                    if (
                        self.matches_gametype(
                            new_game_info["game_type"], spawn["gametypes"]
                        )
                        and d <= 0.2
                    ):
                        events.append(
                            f'{game_time}: {new_player["name"]} spawned at spawn id {spawn["spawn_id"]}'
                        )
                        spawn_found = True
                        break
                if not spawn_found:
                    events.append(
                        f'{game_time}: {new_player["name"]} spawned at an unknown spawn id ({player_x}, {player_y}, {player_z})'
                    )

    def process_write_queue(self):

        # TODO: keep a log of all modifications this session?
        while self.write_queue_from_ui.qsize() > 0:

            write_data = self.write_queue_from_ui.get(block=False)
            print(f"about to write: {write_data}")
            address = int(write_data["address"], 0)
            length = int(write_data["length"], 0)
            value = int(write_data["value"], 0).to_bytes(
                byteorder="little", length=length
            )

            print("before:", self.mem_reader.get_formatted_bytes(0x9C514, 2))
            self.mem_reader.write_bytes(address, value, length)
            print("after: ", self.mem_reader.get_formatted_bytes(0x9C514, 2))
