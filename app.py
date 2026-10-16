import os
import secrets
import string
import time
from threading import Lock

from flask import Flask, jsonify, render_template, request
from flask_socketio import SocketIO, emit, join_room, leave_room

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "change-this-for-deployment")
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

rooms = {}
rooms_lock = Lock()
DEMO_VIDEO = "https://interactive-examples.mdn.mozilla.net/media/cc0-videos/flower.mp4"

MOVIES = [
    {"id": "demo-1", "title": "Flower Demo", "category": "Nature", "year": 2024, "tags": ["calm", "demo"], "url": DEMO_VIDEO},
    {"id": "demo-2", "title": "Big Buck Bunny (sample)", "category": "Animation", "year": 2008, "tags": ["animation", "comedy"], "url": "https://storage.googleapis.com/gtv-videos-bucket/sample/BigBuckBunny.mp4"},
    {"id": "demo-3", "title": "Elephant Dream (sample)", "category": "Sci-Fi", "year": 2006, "tags": ["scifi", "short"], "url": "https://storage.googleapis.com/gtv-videos-bucket/sample/ElephantsDream.mp4"},
    {"id": "demo-4", "title": "For Bigger Escapes (sample)", "category": "Adventure", "year": 2013, "tags": ["adventure", "travel"], "url": "https://storage.googleapis.com/gtv-videos-bucket/sample/ForBiggerEscapes.mp4"},
    {"id": "demo-5", "title": "For Bigger Fun (sample)", "category": "Comedy", "year": 2011, "tags": ["comedy", "short"], "url": "https://storage.googleapis.com/gtv-videos-bucket/sample/ForBiggerFun.mp4"},
]
EMOJIS = {"😂", "❤️", "🔥", "👏", "😮", "😭"}


def timestamp_ms():
    return int(time.time() * 1000)


def new_code():
    alphabet = string.ascii_uppercase + string.digits
    while True:
        code = "".join(secrets.choice(alphabet) for _ in range(6))
        if code not in rooms:
            return code


def safe_name(value):
    return str(value or "Guest").strip()[:24] or "Guest"


def room_snapshot(room):
    return {
        "code": room["code"],
        "hostId": room["hostId"],
        "users": list(room["users"].values()),
        "playback": dict(room["playback"]),
        "playlist": list(room["playlist"]),
        "chat": room["chat"][-100:],
        "reactions": room["reactions"][-20:],
        "polls": room["polls"],
    }


def broadcast_state(room):
    socketio.emit("room:state", room_snapshot(room), to=room["code"])


def get_room(code):
    return rooms.get(str(code or "").strip().upper())


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/health")
def health():
    return jsonify({"ok": True, "rooms": len(rooms)})


@app.get("/api/movies")
def movies():
    return jsonify(MOVIES)


@socketio.on("room:create")
def create_room(data):
    name = safe_name((data or {}).get("name"))
    with rooms_lock:
        code = new_code()
        room = {
            "code": code,
            "hostId": request.sid,
            "users": {request.sid: {"id": request.sid, "name": name}},
            "playback": {
                "videoUrl": DEMO_VIDEO, "playing": False, "currentTime": 0,
                "updatedAt": timestamp_ms(), "revision": 0
            },
            "playlist": [], "chat": [], "reactions": [], "polls": [],
        }
        rooms[code] = room
    join_room(code)
    emit("room:joined", {"ok": True, "code": code, "state": room_snapshot(room)})
    broadcast_state(room)


@socketio.on("room:join")
def join_watch_room(data):
    data = data or {}
    code = str(data.get("code", "")).strip().upper()
    name = safe_name(data.get("name"))
    with rooms_lock:
        room = get_room(code)
        if not room:
            emit("room:error", {"error": "Room not found. Check the code or create a room."})
            return
        room["users"][request.sid] = {"id": request.sid, "name": name}
        snapshot = room_snapshot(room)
    join_room(code)
    emit("room:joined", {"ok": True, "code": code, "state": snapshot})
    socketio.emit("system:message", f"{name} joined the room", to=code, include_self=False)
    broadcast_state(room)


@socketio.on("playback:command")
def playback_command(data):
    data = data or {}
    room = get_room(data.get("code"))
    if not room or request.sid not in room["users"]:
        emit("app:error", "Join the room first.")
        return
    if request.sid != room["hostId"]:
        emit("app:error", "Only the host can control playback.")
        return
    action = data.get("action")
    if action not in {"play", "pause", "seek", "load"}:
        emit("app:error", "Invalid playback action.")
        return
    playback = room["playback"]
    if action == "load":
        url = str(data.get("videoUrl", ""))
        if not url.startswith(("http://", "https://")):
            emit("app:error", "Video URL must start with http:// or https://.")
            return
        playback["videoUrl"] = url[:2000]
        playback["currentTime"] = 0
        playback["playing"] = False
    else:
        try:
            playback["currentTime"] = max(0.0, float(data.get("currentTime", 0)))
        except (TypeError, ValueError):
            emit("app:error", "Invalid playback position.")
            return
        if action == "play":
            playback["playing"] = True
        elif action == "pause":
            playback["playing"] = False
    playback["updatedAt"] = timestamp_ms()
    playback["revision"] += 1
    update = {**playback, "action": action, "serverTime": timestamp_ms(), "clientSentAt": data.get("clientSentAt")}
    socketio.emit("playback:update", update, to=room["code"])
    broadcast_state(room)


@socketio.on("chat:send")
def chat_send(data):
    data = data or {}
    room = get_room(data.get("code"))
    if not room or request.sid not in room["users"]:
        return
    text = str(data.get("text", "")).strip()[:500]
    if not text:
        return
    user = room["users"][request.sid]
    message = {"id": secrets.token_hex(5), "userId": request.sid, "name": user["name"], "text": text, "at": timestamp_ms()}
    room["chat"].append(message)
    room["chat"] = room["chat"][-200:]
    socketio.emit("chat:new", message, to=room["code"])


@socketio.on("reaction:send")
def reaction_send(data):
    data = data or {}
    room = get_room(data.get("code"))
    if not room or request.sid not in room["users"]:
        return
    emoji = str(data.get("emoji", "❤️"))[:8]
    reaction = {"id": secrets.token_hex(4), "emoji": emoji, "name": room["users"][request.sid]["name"], "at": timestamp_ms()}
    room["reactions"].append(reaction)
    room["reactions"] = room["reactions"][-50:]
    socketio.emit("reaction:new", reaction, to=room["code"])


@socketio.on("playlist:add")
def playlist_add(data):
    data = data or {}
    room = get_room(data.get("code"))
    movie = data.get("movie") or {}
    if not room or request.sid not in room["users"] or not movie.get("url"):
        return
    if len(room["playlist"]) >= 50:
        emit("app:error", "Playlist limit reached (50 items).")
        return
    item = {**movie, "id": f"{movie.get('id', 'video')}-{secrets.token_hex(3)}",
            "addedBy": room["users"][request.sid]["name"], "addedAt": timestamp_ms()}
    room["playlist"].append(item)
    broadcast_state(room)


@socketio.on("playlist:remove")
def playlist_remove(data):
    data = data or {}
    room = get_room(data.get("code"))
    if not room or request.sid != room["hostId"]:
        return
    room["playlist"] = [item for item in room["playlist"] if item["id"] != data.get("id")]
    broadcast_state(room)


@socketio.on("poll:create")
def poll_create(data):
    data = data or {}
    room = get_room(data.get("code"))
    if not room or request.sid not in room["users"]:
        return
    question = str(data.get("question", "")).strip()[:160]
    options = data.get("options", [])
    if not isinstance(options, list):
        options = []
    options = [str(option).strip()[:80] for option in options if str(option).strip()][:5]
    if not question or len(options) < 2:
        emit("app:error", "Poll needs a question and at least two options.")
        return
    poll = {"id": secrets.token_hex(4), "question": question,
            "options": [{"id": str(i), "label": label, "votes": []} for i, label in enumerate(options)],
            "createdBy": room["users"][request.sid]["name"], "closed": False}
    room["polls"].append(poll)
    room["polls"] = room["polls"][-10:]
    broadcast_state(room)


@socketio.on("poll:vote")
def poll_vote(data):
    data = data or {}
    room = get_room(data.get("code"))
    if not room or request.sid not in room["users"]:
        return
    poll = next((p for p in room["polls"] if p["id"] == data.get("pollId") and not p["closed"]), None)
    if not poll:
        return
    option = next((o for o in poll["options"] if o["id"] == str(data.get("optionId"))), None)
    if not option:
        return
    for item in poll["options"]:
        item["votes"] = [voter for voter in item["votes"] if voter != request.sid]
    option["votes"].append(request.sid)
    broadcast_state(room)


@socketio.on("host:transfer")
def host_transfer(data):
    data = data or {}
    room = get_room(data.get("code"))
    target = data.get("targetId")
    if not room or request.sid != room["hostId"] or target not in room["users"]:
        emit("app:error", "Host transfer failed.")
        return
    room["hostId"] = target
    socketio.emit("system:message", f"{room['users'][target]['name']} is now the host", to=room["code"])
    broadcast_state(room)


@socketio.on("disconnect")
def on_disconnect():
    with rooms_lock:
        for code, room in list(rooms.items()):
            if request.sid not in room["users"]:
                continue
            name = room["users"][request.sid]["name"]
            del room["users"][request.sid]
            if room["hostId"] == request.sid:
                room["hostId"] = next(iter(room["users"]), None)
            socketio.emit("system:message", f"{name} disconnected", to=code)
            if not room["users"]:
                del rooms[code]
            else:
                broadcast_state(room)


if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True, allow_unsafe_werkzeug=True)
