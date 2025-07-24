import queue
import orjson
import asyncio
import websockets
import threading
import re

WEBSOCKET_HOST = "localhost"
WEBSOCKET_PORT = 9000


async def websocket_server(websocket, path, game_info_queue_for_ui):
    series_score = {"red": 0, "blue": 0}
    previous_players_signature = None

    def get_player_signature(players):
        sorted_players = sorted(players, key=lambda x: x["name"])
        signature_parts = [f"{p['name']}:{p['team']}" for p in sorted_players]
        return ",".join(signature_parts)

    while True:
        try:
            game_info = game_info_queue_for_ui.get(block=False)
            current_players = game_info.get("players", [])
            current_signature = get_player_signature(current_players)
            events = game_info.get("events", [])

            # Process game events
            game_ended = any("game ended" in e.lower() for e in events)
            game_started = any("game started" in e.lower() for e in events)

            if game_ended:
                red_kills = sum(p["kills"] for p in current_players if p["team"] == 0)
                blue_kills = sum(p["kills"] for p in current_players if p["team"] == 1)
                if red_kills > blue_kills:
                    series_score["red"] += 1
                elif blue_kills > red_kills:
                    series_score["blue"] += 1

            if game_started:
                if (
                    previous_players_signature
                    and current_signature != previous_players_signature
                ):
                    series_score.update({"red": 0, "blue": 0})
                previous_players_signature = current_signature

            # Prepare data to send
            data = {
                "map_name": format_map_name(game_info.get("multiplayer_map_name")),
                "game_type": game_info.get("game_type", "Unknown Game Type"),
                "variant": game_info.get("variant", "Unknown Variant"),
                "real_time_elapsed": game_info.get("game_time_info", {}).get(
                    "real_time_elapsed", 0
                ),
                "events": events,
                "players": current_players,
                "red_team_kills": sum(
                    p["kills"] for p in current_players if p["team"] == 0
                ),
                "blue_team_kills": sum(
                    p["kills"] for p in current_players if p["team"] == 1
                ),
                "series_score": series_score,
            }
            await websocket.send(orjson.dumps(data).decode())
        except queue.Empty:
            await asyncio.sleep(0.1)


def start_websocket_server(game_info_queue_for_ui):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    start_server = websockets.serve(lambda ws, path: websocket_server(ws, path, game_info_queue_for_ui), WEBSOCKET_HOST, WEBSOCKET_PORT)  # type: ignore
    loop.run_until_complete(start_server)
    loop.run_forever()


def format_map_name(map_name):
    if not map_name:
        return "Unknown Map"
    parts = map_name.split("\\")
    for i in range(len(parts) - 2):
        if parts[i].lower() == "levels" and parts[i + 1].lower() == "test":
            name_part = parts[i + 2]
            formatted = "".join(
                [
                    word.capitalize()
                    for word in re.sub(r"[^a-zA-Z0-9]", " ", name_part).split()
                ]
            )
            return formatted
    last_part = parts[-1]
    formatted = re.sub(r"[^a-zA-Z0-9]", " ", last_part)
    words = formatted.split()
    if not words:
        return "Unknown Map"
    camel_case = words[0].lower() + "".join(word.capitalize() for word in words[1:])
    return camel_case


if __name__ == "__main__":
    game_info_queue = queue.Queue()

    websocket_thread = threading.Thread(
        target=start_websocket_server, args=(game_info_queue,)
    )
    websocket_thread.daemon = True
    websocket_thread.start()
