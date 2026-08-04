"""
Сервер Ascension.
Запускается отдельно от клиента: python server.py
По умолчанию слушает ws://localhost:8765

Данные о зарегистрированных пользователях, дружбе и каналах
сохраняются в файле data.json рядом с сервером — чтобы не терять
всё при перезапуске.

Протокол — JSON поверх WebSocket:

  Клиент -> сервер:
    {"type": "login", "username": "vasya"}
    {"type": "add_friend", "username": "petya"}
    {"type": "message", "to": "petya", "text": "привет"}
    {"type": "create_channel", "channel": "news"}
    {"type": "join_channel", "channel": "news"}
    {"type": "channel_message", "channel": "news", "text": "привет всем"}

  Сервер -> клиент:
    {"type": "friend_list", "friends": ["petya", "kolya"]}
    {"type": "online_users", "users": ["vasya", "petya"]}
    {"type": "channel_list", "channels": ["news", "memes"]}
    {"type": "message", "from": "vasya", "text": "привет"}
    {"type": "channel_message", "channel": "news", "from": "vasya", "text": "привет всем"}
    {"type": "error", "text": "..."}
"""

import asyncio
import json
import os
import websockets

DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.json")

# username -> websocket-соединение (только для тех, кто сейчас онлайн)
connected_clients = {}

# Постоянные данные — переживают перезапуск сервера
# known_users: множество всех, кто хоть раз логинился
# friendships: {username: [список друзей]}
# channels: {название_канала: {"owner": username, "members": [список участников]}}
data = {
    "known_users": [],
    "friendships": {},
    "channels": {},
}


def load_data():
    global data
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)


def save_data():
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


async def send_to(username, payload: dict):
    """Отправляет сообщение конкретному пользователю, если он сейчас онлайн."""
    ws = connected_clients.get(username)
    if ws is not None:
        try:
            await ws.send(json.dumps(payload))
        except websockets.exceptions.ConnectionClosed:
            pass


async def broadcast_online_users():
    payload = {"type": "online_users", "users": list(connected_clients.keys())}
    for username in list(connected_clients.keys()):
        await send_to(username, payload)


async def send_friend_list(username):
    friends = data["friendships"].get(username, [])
    await send_to(username, {"type": "friend_list", "friends": friends})


async def send_channel_list(username):
    my_channels = [
        name for name, info in data["channels"].items()
        if username in info["members"]
    ]
    await send_to(username, {"type": "channel_list", "channels": my_channels})


async def handler(websocket):
    username = None
    try:
        async for raw_message in websocket:
            try:
                msg = json.loads(raw_message)
            except json.JSONDecodeError:
                continue

            msg_type = msg.get("type")

            # ---------- Логин ----------
            if msg_type == "login":
                candidate = (msg.get("username") or "").strip()
                if not candidate:
                    await websocket.send(json.dumps({"type": "error", "text": "Empty username"}))
                    continue
                if candidate in connected_clients:
                    await websocket.send(json.dumps({"type": "error", "text": "Username already taken"}))
                    continue

                username = candidate
                connected_clients[username] = websocket

                if username not in data["known_users"]:
                    data["known_users"].append(username)
                    data["friendships"].setdefault(username, [])
                    save_data()

                await send_friend_list(username)
                await send_channel_list(username)
                await broadcast_online_users()

            # ---------- Добавление друга по нику ----------
            elif msg_type == "add_friend":
                if username is None:
                    continue
                target = (msg.get("username") or "").strip()

                if target == username:
                    await websocket.send(json.dumps({"type": "error", "text": "Can't add yourself"}))
                    continue
                if target not in data["known_users"]:
                    await websocket.send(json.dumps({"type": "error", "text": f"User '{target}' not found"}))
                    continue

                my_friends = data["friendships"].setdefault(username, [])
                their_friends = data["friendships"].setdefault(target, [])

                if target in my_friends:
                    await websocket.send(json.dumps({"type": "error", "text": "Already friends"}))
                    continue

                my_friends.append(target)
                their_friends.append(username)
                save_data()

                await send_friend_list(username)
                await send_friend_list(target)  # обновится, если target сейчас онлайн

            # ---------- Личное сообщение ----------
            elif msg_type == "message":
                if username is None:
                    continue
                recipient = msg.get("to")
                text = msg.get("text", "")
                await send_to(recipient, {"type": "message", "from": username, "text": text})

            # ---------- Создание канала ----------
            elif msg_type == "create_channel":
                if username is None:
                    continue
                channel_name = (msg.get("channel") or "").strip()
                if not channel_name:
                    await websocket.send(json.dumps({"type": "error", "text": "Empty channel name"}))
                    continue
                if channel_name in data["channels"]:
                    await websocket.send(json.dumps({"type": "error", "text": "Channel already exists"}))
                    continue

                data["channels"][channel_name] = {"owner": username, "members": [username]}
                save_data()
                await send_channel_list(username)

            # ---------- Вступление в канал ----------
            elif msg_type == "join_channel":
                if username is None:
                    continue
                channel_name = (msg.get("channel") or "").strip()
                channel = data["channels"].get(channel_name)
                if channel is None:
                    await websocket.send(json.dumps({"type": "error", "text": "Channel not found"}))
                    continue
                if username not in channel["members"]:
                    channel["members"].append(username)
                    save_data()
                await send_channel_list(username)

            # ---------- Сообщение в канал ----------
            elif msg_type == "channel_message":
                if username is None:
                    continue
                channel_name = msg.get("channel")
                text = msg.get("text", "")
                channel = data["channels"].get(channel_name)
                if channel is None or username not in channel["members"]:
                    continue
                for member in channel["members"]:
                    await send_to(member, {
                        "type": "channel_message",
                        "channel": channel_name,
                        "from": username,
                        "text": text,
                    })

    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        if username and connected_clients.get(username) is websocket:
            del connected_clients[username]
            await broadcast_online_users()


async def main():
    load_data()
    async with websockets.serve(handler, "localhost", 8765):
        print("Ascension server running on ws://localhost:8765")
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
