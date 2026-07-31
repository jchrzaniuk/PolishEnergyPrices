"""Configuration loader for the standalone HTTP and MQTT service."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
from typing import Any

import yaml

from custom_components.polish_energy_price.source_engine import (
    EnergyPriceSourceEngine,
)
from custom_components.polish_energy_price.tariff import parse_day_hours

_PROFILE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_ENV_REFERENCE = re.compile(r"\$\{[^}]+\}")


@dataclass(frozen=True, slots=True)
class ProfileConfig:
    """One OSD and tariff exposed as an HTTP and MQTT profile."""

    profile_id: str
    operator: str
    tariff: str
    price_source: str
    meter_clock: str = "local_time"
    day_hours: str | None = None
    custom_prices: dict[str, float] | None = None


@dataclass(frozen=True, slots=True)
class HttpConfig:
    """HTTP listener settings."""

    host: str = "0.0.0.0"
    port: int = 8080


@dataclass(frozen=True, slots=True)
class MqttConfig:
    """MQTT connection and publication settings."""

    enabled: bool = True
    host: str = "mqtt"
    port: int = 1883
    client_id: str = "polish-energy-prices"
    username: str | None = None
    password: str | None = None
    topic_prefix: str = "polish_energy_prices"
    qos: int = 1
    retain: bool = True
    tls: bool = False
    ca_cert: str | None = None


@dataclass(frozen=True, slots=True)
class ServiceConfig:
    """Complete service configuration."""

    profiles: tuple[ProfileConfig, ...]
    http: HttpConfig
    mqtt: MqttConfig
    refresh_interval_hours: float = 12.0
    data_dir: str = "/data"


def load_config(path: str | Path) -> ServiceConfig:
    """Load, expand environment references and validate a YAML file."""

    config_path = Path(path)
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    raw = _expand_environment(raw)
    if not isinstance(raw, dict):
        raise ValueError("Główny element konfiguracji musi być obiektem")

    profiles_raw = raw.get("profiles")
    if not isinstance(profiles_raw, dict) or not profiles_raw:
        raise ValueError("Sekcja profiles musi zawierać co najmniej jeden profil")
    profiles = tuple(
        _profile_config(str(profile_id), profile)
        for profile_id, profile in profiles_raw.items()
    )

    http_raw = _mapping(raw.get("http", {}), "http")
    http = HttpConfig(
        host=str(http_raw.get("host", "0.0.0.0")),
        port=_port(http_raw.get("port", 8080), "http.port"),
    )

    mqtt_raw = _mapping(raw.get("mqtt", {}), "mqtt")
    mqtt = MqttConfig(
        enabled=bool(mqtt_raw.get("enabled", True)),
        host=str(mqtt_raw.get("host", "mqtt")),
        port=_port(mqtt_raw.get("port", 1883), "mqtt.port"),
        client_id=str(mqtt_raw.get("client_id", "polish-energy-prices")),
        username=_optional_str(mqtt_raw.get("username")),
        password=_optional_str(mqtt_raw.get("password")),
        topic_prefix=str(
            mqtt_raw.get("topic_prefix", "polish_energy_prices")
        ).strip("/"),
        qos=int(mqtt_raw.get("qos", 1)),
        retain=bool(mqtt_raw.get("retain", True)),
        tls=bool(mqtt_raw.get("tls", False)),
        ca_cert=_optional_str(mqtt_raw.get("ca_cert")),
    )
    if mqtt.enabled and not mqtt.host:
        raise ValueError("mqtt.host nie może być pusty")
    if mqtt.qos not in {0, 1, 2}:
        raise ValueError("mqtt.qos musi mieć wartość 0, 1 albo 2")
    if mqtt.enabled and not mqtt.topic_prefix:
        raise ValueError("mqtt.topic_prefix nie może być pusty")

    refresh_interval = float(raw.get("refresh_interval_hours", 12))
    if not 0.25 <= refresh_interval <= 168:
        raise ValueError("refresh_interval_hours musi mieścić się w zakresie 0.25-168")
    data_dir = str(raw.get("data_dir", "/data"))
    if not data_dir:
        raise ValueError("data_dir nie może być pusty")
    return ServiceConfig(profiles, http, mqtt, refresh_interval, data_dir)


def _profile_config(profile_id: str, raw: object) -> ProfileConfig:
    if not _PROFILE_ID.fullmatch(profile_id):
        raise ValueError(
            f"Nieprawidłowy identyfikator profilu {profile_id!r}; "
            "użyj małych liter, cyfr, _ lub -"
        )
    values = _mapping(raw, f"profiles.{profile_id}")
    operator = str(values.get("operator", "")).lower()
    group = str(values.get("tariff", ""))
    if not operator or not group:
        raise ValueError(f"Profil {profile_id} wymaga operator i tariff")
    source_default = (
        "tauron_g13s"
        if operator == "tauron" and group.lower() == "g13s"
        else "regulated"
    )
    price_source = str(values.get("price_source", source_default))
    if price_source not in {"regulated", "tauron_g13s", "custom"}:
        raise ValueError(f"Profil {profile_id} ma nieznane price_source")
    if price_source == "tauron_g13s" and not (
        operator == "tauron" and group.lower() == "g13s"
    ):
        raise ValueError("Źródło tauron_g13s działa tylko dla TAURON G13s")
    if price_source == "regulated" and group.lower() == "g13s":
        raise ValueError("G13s nie występuje w arkuszu sprzedawców z urzędu URE")

    meter_clock = str(values.get("meter_clock", "local_time"))
    if meter_clock not in {"local_time", "fixed_winter_time"}:
        raise ValueError(f"Profil {profile_id} ma nieznane meter_clock")
    day_hours = _optional_str(values.get("day_hours"))
    if day_hours:
        parse_day_hours(day_hours)

    custom_prices_raw = values.get("custom_prices")
    custom_prices: dict[str, float] | None = None
    engine = EnergyPriceSourceEngine(operator, group, price_source)
    if custom_prices_raw is not None:
        custom_prices = engine.validate_prices(custom_prices_raw)
    if price_source == "custom" and custom_prices is None:
        raise ValueError(f"Profil {profile_id} wymaga custom_prices")
    return ProfileConfig(
        profile_id,
        operator,
        engine.tariff.group,
        price_source,
        meter_clock,
        day_hours,
        custom_prices,
    )


def _expand_environment(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _expand_environment(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_expand_environment(item) for item in value]
    if not isinstance(value, str):
        return value
    expanded = os.path.expandvars(value)
    if _ENV_REFERENCE.search(expanded):
        raise ValueError(f"Brak zmiennej środowiskowej użytej w {value!r}")
    return expanded


def _mapping(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"Sekcja {name} musi być obiektem")
    return value


def _port(value: object, name: str) -> int:
    port = int(value)
    if not 1 <= port <= 65535:
        raise ValueError(f"{name} musi mieścić się w zakresie 1-65535")
    return port


def _optional_str(value: object) -> str | None:
    if value is None or value == "":
        return None
    return str(value)
