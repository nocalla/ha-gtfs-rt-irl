"""Support for the GTFS Realtime Ireland service."""
from __future__ import annotations

import datetime
import logging
import os
import sqlite3
import time
from typing import Any

import homeassistant.helpers.config_validation as cv
import pygtfs
import requests
import voluptuous as vol
from google.transit import gtfs_realtime_pb2
from homeassistant.components.sensor import PLATFORM_SCHEMA
from homeassistant.const import ATTR_LATITUDE, ATTR_LONGITUDE, CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType
from homeassistant.util import Throttle

_LOGGER = logging.getLogger(__name__)

REQUIREMENTS = ["gtfs-realtime-bindings==0.0.7", "protobuf==3.20.1"]

ATTR_STOP_NAME = "Stop Name"
ATTR_ROUTE = "Route"
ATTR_DUE_IN = "Due in"
ATTR_NEXT_ARRIVAL = "next_arrival"
ATTR_HITS = "arrivals"
ATTR_DEP_TIME = "departure_time"

CONF_API_KEY = "api_key"
CONF_STOP_NAME = "stop_name"
CONF_ROUTE = "route"
CONF_DEPARTURES = "departures"
CONF_OPERATOR = "operator"
CONF_TRIP_UPDATE_URL = "trip_update_url"
CONF_VEHICLE_POSITION_URL = "vehicle_position_url"
CONF_ZIP_FILE = "schedule_zip_file"
CONF_LIMIT = "arrivals_limit"

DEFAULT_NAME = "gtfs-rt-irl"
DEFAULT_PATH = "gtfs"
ICON = "mdi:bus"

MIN_TIME_BETWEEN_UPDATES = datetime.timedelta(seconds=60)
TIME_STR_FORMAT = "%H:%M"

""" These below constants are new additions
CONF_DIRECTION_ID, CONF_ICON, CONF_ROUTE_DELIMITER, CONF_SERVICE_TYPE,
CONF_STOP_ID, CONF_X_API_KEY, DEFAULT_DIRECTION,
DEFAULT_ICON, DEFAULT_SERVICE,"""

ATTR_STOP_ID = "Stop ID"
ATTR_DIRECTION_ID = "Direction ID"
ATTR_DUE_AT = "Due at"
ATTR_NEXT_UP = "Next Service"
ATTR_ICON = "Icon"

CONF_X_API_KEY = "x_api_key"
CONF_STOP_ID = "stopid"
CONF_DIRECTION_ID = "directionid"
CONF_ROUTE_DELIMITER = "route_delimiter"
CONF_ICON = "icon"
CONF_SERVICE_TYPE = "service_type"

DEFAULT_SERVICE = "Service"
DEFAULT_ICON = "mdi:bus"
DEFAULT_DIRECTION = "0"


PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_TRIP_UPDATE_URL): cv.string,
        vol.Required(CONF_API_KEY): cv.string,
        vol.Optional(CONF_X_API_KEY): cv.string,  # new
        vol.Required(CONF_ZIP_FILE): cv.string,
        vol.Optional(CONF_LIMIT, default=30): vol.Coerce(int),  # type: ignore # what is this limiting?
        vol.Optional(CONF_VEHICLE_POSITION_URL): cv.string,
        vol.Optional(CONF_ROUTE_DELIMITER): cv.string,  # new
        vol.Optional(CONF_DEPARTURES): [
            {
                vol.Required(CONF_NAME): cv.string,
                vol.Required(CONF_STOP_NAME): cv.string,  # remove?
                vol.Required(CONF_STOP_ID): cv.string,  # new
                vol.Required(CONF_ROUTE): cv.string,
                vol.Required(
                    CONF_OPERATOR
                ): cv.string,  # remove (not required in new API)
                vol.Optional(
                    CONF_DIRECTION_ID,
                    default=DEFAULT_DIRECTION,  # type: ignore
                ): str,  # new
                vol.Optional(
                    CONF_ICON, default=DEFAULT_ICON  # type: ignore
                ): cv.string,  # new
                vol.Optional(
                    CONF_SERVICE_TYPE, default=DEFAULT_SERVICE  # type: ignore
                ): cv.string,  # new
            }
        ],
    }
)


def get_times(route_stops, gtfs_database_path, set_limit):
    """Get the next departure times for today for each required/configured
    stop, route and operator."""

    conn = sqlite3.connect(gtfs_database_path)
    ctrips = conn.cursor()
    cstoptimes = conn.cursor()
    cstops = conn.cursor()
    cservice = conn.cursor()
    croutes = conn.cursor()
    cexcp = conn.cursor()

    date_format = "%Y-%m-%d %H:%M:%S.%f"
    pattern = "%Y-%m-%d %H:%M:%S.%f"
    pattern1 = "1970-01-01 %H:%M:%S.%f"
    pattern2 = "%Y-%m-%d"

    def validate_service(service_id):
        """Is a service id valid for today with no exceptions."""

        result = False
        today = datetime.datetime.today().weekday()
        cservice.execute(
            "SELECT * from calendar WHERE service_id=:service",
            {"service": service_id},
        )
        days_of_week = cservice.fetchone()
        today_flag = list(days_of_week)[today + 2]

        today_date = datetime.datetime.today()
        today_date1 = str(today_date)
        today_date2 = datetime.datetime.strftime(today_date, pattern2)
        d_t = int(time.mktime(time.strptime(today_date1, date_format)))
        from_date = list(days_of_week)[9]
        to_date = list(days_of_week)[10]
        dt1 = int(time.mktime(time.strptime(from_date, pattern2)))
        dt2 = int(time.mktime(time.strptime(to_date, pattern2)))

        #    validity = True if d_t >= dt1 and d_t <= dt2 else False
        validity = bool(dt1 <= d_t <= dt2)

        if today_flag == 1 and validity:
            cexcp.execute(
                (
                    "SELECT * from calendar_dates WHERE service_id=:service"
                    " and date=:date"
                ),
                {"service": service_id, "date": today_date2},
            )
            exception_date = cexcp.fetchone()
            if exception_date is not None:
                result = False
            else:
                result = True
        return result

    stop_times = []

    for r_s in route_stops:
        req_stop_name = r_s[0]
        req_route = r_s[1]
        req_operator = r_s[2]

        cstops.execute(
            "SELECT stop_id, stop_name from stops WHERE stop_name=:stop",
            {"stop": req_stop_name},
        )

        stop_data = cstops.fetchone()
        req_stop_id = stop_data[0]

        croutes.execute(
            (
                "SELECT agency_id, route_id from routes WHERE"
                " route_short_name=:route AND agency_id=:operator"
            ),
            {"route": req_route, "operator": req_operator},
        )

        valid_operator = croutes.fetchone()
        req_route_id = valid_operator[1]

        if valid_operator is not None:
            ctrips.execute(
                "SELECT trip_id, service_id from trips WHERE route_id=:route",
                {"route": req_route_id},
            )

            for trip_id, service_id in ctrips.fetchall():
                req_trip = trip_id
                cstoptimes.execute(
                    (
                        "SELECT arrival_time, departure_time, stop_id FROM"
                        " stop_times WHERE trip_id=:trip AND stop_id=:stop"
                    ),
                    {"trip": req_trip, "stop": req_stop_id},
                )

                departure = cstoptimes.fetchone()

                if departure is not None:
                    dep_time_str = list(departure)[1]

                    epoch_dep = int(
                        time.mktime(time.strptime(dep_time_str, pattern))
                    )

                    now = datetime.datetime.now()
                    curr_time_str = now.strftime(pattern1)
                    epoch_now = int(
                        time.mktime(time.strptime(curr_time_str, pattern))
                    )

                    if epoch_dep >= epoch_now:
                        diff = epoch_dep - epoch_now
                        if validate_service(service_id):
                            stop_times.append(
                                (
                                    req_stop_name,
                                    req_route,
                                    req_trip,
                                    int(diff / 60),
                                    dep_time_str,
                                )
                            )

    stop_times = sorted(stop_times, key=lambda x: x[3])
    stop_times = stop_times[0:set_limit]

    ctrips.close()
    cstops.close()
    cstoptimes.close()
    cservice.close()
    croutes.close()
    cexcp.close()

    return stop_times


def setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Set up the GTFS Realtime sensor and load the database
    from the Zip file if needed."""

    zip_file = str(config.get(CONF_ZIP_FILE))
    gtfs_dir = hass.config.path(DEFAULT_PATH)
    os.makedirs(gtfs_dir, exist_ok=True)

    (gtfs_root, _) = os.path.splitext(zip_file)
    sqlite_file = f"{gtfs_root}.sqlite?check_same_thread=False"
    sqlite_filedb = f"{gtfs_root}.sqlite"
    gtfs_database_path = os.path.join(gtfs_dir, sqlite_filedb)

    if not os.path.exists(os.path.join(gtfs_dir, sqlite_filedb)):
        if not os.path.exists(os.path.join(gtfs_dir, zip_file)):
            _LOGGER.error("The GTFS data file/folder was not found")
            return

        gtfs_load_path = os.path.join(gtfs_dir, sqlite_file)

        gtfs = pygtfs.Schedule(gtfs_load_path)
        if not gtfs.feeds:  # type: ignore
            pygtfs.append_feed(gtfs, os.path.join(gtfs_dir, zip_file))
            conn = sqlite3.connect(gtfs_database_path)
            cursor = conn.cursor()
            create_index = (
                "CREATE INDEX index_stop_times_1 ON stop_times(trip_id,"
                " stop_id )"
            )
            cursor.execute(create_index)
            cursor.close()

    trip_url = config.get(CONF_TRIP_UPDATE_URL)
    vehicle_pos_url = str(config.get(CONF_VEHICLE_POSITION_URL))
    api_key = config.get(CONF_API_KEY)
    set_limit = config.get(CONF_LIMIT)

    route_deps = []

    for departure in config.get(CONF_DEPARTURES, []):
        stop_name = departure.get(CONF_STOP_NAME)
        route = departure.get(CONF_ROUTE)
        operator = departure.get(CONF_OPERATOR)
        route_deps.append((stop_name, route, operator))

    data = PublicTransportData(
        gtfs_database_path,
        trip_url,
        route_deps,
        vehicle_pos_url,
        api_key,
        set_limit,  # type: ignore
    )

    sensors = []

    for departure in config.get(CONF_DEPARTURES, []):
        stop_name = departure.get(CONF_STOP_NAME)
        route = departure.get(CONF_ROUTE)
        operator = departure.get(CONF_OPERATOR)
        sensors.append(PublicTransportSensor(data, stop_name, route))

    add_entities(sensors)


class PublicTransportSensor(Entity):
    """Implementation of the GTFS-RT sensor."""

    def __init__(self, data, stop_name, route_no):
        """Initialize the sensor."""
        self.data = data
        self._stop_name = stop_name
        self._route_no = route_no
        self.update()

    @property
    def name(self) -> str:
        """Return the sensor name."""
        return self._stop_name

    def _get_next_buses(self):
        return self.data.info.get(self._route_no, {}).get(self._stop_name, [])

    @property
    def state(self) -> str:
        """Return the state of the sensor."""
        next_buses = self._get_next_buses()
        return next_buses[0].arrival_time if len(next_buses) > 0 else "-"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the extra state attributes."""
        next_buses = self._get_next_buses()
        arrivals = str(len(next_buses))
        next_arrival = "-"
        departure_time = "-"
        attrs = {
            ATTR_DUE_IN: self.state,
            ATTR_STOP_NAME: self._stop_name,
            ATTR_ROUTE: self._route_no,
            ATTR_NEXT_ARRIVAL: next_arrival,
            ATTR_HITS: arrivals,
            ATTR_DEP_TIME: departure_time,
        }
        if len(next_buses) > 0:
            attrs[ATTR_DEP_TIME] = next_buses[0].dep_time

            if next_buses[0].position:
                attrs[ATTR_LATITUDE] = next_buses[0].position.latitude
                attrs[ATTR_LONGITUDE] = next_buses[0].position.longitude
        if len(next_buses) > 1:
            attrs[ATTR_NEXT_ARRIVAL] = (
                next_buses[1].arrival_time if len(next_buses) > 1 else "-"
            )
        return attrs

    @property
    def unit_of_measurement(self) -> str:
        """Return the unit of the state which is in minutes."""
        return "min"

    @property
    def icon(self) -> str:
        """Icon to use in the frontend, if any."""
        return ICON

    def update(self) -> None:
        """Get the latest data from the static schedule data, realtime feed
        and update the states."""
        self.data.update()


class PublicTransportData:
    """The Class for handling the data retrieval from the published API."""

    def __init__(
        self,
        gtfs_database_path,
        trip_url,
        route_deps,
        vehicle_position_url="",
        api_key=None,
        set_limit=0,
    ):
        """Initialize the info object."""
        self._gtfs_database_path = gtfs_database_path
        self._trip_update_url = trip_url
        self._route_deps = route_deps
        self._vehicle_position_url = vehicle_position_url
        self._set_limit = set_limit

        if api_key is not None:
            self._headers = {"x-api-key": api_key}
        else:
            self._headers = None

        self.info = {}

    @Throttle(MIN_TIME_BETWEEN_UPDATES)
    def update(self):
        """Update for the data object."""
        positions = (
            self._get_vehicle_positions()
            if self._vehicle_position_url != ""
            else {}
        )
        self._update_route_statuses(positions)

    def _update_route_statuses(self, vehicle_positions):
        """Get the latest data."""

        class StopDetails:
            """Stop times object list.
            Position is not implemented - future."""

            def __init__(self, arrival_time, position, dep_time):
                self.arrival_time = arrival_time
                self.position = position
                self.dep_time = dep_time

        next_times = get_times(
            self._route_deps, self._gtfs_database_path, self._set_limit
        )

        feed = gtfs_realtime_pb2.FeedMessage()  # type: ignore
        response = requests.get(
            self._trip_update_url, headers=self._headers, timeout=30
        )
        if response.status_code != 200:
            _LOGGER.error(
                "Updating route status got "
                " {response.status_code}:{response.content}"
            )

        feed.ParseFromString(response.content)

        departure_times = {}

        for arrival_time in next_times:
            stop_name = arrival_time[0]
            route_no = arrival_time[1]
            trip_no = arrival_time[2]
            modified_time = int(arrival_time[3])
            dep_time = arrival_time[4]
            dep_time = dep_time[10:16]

            vehicle_position = 0
            for entity in feed.entity:
                if entity.HasField("trip_update"):
                    if entity.trip_update.trip.trip_id == trip_no:
                        for stop in entity.trip_update.stop_time_update:
                            if stop.HasField("arrival"):
                                modified_time = modified_time + int(
                                    stop.arrival.delay / 60
                                )
                    vehicle_position = vehicle_positions.get(
                        entity.trip_update.vehicle.id
                    )

            if route_no not in departure_times:
                departure_times[route_no] = {}
            if not departure_times[route_no].get(stop_name):
                departure_times[route_no][stop_name] = []
            details = StopDetails(modified_time, vehicle_position, dep_time)
            departure_times[route_no][stop_name].append(details)

        # Sort by arrival time
        for route_no in departure_times:
            for stop_name in departure_times[route_no]:
                departure_times[route_no][stop_name].sort(
                    key=lambda t: t.arrival_time
                )

        self.info = departure_times

    def _get_vehicle_positions(self):
        feed = gtfs_realtime_pb2.FeedMessage()  # type: ignore
        response = requests.get(
            self._vehicle_position_url, headers=self._headers, timeout=60
        )
        if response.status_code != 200:
            _LOGGER.error(
                "Updating vehicle positions got"
                " {response.status_code}:{response.content}"
            )
        feed.ParseFromString(response.content)
        positions = {}

        for entity in feed.entity:
            vehicle = entity.vehicle

            if not vehicle.trip.route_id:
                # Vehicle is not in service
                continue

            positions[vehicle.vehicle.id] = vehicle.position

        return positions
