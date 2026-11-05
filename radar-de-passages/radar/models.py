import ipaddress
import json
from datetime import date, datetime, timezone
from typing import Annotated, Literal, Union
from urllib.parse import urlsplit, urlunsplit
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from . import catalog

Airport = Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
Carrier = Annotated[str, Field(pattern=r"^[A-Z0-9]{2}$")]
IsoDate = Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}$")]
Currency = Literal["USD", "PEN", "EUR"]
Place = Annotated[str, Field(min_length=2, max_length=60)]


def _day_passed(iso_day, now):
    """True when the calendar day is before 'now' (UTC calendar, same as the service)."""
    return date.fromisoformat(iso_day) < datetime.fromtimestamp(now, timezone.utc).date()


class _Criteria(BaseModel):
    """Shared behaviour. Criteria are immutable observation conditions of one watch."""

    model_config = ConfigDict(extra="forbid", strict=True)

    def canonical(self):
        return json.dumps(self.model_dump(), sort_keys=True, separators=(",", ":"))


class Criteria(_Criteria):
    """Flight criteria (kept under its original name for backwards compatibility)."""

    kind: Literal["flight"] = "flight"
    origin: Airport
    destination: Airport
    departure: IsoDate
    return_date: IsoDate | None = None
    adults: Annotated[int, Field(ge=1, le=9)] = 1
    children: Annotated[int, Field(ge=0, le=8)] = 0
    infants: Annotated[int, Field(ge=0, le=8)] = 0
    currency: Currency = "USD"
    cabin: Literal["ECONOMY", "PREMIUM_ECONOMY", "BUSINESS"] = "ECONOMY"
    # 0 = direct only. Providers pass it on; the demo fixture ignores it.
    max_connections: Annotated[int, Field(ge=0, le=2)] = 1
    # Optional allow-list of airline IATA codes; empty means every airline.
    airlines: list[Carrier] = Field(default_factory=list, max_length=10)

    @field_validator("airlines")
    @classmethod
    def sorted_unique(cls, value):
        return sorted(set(value))

    @model_validator(mode="after")
    def valid_trip(self):
        departure = date.fromisoformat(self.departure)
        if self.return_date:
            back = date.fromisoformat(self.return_date)
            if back < departure:
                raise ValueError("La vuelta no puede preceder a la ida.")
        if self.origin == self.destination:
            raise ValueError("Origen y destino deben ser distintos.")
        if self.infants > self.adults or self.adults + self.children + self.infants > 9:
            raise ValueError("Máximo nueve pasajeros y un infante por adulto.")
        return self

    @property
    def travelers(self):
        return self.adults + self.children + self.infants

    def is_past(self, now):
        return _day_passed(self.departure, now)

    def describe(self):
        return (
            f"{self.origin} → {self.destination}; salida {self.departure}; "
            f"vuelta {self.return_date or 'solo ida'}; {self.adults} adultos, "
            f"{self.children} niños, {self.infants} infantes; {self.cabin}"
        )


FlightCriteria = Criteria


class BusCriteria(_Criteria):
    kind: Literal["bus"] = "bus"
    origin: Place
    destination: Place
    departure: IsoDate
    return_date: IsoDate | None = None
    passengers: Annotated[int, Field(ge=1, le=9)] = 1
    currency: Currency = "PEN"
    # Optional allow-list of operator names; empty means every operator.
    operators: list[Annotated[str, Field(min_length=2, max_length=40)]] = Field(
        default_factory=list, max_length=10
    )

    @field_validator("origin", "destination")
    @classmethod
    def clean_place(cls, value):
        return " ".join(value.split())

    @field_validator("operators")
    @classmethod
    def sorted_unique(cls, value):
        return sorted({" ".join(v.split()) for v in value})

    @model_validator(mode="after")
    def valid_trip(self):
        departure = date.fromisoformat(self.departure)
        if self.return_date and date.fromisoformat(self.return_date) < departure:
            raise ValueError("La vuelta no puede preceder a la ida.")
        if self.origin.casefold() == self.destination.casefold():
            raise ValueError("Origen y destino deben ser distintos.")
        return self

    @property
    def travelers(self):
        return self.passengers

    def is_past(self, now):
        return _day_passed(self.departure, now)

    def describe(self):
        return (
            f"Bus {self.origin} → {self.destination}; salida {self.departure}; "
            f"vuelta {self.return_date or 'solo ida'}; {self.passengers} pasajeros"
        )


def normalize_url(raw):
    """Lower-case host, drop fragment and credentials, https only, no literal IPs."""
    parts = urlsplit(raw.strip())
    if parts.scheme != "https":
        raise ValueError("La URL debe usar https.")
    if parts.username or parts.password:
        raise ValueError("La URL no puede incluir credenciales.")
    host = (parts.hostname or "").lower().rstrip(".")
    if not host or "." not in host or host == "localhost" or host.endswith(".local"):
        raise ValueError("La URL debe tener un dominio público.")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ValueError("No se admiten direcciones IP; usa el dominio de la tienda.")
    port = f":{parts.port}" if parts.port and parts.port != 443 else ""
    return urlunsplit(("https", host + port, parts.path or "/", parts.query, ""))


class ProductCriteria(_Criteria):
    kind: Literal["product"] = "product"
    url: Annotated[str, Field(min_length=12, max_length=500)]
    sku: Annotated[str, Field(min_length=1, max_length=80)] | None = None
    currency: Currency = "PEN"

    @field_validator("url")
    @classmethod
    def valid_url(cls, value):
        return normalize_url(value)

    @property
    def domain(self):
        return urlsplit(self.url).hostname

    @property
    def travelers(self):
        return 1

    def is_past(self, now):
        return False

    def describe(self):
        return f"{self.domain}{urlsplit(self.url).path}"


AnyCriteria = Annotated[
    Union[Criteria, BusCriteria, ProductCriteria], Field(discriminator="kind")
]
CRITERIA_BY_KIND = {"flight": Criteria, "bus": BusCriteria, "product": ProductCriteria}


def parse_criteria(kind, data):
    return CRITERIA_BY_KIND[kind].model_validate(data)


Target = Annotated[int, Field(ge=1, le=1_000_000_000)]
Provider = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_-]{1,30}$")]


AlertMode = Literal["target", "smart", "both"]
NO_TARGET = 1  # stored when alert_mode='smart' and no absolute target is given (never used)


class WatchInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    criteria: AnyCriteria
    target_cents: Target | None = None
    alert_mode: AlertMode = "target"
    enabled: bool = True
    channel: Literal["simulation", "telegram"] = "simulation"
    provider: Provider = "demo"
    label: Annotated[str, Field(min_length=1, max_length=80)] | None = None
    category: str | None = None
    subcategory: str | None = None

    @model_validator(mode="after")
    def valid_category(self):
        if self.target_cents is None:
            if self.alert_mode != "smart":
                raise ValueError("Indica el precio objetivo (o usa alert_mode=smart).")
            self.target_cents = NO_TARGET
        self.category, self.subcategory = catalog.resolve_category(
            self.criteria.kind, self.category, self.subcategory
        )
        return self


class WatchConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    target_cents: Target | None = None  # None keeps the stored target (a smart watch has none of its own)
    alert_mode: AlertMode | None = None
    enabled: bool
    channel: Literal["simulation", "telegram"] = "simulation"
