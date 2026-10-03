import os
import json
import math
import uuid
import asyncio
from dataclasses import dataclass, field
from typing import Dict, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI()

WORLD_W = 3600.0
WORLD_H = 2200.0
PLAYER_RADIUS = 24.0
PLAYER_SPEED = 430.0
MAX_HP = 100
BULLET_DAMAGE = 10
BULLET_SPEED = 1100.0
FIRE_INTERVAL = 0.15
RELOAD_TIME = 1.5
MAX_AMMO = 12
RESPAWN_TIME = 2.0
MATCH_TIME = 600.0
KILLS_TO_WIN = 15

# Rectangles: x, y, width, height
OBSTACLES = [
    (600, 300, 520, 90),
    (600, 1810, 520, 90),
    (2480, 300, 520, 90),
    (2480, 1810, 520, 90),
    (1540, 500, 520, 90),
    (1540, 1610, 520, 90),
    (900, 760, 180, 680),
    (2520, 760, 180, 680),
    (1710, 900, 180, 400),
]

SPAWNS = [(260, WORLD_H / 2), (WORLD_W - 260, WORLD_H / 2)]


@dataclass
class Player:
    pid: str
    slot: int
    x: float
    y: float
    angle: float = 0.0
    hp: int = MAX_HP
    ammo: int = MAX_AMMO
    kills: int = 0
    deaths: int = 0
    connected: bool = True
    dead_until: float = 0.0
    reload_until: float = 0.0
    last_shot: float = 0.0
    input_x: float = 0.0
    input_y: float = 0.0
    shooting: bool = False
    seq: int = 0


@dataclass
class Room:
    room_id: str
    players: Dict[str, Player] = field(default_factory=dict)
    sockets: Dict[str, WebSocket] = field(default_factory=dict)
    bullets: list = field(default_factory=list)
    started_at: float = 0.0
    match_over: bool = False


rooms: Dict[str, Room] = {}
lock = asyncio.Lock()


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def circle_rect_collision(cx, cy, r, rect):
    rx, ry, rw, rh = rect
    nx = clamp(cx, rx, rx + rw)
    ny = clamp(cy, ry, ry + rh)
    dx = cx - nx
    dy = cy - ny
    return dx * dx + dy * dy < r * r


def blocked(x, y):
    if x < PLAYER_RADIUS or x > WORLD_W - PLAYER_RADIUS:
        return True
    if y < PLAYER_RADIUS or y > WORLD_H - PLAYER_RADIUS:
        return True
    return any(circle_rect_collision(x, y, PLAYER_RADIUS, r) for r in OBSTACLES)


def move_player(p, dt):
    ix, iy = p.input_x, p.input_y
    mag = math.hypot(ix, iy)
    if mag > 1:
        ix, iy = ix / mag, iy / mag

    dx = ix * PLAYER_SPEED * dt
    dy = iy * PLAYER_SPEED * dt

    nx = p.x + dx
    if not blocked(nx, p.y):
        p.x = nx
    ny = p.y + dy
    if not blocked(p.x, ny):
        p.y = ny


def bullet_hits_obstacle(x, y):
    return any(circle_rect_collision(x, y, 4, r) for r in OBSTACLES)


def spawn_player(p):
    p.x, p.y = SPAWNS[p.slot]
    p.hp = MAX_HP
    p.ammo = MAX_AMMO
    p.dead_until = 0.0
    p.reload_until = 0.0
    p.shooting = False


def create_room():
    rid = uuid.uuid4().hex[:6].upper()
    rooms[rid] = Room(room_id=rid)
    return rooms[rid]


def room_snapshot(room, now):
    players = []
    for p in room.players.values():
        players.append({
            "id": p.pid,
            "slot": p.slot,
            "x": round(p.x, 2),
            "y": round(p.y, 2),
            "angle": round(p.angle, 4),
            "hp": p.hp,
            "ammo": p.ammo,
            "kills": p.kills,
            "deaths": p.deaths,
            "dead": p.dead_until > now,
            "reload": p.reload_until > now,
        })

    return {
        "type": "state",
        "world": {"w": WORLD_W, "h": WORLD_H},
        "players": players,
        "bullets": [
            {"x": round(b["x"], 1), "y": round(b["y"], 1)}
            for b in room.bullets
        ],
        "obstacles": OBSTACLES,
        "time_left": max(0.0, MATCH_TIME - (now - room.started_at)),
        "match_over": room.match_over,
    }


async def broadcast(room, message):
    dead = []
    for pid, ws in list(room.sockets.items()):
        try:
            await ws.send_text(json.dumps(message))
        except Exception:
            dead.append(pid)
    for pid in dead:
        room.sockets.pop(pid, None)


def shoot(room, p, now):
    if p.dead_until > now:
        return
    if p.reload_until > now:
        return
    if now - p.last_shot < FIRE_INTERVAL:
        return
    if p.ammo <= 0:
        p.reload_until = now + RELOAD_TIME
        return

    p.last_shot = now
    p.ammo -= 1

    room.bullets.append({
        "owner": p.pid,
        "x": p.x + math.cos(p.angle) * (PLAYER_RADIUS + 8),
        "y": p.y + math.sin(p.angle) * (PLAYER_RADIUS + 8),
        "vx": math.cos(p.angle) * BULLET_SPEED,
        "vy": math.sin(p.angle) * BULLET_SPEED,
        "born": now,
    })


def update_room(room, now, dt):
    if not room.started_at:
        room.started_at = now

    if room.match_over:
        return

    for p in room.players.values():
        if p.dead_until > 0 and p.dead_until <= now:
            spawn_player(p)
        if p.reload_until > 0 and p.reload_until <= now:
            p.reload_until = 0.0
            p.ammo = MAX_AMMO

        if p.dead_until <= now:
            move_player(p, dt)
            if p.shooting:
                shoot(room, p, now)

    new_bullets = []
    for b in room.bullets:
        b["x"] += b["vx"] * dt
        b["y"] += b["vy"] * dt

        if (
            b["x"] < 0 or b["x"] > WORLD_W or
            b["y"] < 0 or b["y"] > WORLD_H or
            bullet_hits_obstacle(b["x"], b["y"]) or
            now - b["born"] > 2.5
        ):
            continue

        hit = False
        for target in room.players.values():
            if target.pid == b["owner"] or target.dead_until > now:
                continue
            if math.hypot(target.x - b["x"], target.y - b["y"]) <= PLAYER_RADIUS + 5:
                target.hp -= BULLET_DAMAGE
                hit = True
                if target.hp <= 0:
                    killer = room.players.get(b["owner"])
                    target.deaths += 1
                    target.dead_until = now + RESPAWN_TIME
                    target.hp = 0
                    target.shooting = False
                    if killer:
                        killer.kills += 1
                        if killer.kills >= KILLS_TO_WIN:
                            room.match_over = True
                break

        if not hit:
            new_bullets.append(b)

    room.bullets = new_bullets

    if now - room.started_at >= MATCH_TIME:
        room.match_over = True


@app.get("/")
async def index():
    return FileResponse("static/index.html")


app.mount("/static", StaticFiles(directory="static"), name="static")


@app.post("/api/rooms")
async def create_room():
    async with lock:
        rid = uuid.uuid4().hex[:6].upper()
        while rid in rooms:
            rid = uuid.uuid4().hex[:6].upper()
        rooms[rid] = Room(room_id=rid)
    return {"room": rid, "url": f"/?room={rid}"}


@app.get("/health")
async def health():
    return {"ok": True, "rooms": len(rooms)}


@app.websocket("/ws/{room_id}")
async def websocket_endpoint(websocket: WebSocket, room_id: str):
    await websocket.accept()

    async with lock:
        room = rooms.get(room_id)
        if room is None:
            await websocket.send_text(json.dumps({
                "type": "error", "message": "ルームがありません。"
            }))
            await websocket.close()
            return

        if len(room.players) >= 2:
            await websocket.send_text(json.dumps({
                "type": "error", "message": "このルームは満員です。"
            }))
            await websocket.close()
            return

        slot = 0 if not room.players else 1
        pid = uuid.uuid4().hex
        p = Player(pid=pid, slot=slot, x=SPAWNS[slot][0], y=SPAWNS[slot][1])
        p.angle = 0.0 if slot == 0 else math.pi
        room.players[pid] = p
        room.sockets[pid] = websocket

    await websocket.send_text(json.dumps({
        "type": "welcome",
        "id": pid,
        "slot": slot,
        "room": room_id,
    }))

    if len(room.players) == 2:
        await broadcast(room, {"type": "match_start"})

    async def receive_loop():
        while True:
            raw = await websocket.receive_text()
            data = json.loads(raw)
            if data.get("type") != "input":
                continue

            p = room.players.get(pid)
            if not p:
                continue

            p.input_x = float(clamp(float(data.get("mx", 0)), -1, 1))
            p.input_y = float(clamp(float(data.get("my", 0)), -1, 1))
            p.angle = float(data.get("angle", p.angle))
            p.shooting = bool(data.get("shooting", False))
            p.seq = int(data.get("seq", p.seq))

            if data.get("reload"):
                now = asyncio.get_running_loop().time()
                if p.ammo < MAX_AMMO and p.reload_until <= now and p.dead_until <= now:
                    p.reload_until = now + RELOAD_TIME
                    p.shooting = False

    async def game_loop():
        last = asyncio.get_running_loop().time()
        while True:
            await asyncio.sleep(1 / 30)
            now = asyncio.get_running_loop().time()
            dt = min(0.05, now - last)
            last = now

            update_room(room, now, dt)

            if room.sockets:
                await broadcast(room, room_snapshot(room, now))

            if not room.sockets:
                break

    try:
        await asyncio.gather(receive_loop(), game_loop())
    except (WebSocketDisconnect, asyncio.CancelledError, Exception):
        pass
    finally:
        room.sockets.pop(pid, None)
        room.players.pop(pid, None)
        if not room.players:
            rooms.pop(room_id, None)
