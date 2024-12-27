import socket
import binascii
import threading
import json
import time
import logging

from .aes_utils import aes_encrypt,aes_decrypt
import asyncio

_LOGGER = logging.getLogger(__name__)

class BroadcastProtocol:

    def __init__(self,callback,user_id):
        self.aes_key = bytearray(32)
        key_string = "T54uednca587"
        key_bytes = key_string.encode()     
        self.aes_key[:len(key_bytes)] = key_bytes

        self._discoverCb = callback
        self.user_id = user_id

    def connection_made(self, transport):
        self.transport = transport
        sock = transport.get_extra_info("socket")
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self.discover_task = asyncio.create_task(self.do_discover())
    
    async def do_discover(self):
        while True:
            current_timestamp_milliseconds = int(time.time() * 1000)
            seq = str(current_timestamp_milliseconds + 1)[-9:]
            message = {
                "protocolVer":"2.0.0",
                "service":"device",
                "method":"devDiscoveryReq",
                "seq": seq,
                "srcAddr":f"0.{self.user_id}]",
                "tst":current_timestamp_milliseconds,
                "payload":{
                    "extends":{ },
                    "localCtrFlag":1,
                    "timestamp":str(current_timestamp_milliseconds)
                }
            }
            send_data = aes_encrypt(json.dumps(message).encode(),self.aes_key)
            self.transport.sendto(send_data, ('255.255.255.255', 6666))
            await asyncio.sleep(3)

    def datagram_received(self, data, addr):
        data_str = aes_decrypt(data,self.aes_key)
        data_json = json.loads(data_str)
        if("payload" in data_json):
            if("mac" in data_json["payload"]):
                devId = data_json["payload"]["devId"]
                if self._discoverCb:
                        self._discoverCb(devId,{"ipAddress" : addr[0]})

    def error_received(self, exc):
        _LOGGER.error(f"Error occurred: {exc}")

class Discover:

    async def broadcast_message(self,callback,user_id):
        transport, protocol = await asyncio.get_event_loop().create_datagram_endpoint(
            lambda: BroadcastProtocol(callback,user_id),
            local_addr=("0.0.0.0", 0),  
        )