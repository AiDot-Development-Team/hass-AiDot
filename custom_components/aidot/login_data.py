"""The aidot integration."""

from .login_const import BASE_URL

class LoginData:

    _instance = None  # singleton

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self) -> None:
        self.baseUrl = BASE_URL