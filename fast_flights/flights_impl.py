"""Typed implementation of flights_pb2.py"""

import base64
from dataclasses import dataclass
from typing import Any, List, Optional, TYPE_CHECKING, Literal, Union

from . import flights_pb2 as PB
from ._generated_enum import Airport

if TYPE_CHECKING:
    PB: Any

AIRLINE_ALLIANCES = ["SKYTEAM", "STAR_ALLIANCE", "ONEWORLD"]

class FlightData:
    """Represents flight data.

    Args:
        date (str): Date.
        from_airport (str): Departure (airport). Where from?
        to_airport (str): Arrival (airport). Where to?
        max_stops (int, optional): Maximum number of stops. Default is None.
        airlines (List[str], optional): Airlines this flight should be taken with. Default is None.
    """

    __slots__ = ("date", "from_airport", "to_airport", "max_stops", "airlines", "dep_hour")
    date: str
    from_airport: str
    to_airport: str
    max_stops: Optional[int]
    airlines: Optional[List[str]]
    dep_hour: Optional[int]

    def __init__(
        self,
        *,
        date: str,
        from_airport: Union[Airport, str],
        to_airport: Union[Airport, str],
        max_stops: Optional[int] = None,
        airlines: Optional[List[str]] = None,
        dep_hour: Optional[int] = None,
    ):
        self.date = date
        self.from_airport = (
            from_airport.value if isinstance(from_airport, Airport) else from_airport
        )
        self.to_airport = (
            to_airport.value if isinstance(to_airport, Airport) else to_airport
        )
        self.max_stops = max_stops
        self.dep_hour = dep_hour
        # TODO: All the list of airlines should technically be added to ._generated_enum like Airports
        # but I don't know how to find the comprehensive list of airlines now.
        if airlines is not None:
            self.airlines = []
            for airline in airlines:
                airline = airline.upper()
                if not (len(airline) == 2 or airline in AIRLINE_ALLIANCES):
                    raise ValueError(
                        f"Invalid airline code: {airline}. "
                        f"Airline codes should be 2 characters long or in the list of airline alliances: {AIRLINE_ALLIANCES}"
                    )
                self.airlines.append(airline)
        else:
            # make it consistent with self.max_stops and set it to None
            self.airlines = None

    def attach(self, info: PB.Info) -> None:  # type: ignore
        data = info.data.add()
        data.date = self.date
        data.from_flight.airport = self.from_airport
        data.to_flight.airport = self.to_airport
        if self.max_stops is not None:
            data.max_stops = self.max_stops
        if self.airlines is not None:
            data.airlines.extend(self.airlines)

    def __repr__(self) -> str:
        return (
            f"FlightData(date={self.date!r}, "
            f"from_airport={self.from_airport}, "
            f"to_airport={self.to_airport}, "
            f"max_stops={self.max_stops}, "
            f"airlines={self.airlines}"
        )


class Passengers:
    def __init__(
        self,
        *,
        adults: int = 0,
        children: int = 0,
        infants_in_seat: int = 0,
        infants_on_lap: int = 0,
    ):
        assert (
            sum((adults, children, infants_in_seat, infants_on_lap)) <= 9
        ), "Too many passengers (> 9)"
        assert (
            infants_on_lap <= adults
        ), "You must have at least one adult per infant on lap"

        self.pb = []
        self.pb += [PB.Passenger.ADULT for _ in range(adults)]
        self.pb += [PB.Passenger.CHILD for _ in range(children)]
        self.pb += [PB.Passenger.INFANT_IN_SEAT for _ in range(infants_in_seat)]
        self.pb += [PB.Passenger.INFANT_ON_LAP for _ in range(infants_on_lap)]

        self._data = (adults, children, infants_in_seat, infants_on_lap)

    def attach(self, info: PB.Info) -> None:  # type: ignore
        for p in self.pb:
            info.passengers.append(p)

    def __repr__(self) -> str:
        return f"Passengers({self._data})"


class TFSData:
    """``?tfs=`` data. (internal)

    Use `TFSData.from_interface` instead.
    """

    def __init__(
        self,
        *,
        flight_data: List[FlightData],
        seat: PB.Seat,  # type: ignore
        trip: PB.Trip,  # type: ignore
        passengers: Passengers,
        max_stops: Optional[int] = None,
        max_price: Optional[int] = None,
    ):
        self.flight_data = flight_data
        self.seat = seat
        self.trip = trip
        self.passengers = passengers
        self.max_stops = max_stops
        self.max_price = max_price

    def pb(self) -> PB.Info:  # type: ignore
        info = PB.Info()
        info.seat = self.seat
        info.trip = self.trip

        self.passengers.attach(info)

        for fd in self.flight_data:
            fd.attach(info)

        # If max_stops is set, attach it to all flight data entries
        if self.max_stops is not None:
            for flight in info.data:
                flight.max_stops = self.max_stops

        return info

    @staticmethod
    def _varint(value: int) -> bytes:
        out = []
        while value > 0x7f:
            out.append((value & 0x7f) | 0x80)
            value >>= 7
        out.append(value & 0x7f)
        return bytes(out)

    @staticmethod
    def _decode_var(data: bytes, pos: int):
        result = 0; shift = 0
        while True:
            b = data[pos]; pos += 1
            result |= (b & 0x7f) << shift
            if not (b & 0x80): break
            shift += 7
        return result, pos

    @classmethod
    def _inject_dep_hour(cls, data: bytes, flight_data_list: list) -> bytes:
        """Inyecta los campos dep_hour dentro de cada submensaje FlightData (field 3) del protobuf."""
        if not any(getattr(fd, 'dep_hour', None) is not None for fd in flight_data_list):
            return data

        result = bytearray()
        i = 0
        fd_idx = 0

        while i < len(data):
            tag, ni = cls._decode_var(data, i)
            wire_type = tag & 7
            field_num = tag >> 3

            if field_num == 3 and wire_type == 2:
                # FlightData submessage
                length, ni2 = cls._decode_var(data, ni)
                body = bytearray(data[ni2: ni2 + length])

                fd = flight_data_list[fd_idx] if fd_idx < len(flight_data_list) else None
                fd_idx += 1
                dep_hour = getattr(fd, 'dep_hour', None)

                if dep_hour is not None:
                    h = int(dep_hour)
                    # field 8 = dep_hour_from, field 9 = dep_hour_to (same hour = 1h window)
                    # field 10 = arr_hour_from (0), field 11 = arr_hour_to (23 = no filter)
                    body += (
                        b'\x40' + cls._varint(h) +
                        b'\x48' + cls._varint(h) +
                        b'\x50\x00\x58\x17'
                    )

                result += data[i: ni]          # tag bytes
                result += cls._varint(len(body))
                result += body
                i = ni2 + length

            elif wire_type == 0:
                val, ni2 = cls._decode_var(data, ni)
                result += data[i: ni2]
                i = ni2
            elif wire_type == 2:
                length, ni2 = cls._decode_var(data, ni)
                result += data[i: ni2 + length]
                i = ni2 + length
            elif wire_type == 5:
                result += data[i: ni + 4]
                i = ni + 4
            elif wire_type == 1:
                result += data[i: ni + 8]
                i = ni + 8
            else:
                result += data[i:]
                break

        return bytes(result)

    def to_string(self) -> bytes:
        raw = self.pb().SerializeToString()
        raw = self._inject_dep_hour(raw, self.flight_data)
        if self.max_price is not None:
            # Field 12, wire type 0 (varint) = tag 0x60; value = price in EUR
            raw += b'\x60' + self._varint(self.max_price)
        return raw

    def as_b64(self) -> bytes:
        return base64.b64encode(self.to_string())

    def as_url(self, currency: str = "", hl: str = "en") -> str:
        """Genera la URL de Google Flights para esta búsqueda."""
        from urllib.parse import urlencode
        params: dict = {"tfs": self.as_b64().decode("utf-8"), "hl": hl}
        if currency:
            params["curr"] = currency
        return "https://www.google.com/travel/flights?" + urlencode(params)

    @staticmethod
    def from_interface(
        *,
        flight_data: List[FlightData],
        trip: Literal["round-trip", "one-way", "multi-city"],
        passengers: Passengers,
        seat: Literal["economy", "premium-economy", "business", "first"],
        max_stops: Optional[int] = None,
        max_price: Optional[int] = None,
    ):
        """Use ``?tfs=`` from an interface.

        Args:
            flight_data (list[FlightData]): Flight data as a list.
            trip ("one-way" | "round-trip" | "multi-city"): Trip type.
            passengers (Passengers): Passengers.
            seat ("economy" | "premium-economy" | "business" | "first"): Seat.
            max_stops (int, optional): Maximum number of stops.
        """
        trip_t = {
            "round-trip": PB.Trip.ROUND_TRIP,
            "one-way": PB.Trip.ONE_WAY,
            "multi-city": PB.Trip.MULTI_CITY,
        }[trip]
        seat_t = {
            "economy": PB.Seat.ECONOMY,
            "premium-economy": PB.Seat.PREMIUM_ECONOMY,
            "business": PB.Seat.BUSINESS,
            "first": PB.Seat.FIRST,
        }[seat]

        return TFSData(
            flight_data=flight_data,
            seat=seat_t,
            trip=trip_t,
            passengers=passengers,
            max_stops=max_stops,
            max_price=max_price,
        )

    def __repr__(self) -> str:
        return f"TFSData(flight_data={self.flight_data!r}, max_stops={self.max_stops!r})"

@dataclass
class ItinerarySummary:
    flights: str
    price: int
    currency: str

    @classmethod
    def from_b64(cls, b64_string: str) -> 'ItinerarySummary':
        raw = base64.b64decode(b64_string)
        pb = PB.ItinerarySummary()
        pb.ParseFromString(raw)
        return cls(pb.flights, pb.price.price / 100, pb.price.currency)
