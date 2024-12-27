"""The aidot integration."""

from homeassistant.core import HomeAssistant
import aiohttp
import logging
from .login_data import LoginData

import base64
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives import hashes

from .login_const import APP_ID, PUBLIC_KEY_PEM
_LOGGER = logging.getLogger(__name__)

def rsa_password_encrypt(message: str):
    """Get password rsa encrypt."""   
    public_key = serialization.load_pem_public_key(
        PUBLIC_KEY_PEM, backend=default_backend()
    )

    encrypted = public_key.encrypt(
        message.encode("utf-8"),
        padding.PKCS1v15(),  
    )

    encrypted_base64 = base64.b64encode(encrypted).decode("utf-8")

    return encrypted_base64


class LoginControl:
    _instance = None  # singleton

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self) -> None:
        self.LoginData = LoginData()

    def change_country_code(self, selected_contry_obj: str):
        """Do change_country_code."""
        self.LoginData.baseUrl = (
            f"https://prod-{selected_contry_obj['region'].lower()}-api.arnoo.com/v17"
        )

    async def async_get_products(
        self, hass: HomeAssistant, token: str, product_ids: str
    ):
        """Get device list."""
        url = f"{self.LoginData.baseUrl}/products/{product_ids}"
        headers = {
            "Terminal": "app",
            "Token": token,
            "Appid": APP_ID,
        }

        session = hass.helpers.aiohttp_client.async_get_clientsession()

        try:
            async with session.get(url, headers=headers) as response:
                response.raise_for_status()
                response_data = await response.json()
                return response_data
        except aiohttp.ClientError as e:
            _LOGGER.info("async_get_products ClientError {e}")
            return None

    async def async_get_devices(self, hass: HomeAssistant, token: str, house_id: str):
        """Get device list."""

        url = f"{self.LoginData.baseUrl}/devices?houseId={house_id}"
        headers = {
            "Terminal": "app",
            "Token": token,
            "Appid": APP_ID,
        }

        session = hass.helpers.aiohttp_client.async_get_clientsession()

        try:
            async with session.get(url, headers=headers) as response:
                response.raise_for_status()
                response_data = await response.json()
                return response_data
        except aiohttp.ClientError as e:
            _LOGGER.info("async_get_devices ClientError {e}")
            return None

    async def async_get_houses(self, hass: HomeAssistant, token: str):
        """Get house list."""

        url = f"{self.LoginData.baseUrl}/houses"
        headers = {
            "Terminal": "app",
            "Token": token,
            "Appid": APP_ID,
        }

        session = hass.helpers.aiohttp_client.async_get_clientsession()

        try:
            async with session.get(url, headers=headers) as response:
                response.raise_for_status()
                response_data = await response.json()
                return response_data
        except aiohttp.ClientError as e:
            _LOGGER.info("async_get_houses ClientError {e}")
            return None

    async def async_post_login(self, hass: HomeAssistant, username: str, password: str):
        """Login the user input allows us to connect."""

        url = f"{self.LoginData.baseUrl}/users/loginWithFreeVerification"
        headers = {"Appid": APP_ID, "Terminal": "app"}
        data = {
            "countryKey": "region:UnitedStates",
            "username": username,
            "password": rsa_password_encrypt(password),
            "terminalId": "gvz3gjae10l4zii00t7y0",
            "webVersion": "0.5.0",
            "area": "Asia/Shanghai",
            "UTC": "UTC+8",
        }
        session = hass.helpers.aiohttp_client.async_get_clientsession()

        try:
            async with session.post(url, headers=headers, json=data) as response:
                response.raise_for_status()
                login_response = await response.json()
                return login_response
        except aiohttp.ClientError as e:
            _LOGGER.info("async_post_login ClientError {e}")
            return None

    async def async_get_all_login_info(
        self, hass: HomeAssistant, username: str, password: str
    ):
        """Get get all login info."""
        # login in
        login_response = await self.async_post_login(
            hass,
            username,
            password,
        )
        accessToken = login_response["accessToken"]

        # get houses
        default_house = await self.async_get_houses(hass, accessToken)

        # get device_list
        device_list = await self.async_get_devices(
            hass, accessToken, default_house["id"]
        )

        # get product_list
        productIds = ",".join([item["productId"] for item in device_list])
        product_list = await self.async_get_products(hass, accessToken, productIds)

        return (login_response, default_house, device_list, product_list)
