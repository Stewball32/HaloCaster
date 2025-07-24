from dataclasses import dataclass, field
from typing import List, Optional, Dict


@dataclass
class Position:
    x: float
    y: float
    z: float


@dataclass
class Rotation:
    pitch: float
    yaw: float
    roll: float


@dataclass
class PlayerInfo:
    id: int
    name: str
    team: str
    is_alive: bool
    health: float
    shield: float
    position: Position
    rotation: Rotation
    vehicle_id: Optional[int] = None
    weapon_primary_id: Optional[int] = None
    weapon_secondary_id: Optional[int] = None
    kills: int = 0
    deaths: int = 0
    flag_captures: int = 0


@dataclass
class ObjectInfo:
    id: int
    type: str
    name: str
    position: Position
    rotation: Optional[Rotation] = None
    velocity: Optional[Position] = None
    health: Optional[float] = None
    occupants: List[Optional[int]] = field(default_factory=list)
    ammo_loaded: Optional[int] = None
    ammo_reserve: Optional[int] = None
    team: Optional[str] = None


@dataclass
class GameEvent:
    type: str
    time: Optional[float] = None
    killer_id: Optional[int] = None
    victim_id: Optional[int] = None
    player_id: Optional[int] = None
    weapon: Optional[str] = None
    team: Optional[str] = None
    flag_team: Optional[str] = None


@dataclass
class GameInfo:
    map_name: str
    game_type: str
    variant: Optional[str] = None
    time_limit: Optional[float] = None
    time_remaining: Optional[float] = None
    score_limit: Optional[int] = None
    red_score: int = 0
    blue_score: int = 0
    is_game_over: bool = False
    players: List[PlayerInfo] = field(default_factory=list)
    objects: List[ObjectInfo] = field(default_factory=list)
    events: List[GameEvent] = field(default_factory=list)

    def to_dict(self) -> Dict:
        """Convert to raw dict (for JSON or websocket)."""
        from dataclasses import asdict

        return asdict(self)
