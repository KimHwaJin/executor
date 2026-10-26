from enum import Enum

class GaiaChatType(str, Enum):
    DEFAULT = "default"
    TEMP = "temp"
    LIVE = "live"

class ChatMode(str, Enum):
    ACTIVE = "active"