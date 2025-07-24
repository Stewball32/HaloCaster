import queue
import orjson
import dearpygui.dearpygui as dpg

# Global flags for window visibility
info_window_enabled = True
positions_window_enabled = True
performance_window_enabled = False
editor_window_enabled = False

# Global flags for plot series visibility
scatter_series_enabled = True
item_series_enabled = False
object_series_enabled = False

# WebSocket server settings


class Diff:
    """
    Represents a memory difference in the guest system, with address, value, and length.
    Address, value, and length can be given in hex (with prefix 0x) or in decimal.
    """

    def __init__(self, address, value, length):
        self.address = address
        self.value = value
        self.length = length

    @classmethod
    def from_diff_string(cls, s):
        """
        Constructs a Diff object from a single line of an IDA diff string.
        Converts file offsets to memory offsets.
        """
        address, _, value = s.strip().split()
        address = hex(int(address.removesuffix(":"), 16) + 0x10000)
        value = f"0x{value}"
        return cls(address, value, "1")

    def as_dict(self):
        return {"address": self.address, "value": self.value, "length": self.length}

    def __repr__(self):
        return f"<Diff: address:{self.address} value:{self.value} length:{self.length}>"


def handle_write_clicked(sender, app_data, user_data):
    write_queue_from_ui = user_data
    write_queue_from_ui.put(
        {
            "address": dpg.get_value("write_address"),
            "value": dpg.get_value("write_value"),
            "length": dpg.get_value("write_length"),
        }
    )

    # Reset inputs
    dpg.set_value("write_address", "")
    dpg.set_value("write_value", "")
    dpg.set_value("write_length", "")


def send_preset(diffs, write_queue):
    print("Sending diffs through queue")
    for diff in diffs:
        write_queue.put(diff.as_dict())


def handle_solobox_clicked(sender, app_data, user_data):
    """
    Changes memory in xemu to allow solo box start and ignore team checks.
    """
    diff_string = """
        # always_allow_start_game.dif
        0008C514: 32 B0
        0008C515: C0 01
        # startgame_ignore-teamcheck_ignore-endgameteams.dif
        0008C0D2: 01 00
        000F7DEA: 0F 90
        000F7DEB: 84 90
        000F7DEC: 92 90
        000F7DED: 01 90
        000F7DEE: 00 90
        000F7DEF: 00 90
    """

    diffs = [
        Diff.from_diff_string(s)
        for s in diff_string.splitlines()
        if s and ":" in s and not s.strip().startswith("#")
    ]
    send_preset(diffs, user_data)


def start_ui(game_info_queue_for_ui, write_queue_from_ui):
    dpg.create_context()
    dpg.create_viewport(title="Xemu Memory Watcher", width=1680, height=1050)

    # Setup windows with visibility flags
    if info_window_enabled:
        with dpg.window(label="info", tag="info"):
            dpg.add_input_text(tag="filter", label="Filter")
            dpg.add_input_text(
                tag="player_info", width=800, height=900, multiline=True, readonly=True
            )

    if positions_window_enabled:
        with dpg.window(label="positions", pos=(900, 0), tag="positions"):
            with dpg.theme(tag="plot_theme"):
                with dpg.theme_component(dpg.mvScatterSeries):
                    dpg.add_theme_style(
                        dpg.mvPlotStyleVar_Marker,
                        dpg.mvPlotMarker_Circle,
                        category=dpg.mvThemeCat_Plots,
                    )
                    dpg.add_theme_style(
                        dpg.mvPlotStyleVar_MarkerSize, 20, category=dpg.mvThemeCat_Plots
                    )

            with dpg.plot(label="positions", width=600, height=600):
                dpg.add_plot_axis(
                    dpg.mvXAxis,
                    label="x",
                    tag="x_axis",
                    no_gridlines=True,
                    no_tick_marks=True,
                )
                dpg.set_axis_limits(dpg.last_item(), -20, 20)
                dpg.add_plot_axis(
                    dpg.mvYAxis,
                    label="y",
                    tag="y_axis",
                    no_gridlines=True,
                    no_tick_marks=True,
                )
                dpg.set_axis_limits(dpg.last_item(), -20, 20)

                if scatter_series_enabled:
                    dpg.add_scatter_series([], [], parent="y_axis", tag="team_1_series")
                    dpg.add_scatter_series([], [], parent="y_axis", tag="team_0_series")

                dpg.bind_item_theme("team_1_series", "plot_theme")
                dpg.bind_item_theme("team_0_series", "plot_theme")

    # Keep other window definitions the same...

    dpg.setup_dearpygui()
    dpg.show_viewport()

    while dpg.is_dearpygui_running():
        try:
            game_info = game_info_queue_for_ui.get(block=False)
            player_info_string = orjson.dumps(
                game_info, option=orjson.OPT_INDENT_2 | orjson.OPT_NON_STR_KEYS
            ).decode()

            if filter_string := dpg.get_value("filter"):
                player_info_string = "\n".join(
                    [
                        line
                        for line in player_info_string.splitlines()
                        if filter_string in line
                    ]
                )
            dpg.set_value("player_info", value=player_info_string)

            # Update positions and other UI elements...

        except queue.Empty:
            pass

        dpg.render_dearpygui_frame()

    dpg.destroy_context()


if __name__ == "__main__":
    game_info_queue = queue.Queue()
    write_queue = queue.Queue()

    start_ui(game_info_queue, write_queue)
