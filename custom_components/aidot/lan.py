import socket
import struct
import binascii
import time
from datetime import datetime
import json
import threading
import colorsys
from time import sleep
import asyncio
import logging

from .aes_utils import aes_encrypt,aes_decrypt
_LOGGER = logging.getLogger(__name__)

class Lan(object):

    _is_on : bool = False
    _dimming = 0
    _rgdb : int
    _cct : int
    _login_uuid = 0
    _available : bool = False

    _connectAndLogin : bool = False
    _connecting = False
    _simpleVersion = ""
    _colorMode = ""

    @property
    def is_on(self) -> bool:
        return self._is_on

    @property
    def brightness(self) -> int:
        return self._dimming * 255 / 100

    @property
    def rgdb(self) -> int:
        return self._rgdb

    @property
    def cct(self) -> int:
        return self._cct

    @property
    def available(self) -> bool:
        return self._available

    @property
    def connectAndLogin(self) -> bool:
        return self._connectAndLogin

    @property
    def connecting(self) -> bool:
        return self._connecting

    @property
    def colorMode(self) -> str:
        return self._colorMode

    def __init__(self,device:dict,user_info:dict) -> None:
        self.ping_count = 0

        if "id" in user_info:
            self.user_id = user_info["id"]

        if "aesKey" in device :
            key_string = device["aesKey"][0]
            if key_string is not None:
                self.aes_key = bytearray(16)
                key_bytes = key_string.encode()
                self.aes_key[:len(key_bytes)] = key_bytes

        if "password" in device:
            self.password = device["password"]

        if "id" in device:
            self.device_id = device["id"]

        if "simpleVersion" in device:
            self._simpleVersion = device["simpleVersion"]

    async def connect(self,ipAddress):
        self.reader = self.writer = None
        self._connecting = True
        try:
            self.reader, self.writer = await asyncio.open_connection(ipAddress, 10000)
            sock: socket.socket = self.writer.get_extra_info("socket")
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.seq_num = 1
            await self.login()
            self._connectAndLogin = True
        except Exception as e:
            _LOGGER.error(f"Login failed for '{ipAddress}'. Exception {e}")
            self._connectAndLogin = False
        finally:
            self._connecting = False

    def setUpdateDeviceCb(self,callback):
        self._updateDeviceCb = callback

    def printfHex(self,packet):
        hex_representation = binascii.hexlify(packet).decode()

    def getSendPacket(self,message,msgtype):
        magic = struct.pack('>H', 0x1eed)
        _msgtype = struct.pack('>h', msgtype)

        if self.aes_key is not None:
            send_data = aes_encrypt(message,self.aes_key)
        else :
            send_data = message

        bodysize = struct.pack('>i', len(send_data))
        packet = magic + _msgtype + bodysize + send_data

        return packet

    async def login(self):
        login_seq = str(int(time.time() * 1000) + self._login_uuid)[-9:]
        self._login_uuid += 1
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")
        message = {
            "service":"device",
            "method":"loginReq",
            "seq":login_seq,
            "srcAddr":self.user_id,
            "deviceId":self.device_id,
            "payload":{
                "userId":self.user_id,
                "password":self.password,
                "timestamp":timestamp,
                "ascNumber":1
            }
        }
        self.writer.write(self.getSendPacket(json.dumps(message).encode(),1))
        await self.writer.drain()

        data = await self.reader.read(1024)
        data_len = len(data)
        if(data_len <= 0):
            return

        magic, msgtype, bodysize = struct.unpack('>HHI', data[:8])
        encrypted_data = data[8:]
        if self.aes_key is not None:
            decrypted_data = aes_decrypt(encrypted_data, self.aes_key)
        else :
            decrypted_data = encrypted_data

        json_data = json.loads(decrypted_data)

        self.ascNumber = json_data["payload"]["ascNumber"]
        self.ascNumber += 1

        self._available = True

        await self.sendAction({},"getDevAttrReq")

    async def recvData(self):
        while True:
            try :
                data = await self.reader.read(1024)
            except Exception as e:
                _LOGGER.error(f"recv data error {e}")
                await asyncio.sleep(3)
                continue
            data_len = len(data)
            if(data_len <= 0):
                break

            try:
                magic, msgtype, bodysize = struct.unpack('>HHI', data[:8])
                encrypted_data = data[8:]
                decrypted_data = aes_decrypt(encrypted_data, self.aes_key)

                json_data = json.loads(decrypted_data)
            except Exception as e:
                _LOGGER.error(f"recv json error : {e}")
                await asyncio.sleep(3)
                continue

            if "service" in json_data:
                if "test" == json_data["service"]:
                    self.ping_count = 0

            if "payload" in json_data:
                if "ascNumber" in json_data["payload"]:
                    self.ascNumber = json_data["payload"]["ascNumber"]
                if "attr" in json_data["payload"]:
                    if "OnOff" in json_data["payload"]["attr"]:
                        self._is_on = json_data["payload"]["attr"]["OnOff"]
                    if "Dimming" in json_data["payload"]["attr"]:
                        self._dimming = json_data["payload"]["attr"]["Dimming"]
                    if "RGBW" in json_data["payload"]["attr"]:
                        self._rgdb = json_data["payload"]["attr"]["RGBW"]
                        self._colorMode = "rgbw"
                    if "CCT" in json_data["payload"]["attr"]:
                        self._cct = json_data["payload"]["attr"]["CCT"]
                        self._colorMode = "cct"
                    if self._updateDeviceCb:
                        await self._updateDeviceCb()

    async def ping_task(self):
        while True:
            if await self.sendPingAction() == -1 :
                return
            await asyncio.sleep(10)

    def getOnOffAction(self,OnOff):
        self._is_on = OnOff
        return {"OnOff": self._is_on}

    def getDimingAction(self,brightness):
        self._dimming = int(brightness * 100 / 255)
        return {"Dimming": self._dimming}

    def getCCTAction(self,cct):
        self._cct = cct
        self._colorMode = "cct"
        return {"CCT": self._cct}

    def getRGBWAction(self,rgbw):
        self._rgdb = rgbw
        self._colorMode = "rgbw"
        return {"RGBW": rgbw}

    async def sendDevAttr(self,devAttr):
        await self.sendAction(devAttr,"setDevAttrReq")

    async def sendAction(self,attr,method):

        current_timestamp_milliseconds = int(time.time() * 1000)

        self.seq_num += 1

        seq = "ha93" + str(self.seq_num).zfill(5)

        if not self._is_on and not "OnOff" in attr:
            attr["OnOff"] = 1
            self._is_on = 1

        if self._simpleVersion is not None:
            action = {
                "method": method,
                "service": "device",
                "clientId": "ha-" + self.user_id,
                "srcAddr": "0." + self.user_id,
                "seq": "" + seq,
                "payload": {
                    "devId": self.device_id,
                    "parentId": self.device_id,
                    "userId": self.user_id,
                    "password": self.password,
                    "attr": attr,
                    "channel":"tcp",
                    "ascNumber":self.ascNumber,
                },
                "tst": current_timestamp_milliseconds,
                # "tid": "homeassistant",
                "deviceId": self.device_id,
            }
        else :
            action = {
                "method": method,
                "service": "device",
                "seq": "" + seq,
                "srcAddr": "0." + self.user_id,
                "payload": {
                    "attr": attr,
                    "ascNumber":self.ascNumber,
                },
                "tst": current_timestamp_milliseconds,
                # "tid": "homeassistant",
                "deviceId": self.device_id,
            }

        try:
            self.writer.write(self.getSendPacket(json.dumps(action).encode(),1))
            await self.writer.drain()
        except BrokenPipeError as e :
            _LOGGER.error(f"{self.device_id} send action error {e}")
        except Exception as e:
            _LOGGER.error(f"{self.device_id} send action error {e}")

    async def sendPingAction(self):
        ping = {
            "service": "test",
            "method": "pingreq",
            "seq": "123456",
            "srcAddr": "x.xxxxxxx",
            "payload": {}
        }
        try:
            if self.ping_count >= 2 :
                _LOGGER.error(f"Last ping did not return within 20 seconds. device id:{self.device_id}")
                await self.reset()
                return -1
            self.writer.write(self.getSendPacket(json.dumps(ping).encode(),2))
            await self.writer.drain()
            self.ping_count += 1
            return 1
        except Exception as e:
            _LOGGER.error(f"{self.device_id} ping error {e}")
            await self.reset()
            return -1

    async def reset(self):
        try:
            if self.writer:
                self.writer.close()
                await self.writer.wait_closed()
        except Exception as e:
            _LOGGER.error(f"{self.device_id} writer close error {e}")
        self._connectAndLogin = False
        self._available = False
        self.ping_count = 0
        if self._updateDeviceCb:
            await self._updateDeviceCb()
        
