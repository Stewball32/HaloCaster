"""
These are functions that I found but don't do anything.
I'm storing them here to clean up the files they were found in.
"""

"""
halocaster.py
"""


class GameState:
    """
    Primarily used for tracking game state changes which cannot be determined by looking at a single tick's data
    """

    players = []
    damage_table = []

    def _new_game(self):

        # create new players and reset stats
        pass


class hexdump:
    """
    https://gist.github.com/NeatMonster/c06c61ba4114a2b31418a364341c26c0
    """

    def __init__(self, buf, off=0):
        self.buf = buf
        self.off = off

    def __iter__(self):
        last_bs, last_line = None, None
        for i in range(0, len(self.buf), 16):
            bs = bytearray(self.buf[i : i + 16])
            line = "{:08x}  {:23}  {:23}  |{:16}|".format(
                self.off + i,
                " ".join(("{:02x}".format(x) for x in bs[:8])),
                " ".join(("{:02x}".format(x) for x in bs[8:])),
                "".join((chr(x) if 32 <= x < 127 else "." for x in bs)),
            )
            if bs == last_bs:
                line = "*"
            if bs != last_bs or line != last_line:
                yield line
            last_bs, last_line = bs, line
        yield "{:08x}".format(self.off + len(self.buf))

    def __str__(self):
        return "\n".join(self)

    def __repr__(self):
        return "\n".join(self)


object_type_datum_sizes = dict()


# TODO: need to list out some use cases here -- where would intentional collisions be useful, if we can just
#       search by parameters individually. My first thought was map variants (dammy vs. dammy pe)
def calculate_map_hash():
    """
    spawn locations and rotations
    item locations and rotations
    equipment locations and rotations
    portal locations and rotations
    map name
    map description
    some chosen tag data (e.g. spread values and other things that may have been changed in different versions)
    TODO: could also include a separate hash based only on locations as a way to suggest alternate map versions
            (also look at locality-sensitive hashing for this)
    TODO: define some kind of hash versioning (like borrowing the $1$deadbeef, $2$cafebabe format from pw hashes?)
    """

    pass


def calculate_match_hash():
    """
    map hash
    player hash
    game hash
    stored global random
    xbox names
    game start time? (see below)
    TODO: do we need two of these hashes? one with start time (xbox clock) and one without?
            The one without start time will be the same on each xbox, but will also be the same on map reruns
            The one with start time will be different on each xbox and different across map reruns
            Is there some additional match start time data that comes along with one of the map start packets from host?
    """

    pass


def calculate_game_hash():
    """
    game version strings
    overall xbe hash (or hash of some chosen regions of the xbe -- like map list?)
    """

    pass


def calculate_player_hash():
    """
    player names
    player sensitivities
    player control scheme
    player order (nonlocal ids)  <-- this should be excluded from individual hashes, and introduced in combined via the order of the individual hashes
    TODO: should this be individual player hashes or combined?
            probably individual hashes that get combined for the match hash
    """

    pass


def get_hud_message(message_index):

    return read_string(hud_messages_pointer + 0x460 * message_index)


def datum_size_from_object_type(object_type):

    object_type_definitions_array = 0x1FCB78
    type_def_addr = read_u32(object_type_definitions_array + 4 * object_type)
    datum_size = read_u16(type_def_addr + 8)
    # print(get_formatted_bytes(type_def_addr, 24))
    return datum_size


def vector_3d_from_euler_angles_2d(euler_x, euler_y):

    x = math.cos(euler_x) * math.cos(euler_y)
    y = math.sin(euler_x) * math.cos(euler_y)
    z = math.sin(euler_y)
    return x, y, z


def get_all_team_scores():

    return dict(
        ctf_team_score=(read_u32(0x2762B4), read_u32(0x2762B4 + 0x4)),
        ctf_score_limit=read_u32(0x2762BC),
        slayer_team_scores_address=f"{hex(get_host_address(0x276710))}",
        slayer_team_score=(
            read_u32(0x276710),
            read_u32(0x276710 + 0x4),
        ),  # TODO: this is an array of 16 scores for ffa
        #       individual player scores are 16*4 after this address, even in a team game
        slayer_score_limit=read_u32(0x2F90E8),
        oddball_team_score=(
            read_u32(0x27653C),
            read_u32(0x27653C + 0x4),
        ),  # TODO: is this an array of 16 scores for ffa?
        oddball_score_limit=read_u32(0x276538),
        king_team_score=(read_u32(0x2762D8), read_u32(0x2762D8 + 0x4)),
        race_team_score=(read_u32(0x2766C8), read_u32(0x2766C8 + 0x4)),
    )


def get_global_variant():

    global_variant_address = 0x2F90A8


def team_score_by_team_id(team_id, gametype):

    return read_s32(team_score_addresses_by_gametype[gametype] + 4 * team_id)


def analyze_offset_map():
    """
    Compare guest and host memory offsets to check for contiguous regions

    TODO: make sure guest addresses above 0x80000000 are always contiguous in host memory
    :return:
    """

    memory_map = []
    mismatches = []

    last_guest = 0
    last_host = 0

    for guest, value in sorted(known_addresses.items()):
        if "qmp" in value:
            host = value["host_address"]
            memory_map.append(
                [
                    hex(guest),
                    hex(host),
                    guest - last_guest,
                    host - last_host,
                    value["qmp_traceback"],
                ]
            )
            if guest - last_guest != host - last_host:
                mismatches.append(
                    [
                        hex(guest),
                        hex(host),
                        guest - last_guest,
                        host - last_host,
                        value["qmp_traceback"],
                    ]
                )
            last_guest = guest
            last_host = host

    print("============= MEMORY MAP =============")
    print("guest, host, guest diff, host diff")
    pprint(memory_map)
    print("============= MISMATCHES =============")
    print("guest, host, guest diff, host diff")
    pprint(mismatches)


# TODO: do something with this
def get_game_data():
    team_game_address = 0x2F90C4
    game_engine_address = 0x2F9110
    game_server_address = 0x2E3628
    game_client_address = 0x2E362C
    game_connection_word = 0x2E3684
    players_globals_address = 0x2FAD20
    team_data_address = 0x2FAD24


def send_to_database(game_info, db):

    for player in game_info["players"]:
        if dynamic := player["player_object_data"]:
            location = (dynamic["x"], dynamic["y"], dynamic["z"])
        else:
            location = None
        data = dict(
            time=game_info["current_time"],
            player=player["local_player"],
            tick=game_info["game_time"],
            location=location,
        )
        db.insert_player_data(data)


default_framerate_address = 0xBB648
refresh_rate_address = 0x1F8C98


def memory_benchmark():

    print("Starting memory benchmark")

    starting_address = 0x80000000
    iterations = 1000
    length = 1024

    for i in range(0, length, 4):
        read_u32(starting_address + i)

    ttt = datetime.datetime.now()
    for _ in range(iterations):
        for i in range(0, length, 4):
            read_u32(starting_address + i)
    test_one = datetime.datetime.now() - ttt
    print(
        f"   {iterations} iterations of {length} bytes in 4-byte chunks took {test_one} seconds"
    )

    ttt = datetime.datetime.now()
    for _ in range(iterations):
        read_bytes(starting_address, length)
    test_two = datetime.datetime.now() - ttt
    print(
        f"   {iterations} iterations of one {length} byte chunk ({sizeof_fmt(iterations*length)}) took {test_two} seconds ({test_one/test_two}x faster)"
    )


def sizeof_fmt(num, suffix="B"):
    for unit in ["", "Ki", "Mi", "Gi", "Ti", "Pi", "Ei", "Zi"]:
        if abs(num) < 1024.0:
            return f"{num:3.1f}{unit}{suffix}"
        num /= 1024.0
    return f"{num:.1f}Yi{suffix}"
