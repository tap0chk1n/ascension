import sys
import os
import json
import asyncio

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QSplitter, QListWidget,
    QVBoxLayout, QHBoxLayout, QLabel, QTextEdit, QLineEdit,
    QPushButton, QComboBox, QInputDialog, QMessageBox, QTabWidget
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QIcon, QPixmap

import websockets

LOCALES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "locales")
ASSETS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
DEFAULT_LANG = "en"
SERVER_URL = "ws://localhost:8765"
APP_VERSION = "v001"


def load_translations(lang_code: str) -> dict:
    """Загружает словарь переводов. Отсутствующие ключи подставляются из английского."""
    fallback_path = os.path.join(LOCALES_DIR, f"{DEFAULT_LANG}.json")
    fallback = {}
    if os.path.exists(fallback_path):
        with open(fallback_path, "r", encoding="utf-8") as f:
            fallback = json.load(f)

    path = os.path.join(LOCALES_DIR, f"{lang_code}.json")
    if not os.path.exists(path):
        return fallback

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    merged = dict(fallback)
    merged.update(data)
    return merged


class NetworkClient(QThread):
    """Сетевой поток: собственный asyncio-цикл, общается с интерфейсом через сигналы."""

    message_received = pyqtSignal(str, str)              # (от кого, текст)
    channel_message_received = pyqtSignal(str, str, str)  # (канал, от кого, текст)
    friend_list_updated = pyqtSignal(list)
    online_users_updated = pyqtSignal(list)
    channel_list_updated = pyqtSignal(list)
    error_occurred = pyqtSignal(str)

    def __init__(self, username: str, server_url: str = SERVER_URL):
        super().__init__()
        self.username = username
        self.server_url = server_url
        self.loop = None
        self.outgoing_queue = None

    def run(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.outgoing_queue = asyncio.Queue()
        try:
            self.loop.run_until_complete(self._main())
        except Exception as e:
            self.error_occurred.emit(str(e))

    async def _main(self):
        async with websockets.connect(self.server_url) as ws:
            await ws.send(json.dumps({"type": "login", "username": self.username}))
            receiver_task = asyncio.create_task(self._receive_loop(ws))
            sender_task = asyncio.create_task(self._send_loop(ws))
            await asyncio.gather(receiver_task, sender_task)

    async def _receive_loop(self, ws):
        async for raw_message in ws:
            data = json.loads(raw_message)
            msg_type = data.get("type")

            if msg_type == "message":
                self.message_received.emit(data.get("from", "?"), data.get("text", ""))
            elif msg_type == "channel_message":
                self.channel_message_received.emit(
                    data.get("channel", "?"), data.get("from", "?"), data.get("text", "")
                )
            elif msg_type == "friend_list":
                self.friend_list_updated.emit(data.get("friends", []))
            elif msg_type == "online_users":
                self.online_users_updated.emit(data.get("users", []))
            elif msg_type == "channel_list":
                self.channel_list_updated.emit(data.get("channels", []))
            elif msg_type == "error":
                self.error_occurred.emit(data.get("text", "Unknown error"))

    async def _send_loop(self, ws):
        while True:
            payload = await self.outgoing_queue.get()
            await ws.send(json.dumps(payload))

    def _enqueue(self, payload: dict):
        if self.loop and self.outgoing_queue:
            asyncio.run_coroutine_threadsafe(self.outgoing_queue.put(payload), self.loop)

    def send_message(self, to: str, text: str):
        self._enqueue({"type": "message", "to": to, "text": text})

    def add_friend(self, username: str):
        self._enqueue({"type": "add_friend", "username": username})

    def create_channel(self, channel: str):
        self._enqueue({"type": "create_channel", "channel": channel})

    def join_channel(self, channel: str):
        self._enqueue({"type": "join_channel", "channel": channel})

    def send_channel_message(self, channel: str, text: str):
        self._enqueue({"type": "channel_message", "channel": channel, "text": text})


class MainWindow(QMainWindow):
    LANGUAGES = [
        ("en", "English"),
        ("ru", "Русский"),
    ]

    def __init__(self, username: str):
        super().__init__()
        self.username = username
        self.current_lang = DEFAULT_LANG
        self.translations = load_translations(self.current_lang)

        # Текущий открытый чат: ("friend", "petya") или ("channel", "news")
        self.current_chat = None
        self.chat_histories = {}   # {("friend"/"channel", имя): [(отправитель, текст), ...]}
        self.online_users = set()

        self.setWindowTitle(f"Ascension — {username}")
        self.resize(950, 620)

        icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.ico")
        if os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))

        splitter = QSplitter(Qt.Orientation.Horizontal)

        # ---------- Левая панель ----------
        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)

        title_layout = QHBoxLayout()
        title_label = QLabel("Ascension")
        title_label.setStyleSheet("font-size: 22px; font-weight: bold;")
        alpha_label = QLabel("pre-alpha")
        alpha_label.setStyleSheet("font-size: 10px; color: gray;")
        title_layout.addWidget(title_label, alignment=Qt.AlignmentFlag.AlignVCenter)
        title_layout.addWidget(alpha_label, alignment=Qt.AlignmentFlag.AlignTop)
        title_layout.addStretch()
        left_layout.addLayout(title_layout)

        self.lang_selector = QComboBox()
        for code, name in self.LANGUAGES:
            self.lang_selector.addItem(name, userData=code)
        self.lang_selector.currentIndexChanged.connect(self.on_language_changed)
        left_layout.addWidget(self.lang_selector)

        # Друзья + добавление по нику
        self.friends_label = QLabel()
        left_layout.addWidget(self.friends_label)

        add_friend_layout = QHBoxLayout()
        self.add_friend_input = QLineEdit()
        self.add_friend_button = QPushButton()
        add_friend_layout.addWidget(self.add_friend_input)
        add_friend_layout.addWidget(self.add_friend_button)
        left_layout.addLayout(add_friend_layout)

        self.friends_list = QListWidget()
        left_layout.addWidget(self.friends_list)

        # Каналы: создать / вступить
        self.channels_label = QLabel()
        left_layout.addWidget(self.channels_label)

        channel_buttons_layout = QHBoxLayout()
        self.create_channel_button = QPushButton()
        self.join_channel_button = QPushButton()
        channel_buttons_layout.addWidget(self.create_channel_button)
        channel_buttons_layout.addWidget(self.join_channel_button)
        left_layout.addLayout(channel_buttons_layout)

        self.channels_list = QListWidget()
        left_layout.addWidget(self.channels_list)

        # ---------- Правая панель ----------
        right_widget = QWidget()
        right_layout = QVBoxLayout(right_widget)

        self.chat_area = QTextEdit()
        self.chat_area.setReadOnly(True)
        right_layout.addWidget(self.chat_area)

        input_layout = QHBoxLayout()
        self.message_input = QLineEdit()
        self.send_button = QPushButton()
        input_layout.addWidget(self.message_input)
        input_layout.addWidget(self.send_button)
        right_layout.addLayout(input_layout)

        splitter.addWidget(left_widget)
        splitter.addWidget(right_widget)
        splitter.setSizes([280, 670])

        # ---------- Вкладка "Version" ----------
        version_widget = QWidget()
        version_layout = QVBoxLayout(version_widget)
        version_layout.addStretch()

        icon_path = os.path.join(ASSETS_DIR, "icon.png")
        if os.path.exists(icon_path):
            icon_label = QLabel()
            pixmap = QPixmap(icon_path).scaled(
                128, 128, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation
            )
            icon_label.setPixmap(pixmap)
            icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            version_layout.addWidget(icon_label)

        logo_path = os.path.join(ASSETS_DIR, "logo_text.png")
        if os.path.exists(logo_path):
            logo_label = QLabel()
            pixmap = QPixmap(logo_path).scaled(
                320, 320, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation
            )
            logo_label.setPixmap(pixmap)
            logo_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            version_layout.addWidget(logo_label)

        self.version_number_label = QLabel()
        self.version_number_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.version_number_label.setStyleSheet("font-size: 14px; margin-top: 12px;")
        version_layout.addWidget(self.version_number_label)

        self.copyright_label = QLabel()
        self.copyright_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.copyright_label.setStyleSheet("font-size: 11px; color: gray;")
        version_layout.addWidget(self.copyright_label)

        version_layout.addStretch()

        # ---------- Вкладки ----------
        self.tabs = QTabWidget()
        self.chat_tab_index = self.tabs.addTab(splitter, "Chat")
        self.version_tab_index = self.tabs.addTab(version_widget, "Version")
        self.setCentralWidget(self.tabs)

        # ---------- Связи ----------
        self.friends_list.itemClicked.connect(self.on_friend_selected)
        self.channels_list.itemClicked.connect(self.on_channel_selected)
        self.send_button.clicked.connect(self.send_message)
        self.message_input.returnPressed.connect(self.send_message)
        self.add_friend_button.clicked.connect(self.on_add_friend_clicked)
        self.add_friend_input.returnPressed.connect(self.on_add_friend_clicked)
        self.create_channel_button.clicked.connect(self.on_create_channel_clicked)
        self.join_channel_button.clicked.connect(self.on_join_channel_clicked)

        self.apply_translations()

        # ---------- Сеть ----------
        self.network_client = NetworkClient(self.username)
        self.network_client.message_received.connect(self.on_message_received)
        self.network_client.channel_message_received.connect(self.on_channel_message_received)
        self.network_client.friend_list_updated.connect(self.on_friend_list_updated)
        self.network_client.online_users_updated.connect(self.on_online_users_updated)
        self.network_client.channel_list_updated.connect(self.on_channel_list_updated)
        self.network_client.error_occurred.connect(self.on_network_error)
        self.network_client.start()

    # ---------- Переводы ----------
    def apply_translations(self):
        t = self.translations
        self.friends_label.setText(t.get("friends", "Friends"))
        self.channels_label.setText(t.get("channels", "Channels"))
        self.send_button.setText(t.get("send", "Send"))
        self.add_friend_input.setPlaceholderText(t.get("add_friend_placeholder", "Username..."))
        self.add_friend_button.setText(t.get("add", "Add"))
        self.create_channel_button.setText(t.get("create_channel", "Create channel"))
        self.join_channel_button.setText(t.get("join_channel", "Join channel"))

        self.tabs.setTabText(self.chat_tab_index, t.get("chat_tab", "Chat"))
        self.tabs.setTabText(self.version_tab_index, t.get("version_tab", "Version"))
        version_prefix = t.get("version_prefix", "version")
        self.version_number_label.setText(f"{version_prefix} {APP_VERSION}")
        self.copyright_label.setText(t.get("copyright", "© danilatapochkin 2026"))

    def on_language_changed(self, index: int):
        lang_code = self.lang_selector.itemData(index)
        self.current_lang = lang_code
        self.translations = load_translations(lang_code)
        self.apply_translations()

    # ---------- Открытие чатов ----------
    def open_chat(self, kind: str, name: str):
        self.current_chat = (kind, name)
        template = self.translations.get("chat_opened", "--- Chat opened with {name} ---")
        self.chat_area.clear()
        self.chat_area.append(template.format(name=name))
        for sender, text in self.chat_histories.get((kind, name), []):
            self.chat_area.append(f"{sender}: {text}")

    def on_friend_selected(self, item):
        name = item.text().replace(" (online)", "")
        self.open_chat("friend", name)

    def on_channel_selected(self, item):
        self.open_chat("channel", item.text())

    # ---------- Отправка ----------
    def send_message(self):
        text = self.message_input.text().strip()
        if not text or self.current_chat is None:
            return

        kind, name = self.current_chat
        if kind == "friend":
            self.network_client.send_message(name, text)
        else:
            self.network_client.send_channel_message(name, text)

        prefix = self.translations.get("you_prefix", "You")
        self.chat_area.append(f"{prefix}: {text}")
        self.chat_histories.setdefault((kind, name), []).append((prefix, text))
        self.message_input.clear()

    # ---------- Добавление друга ----------
    def on_add_friend_clicked(self):
        username = self.add_friend_input.text().strip()
        if username:
            self.network_client.add_friend(username)
            self.add_friend_input.clear()

    # ---------- Каналы ----------
    def on_create_channel_clicked(self):
        name, ok = QInputDialog.getText(
            self, self.translations.get("create_channel", "Create channel"),
            self.translations.get("channel_name_prompt", "Channel name:")
        )
        if ok and name.strip():
            self.network_client.create_channel(name.strip())

    def on_join_channel_clicked(self):
        name, ok = QInputDialog.getText(
            self, self.translations.get("join_channel", "Join channel"),
            self.translations.get("channel_name_prompt", "Channel name:")
        )
        if ok and name.strip():
            self.network_client.join_channel(name.strip())

    # ---------- Обработчики сети ----------
    def on_message_received(self, sender: str, text: str):
        self.chat_histories.setdefault(("friend", sender), []).append((sender, text))
        if self.current_chat == ("friend", sender):
            self.chat_area.append(f"{sender}: {text}")

    def on_channel_message_received(self, channel: str, sender: str, text: str):
        self.chat_histories.setdefault(("channel", channel), []).append((sender, text))
        if self.current_chat == ("channel", channel):
            self.chat_area.append(f"{sender}: {text}")

    def on_friend_list_updated(self, friends: list):
        self.friends_list.clear()
        for name in friends:
            label = f"{name} (online)" if name in self.online_users else name
            self.friends_list.addItem(label)

    def on_online_users_updated(self, users: list):
        self.online_users = set(u for u in users if u != self.username)
        # Обновляем пометки "(online)" у уже отображённых друзей
        for i in range(self.friends_list.count()):
            item = self.friends_list.item(i)
            base_name = item.text().replace(" (online)", "")
            item.setText(f"{base_name} (online)" if base_name in self.online_users else base_name)

    def on_channel_list_updated(self, channels: list):
        self.channels_list.clear()
        self.channels_list.addItems(channels)

    def on_network_error(self, message: str):
        QMessageBox.warning(self, "Ascension", message)

    def closeEvent(self, event):
        self.network_client.terminate()
        event.accept()


def main():
    app = QApplication(sys.argv)

    username, ok = QInputDialog.getText(None, "Ascension", "Enter your username:")
    if not ok or not username.strip():
        sys.exit(0)

    window = MainWindow(username.strip())
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
