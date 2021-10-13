import base64
import hashlib
import json
import time
import functools
import logging

from typing import List

import websocket
import requests
import requests.auth

from .common import AbstractRobot, RobotStates, BatteryStatus, PowerMode
from .util import do_http, CachedData

logger = logging.getLogger(__name__)

class CloudRobot(AbstractRobot, CachedData):
    
    def __init__(self, cloudclient, id):
        CachedData.__init__(self)
        
        self.cloudclient = cloudclient
        self.id          = id
    
    def _getinfo_inner(self):
        r = do_http("POST", self.cloudclient.apiurl + "/robots/AppUpdate", auth=self.cloudclient.httpauth, json={"RobotID": self.id, "Email": self.cloudclient.credentials["Email"], "AccountPassword": self.cloudclient.credentials["AccountPassword"]})
        return r.json()
        
    def getstatus(self):
        return RobotStates[self._getinfo()["RobotStatus"]]
        
    def getid(self) -> str():
        """Get the robot's id"""
        return self.id
    
    def getname(self) -> str:
        """Get the robot's name"""
        return self._getinfo()["RobotName"]
    
    def getfirmware(self) -> str:
        """Get robot's firmware version"""
        return self._getinfo()["FirmwareVersion"]
    
    def getbattery(self) -> str:
        """Get the current robot battery status"""
        return BatteryStatus[self._getinfo()["BatteryStatus"]]
    
    def isconnected(self) -> bool:
        return self._getinfo()["Connected"]
    
    def getlocalpw(self):
        return self._getinfo()["LocalRobotPassword"]

    def getpowermode(self):
        
        i = self._getinfo()
        
        powermode = i["PowerMode"] if "PowerMode" in i else None
        isecomode = i["EcoMode"] if "EcoMode" in i else None
        
        if powermode is not None:
            powermode = PowerMode(powermode)
        
        elif isecomode is not None:
            if isecomode:
                powermode = PowerMode.MEDIUM
            else:
                powermode = PowerMode.HIGH
        else:
            powermode = PowerMode.MEDIUM
            
        return powermode
    
    def startclean(self):
        return self._sendCleanCommand(1)
    
    def gohome(self):
        return self._sendCleanCommand(3)

    def pauseclean(self):
        return self._sendCleanCommand(4)
    
    def stopclean(self):
        return self._sendCleanCommand(5)

    def setpowermode(self, mode):
        
        i = self._getinfo()
        
        powermode = i["PowerMode"] if "PowerMode" in i else None
        isecomode = i["EcoMode"] if "EcoMode" in i else None
        
        if powermode is not None:
            self._sendCommand({"PowerMode": mode.value})
            
        elif isecomode is not None:
            if mode == PowerMode.MEDIUM:
                self._sendCommand({"EcoMode": True})
            elif mode == PowerMode.HIGH:
                self._sendCommand({"EcoMode": False})
            else:
                raise Exception("Robot does not support " + str(mode))
        else:
            raise Exception("Robot does not support setting powermode")
        
        return None
    
    def getCleaningSessions(self, nextptr=None):
        
        r = do_http("POST", self.cloudclient.apiurl + "/robots/CleanedAreas", auth=self.cloudclient.httpauth, json={
            "Next": nextptr,
            "Previous": None,
            "Limit": 50,
            "RobotID": self.id,
            "Email": self.cloudclient.credentials["Email"],
            "AccountPassword": self.cloudclient.credentials["AccountPassword"]
        })
        
        if r.status_code == 204:
            return []
        
        js = r.json()
        
        items = list(map(
            lambda x: {
                "timestamp": x["TimeStamp"],
                "cleaned_area": x["CleanedArea"],
                "image": "https://mobile.rvccloud.electrolux.com/image/map/png/" + x["CleaningSession"]["MapImageUrl"] if x["CleaningSession"]["MapImageUrl"] else None,
                "map": x["CleaningSession"]["PersistentMapId"],
                "status": x["CleaningSession"]["Completion"],
                "usererror": x["CleaningSession"]["RobotUserError"],
                "internalerror": x["CleaningSession"]["RobotInternalError"],
            },
            filter(lambda x: x["CleaningSession"] != None, js["Items"])
        ))
            
        if js["Next"] != None:
            items += self.getCleaningSessions(nextptr=js["Next"])

        return items
    
    ###
    
    def _sendCleanCommand(self, command):
        return self._sendCommand({"CleaningCommand": command})

    def _sendCommand(self, body):

        headers = self.cloudclient.credentials.copy()
        headers["RobotId"] = self.id

        ws = websocket.WebSocket()

        try:
            ws.connect("wss://mobile.rvccloud.electrolux.com/api/v1/websocket/AppUser", header = headers)
            ws.send(json.dumps({
                "Type": 1, # 1 Request, 2 Response, 3 Event
                "Command": "AppUpdate",
                "Body": body
            }))
            ws.recv()

            return True
        finally:
            self._mark_changed()
            ws.close()
        
    def getMaps(self):
        r = do_http("GET", self.cloudclient.apiurl + "/robots/" + self.id + "/interactivemaps", auth=self.cloudclient.httpauth)
        
        return list(map(lambda x: CloudMap(self, x), r.json()))

class CloudClient:
    
    def __init__(self, email, password):
        
        password = CloudClient.chksum(password)
        self.apiurl = "https://mobile.rvccloud.electrolux.com/api/v1"
        self.credentials = {
            "AccountPassword": password,
            "Email": email,
        }
        
        self.httpauth = requests.auth.HTTPBasicAuth(email, password)
        
    @staticmethod
    def chksum(pw):
        buf = pw + "947X6kdLJyrhlCDzUyzFwT4s4NZL3O8eLs0PE4Hi7hU="
        buf = buf.encode("utf-16")[2:]
        return base64.b64encode(hashlib.sha256(buf).digest()).decode("ascii")
        
    def getRobots(self) -> List[CloudRobot]:
        """Get all robots linked to the cloud account"""
        r = do_http("POST", self.apiurl + "/accounts/ConnectToAccount", json=self.credentials)
        return list(map(lambda r: CloudRobot(self, r["RobotID"]), r.json()["RobotList"]))
            
    def getRobot(self, id) -> CloudRobot:
        """Make a CloudRobot instance with a given id. id is not checked."""
        return CloudRobot(self, id)
    
class CloudMap:
    
    def __init__(self, cloudrobot, js):
        
        self.cloudclient = cloudrobot.cloudclient
        self.robot       = cloudrobot
        self.id          = js["Id"]
        self.interactiveid = js["InteractiveId"]
        
        self.name        = js["Name"]
        self.zones       = list(map(lambda x: CloudZone(self, x), js["Zones"]))
        
        self.info        = None
        self.image       = None
        
        # self._get()
        
    def getImage2(self):
        r = do_http("GET", self.cloudclient.apiurl + "/robots/" + self.robot.id + "/interactivemaps/" + self.id + "/" + self.interactiveid, auth=self.cloudclient.httpauth)
        
        js = r.json()
        
        # image = base64.b64decode(js["PngImage"])
        # del js["PngImage"]
        # self.info = js
        
        return js
        
    def getImage(self):
        r = do_http("GET", self.cloudclient.apiurl + "/robots/" + self.robot.id + "/interactivemaps/" + self.id, auth=self.cloudclient.httpauth)
        
        js = r.json()
        
        # image = base64.b64decode(js["PngImage"])
        # del js["PngImage"]
        # self.info = js
        
        return js
    
class CloudZone:
    
    def __init__(self, cloudmap, js):
        
        self.cloudclient  = cloudmap.cloudclient
        self.map          = cloudmap
        self.id           = js["Id"]
        
        self.name         = js["Name"]
        self.type         = js["ZoneType"]
        self.roomcategory = js["RoomCategory"]
        
        # self._get()
