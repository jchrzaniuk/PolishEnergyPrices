"""Standalone HTTP and MQTT runtime for Polish Energy Prices."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from copy import deepcopy
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
from pathlib import Path
import ssl
from threading import Lock, Thread
from typing import Any
from urllib.request import Request, urlopen

from paho.mqtt import client as mqtt

from custom_components.polish_energy_price.source_engine import EnergyPriceSourceEngine
from custom_components.polish_energy_price.tariff import (
    EXCISE_NET_PLN_KWH,
    VAT,
    WARSAW,
    price_at,
)

from . import __version__
from .config import MqttConfig, ProfileConfig, ServiceConfig

_LOGGER = logging.getLogger(__name__)
_HTTP_TIMEOUT = 45
_USER_AGENT = f"PolishEnergyPricesService/{__version__}"


class HttpFetcher:
    """Download source documents with size and timeout limits."""

    async def get_bytes(
        self, url: str, limit: int, *, referer: str | None = None
    ) -> bytes:
        return await asyncio.to_thread(self._get_bytes, url, limit, referer)

    @staticmethod
    def _get_bytes(url: str, limit: int, referer: str | None) -> bytes:
        headers = {"User-Agent": _USER_AGENT}
        if referer:
            headers["Referer"] = referer
        request = Request(url, headers=headers)
        with urlopen(request, timeout=_HTTP_TIMEOUT) as response:
            declared = response.headers.get("Content-Length")
            if declared is not None and int(declared) > limit:
                raise ValueError(f"Odpowiedź źródła przekracza limit {limit} bajtów")
            content = response.read(limit + 1)
        if len(content) > limit:
            raise ValueError(f"Odpowiedź źródła przekracza limit {limit} bajtów")
        return content


class ProfileRuntime:
    """Mutable source data and cache for one configured profile."""

    def __init__(self, config: ProfileConfig, data_dir: Path) -> None:
        self.config = config
        self.engine = EnergyPriceSourceEngine(
            config.operator, config.tariff, config.price_source, _LOGGER
        )
        self.cache_path = data_dir / f"{config.profile_id}.json"
        self.data = self.engine.initial_data()

    def load_cache(self) -> None:
        if not self.cache_path.exists():
            return
        try:
            stored = json.loads(self.cache_path.read_text(encoding="utf-8"))
            self.data = self.engine.data_from_cache(stored)
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as err:
            _LOGGER.warning(
                "Pomijam nieprawidłowy cache profilu %s: %s",
                self.config.profile_id,
                err,
            )

    async def refresh(self, fetcher: HttpFetcher, now: datetime) -> None:
        self.data = await self.engine.refresh(
            self.data,
            fetcher.get_bytes,
            _run_sync,
            now,
        )
        payload = self.engine.cache_payload(self.data)
        temporary = self.cache_path.with_suffix(".json.tmp")
        try:
            await asyncio.to_thread(
                temporary.write_text,
                json.dumps(payload, ensure_ascii=False, indent=2),
                "utf-8",
            )
            await asyncio.to_thread(temporary.replace, self.cache_path)
        except OSError as err:
            _LOGGER.warning(
                "Nie udało się zapisać cache profilu %s w %s: %s",
                self.config.profile_id,
                self.cache_path,
                err,
            )

    def snapshot(self, now: datetime) -> dict[str, Any]:
        custom = (
            self.config.custom_prices
            if self.config.price_source == "custom"
            else self.data.prices
        )
        result = price_at(
            self.engine.tariff,
            now,
            custom_energy=custom,
            distribution_net=self.data.distribution_net,
            system_net=self.data.system_total,
            day_hours=self.config.day_hours,
            fixed_winter_time=self.config.meter_clock == "fixed_winter_time",
        )
        system_rates = self.data.system_net or {}
        network_net = float(
            (self.data.distribution_net or self.engine.tariff.distribution_net)[
                result.zone_key
            ]
        )
        quality_net = float(system_rates.get("quality", 0))
        oze_net = float(system_rates.get("oze", 0))
        cogeneration_net = float(system_rates.get("cogeneration", 0))
        distribution_net = network_net + quality_net + oze_net + cogeneration_net
        energy_net_with_excise = result.energy / VAT
        total_net = energy_net_with_excise + distribution_net
        valid = self.data.is_valid_on(now.date())
        source = "custom" if self.config.price_source == "custom" else self.data.source
        source_status = "current"
        if source.endswith("_cache") or self.data.error or self.data.official_error:
            source_status = "cache_or_warning"
        if not valid:
            source_status = "expired"
        return {
            "available": valid,
            "price_gross": round(result.total, 4) if valid else None,
            "unit": "PLN/kWh",
            "operator": self.config.operator,
            "tariff": self.engine.tariff.group,
            "zone": result.zone_key,
            "zone_name": result.zone_name,
            "energy_gross": round(result.energy, 4),
            "network_gross": round(network_net * VAT, 4),
            "quality_gross": round(quality_net * VAT, 4),
            "oze_gross": round(oze_net * VAT, 4),
            "cogeneration_gross": round(cogeneration_net * VAT, 4),
            "distribution_gross": round(result.distribution, 4),
            "excise_net": EXCISE_NET_PLN_KWH,
            "vat_total": round(result.total - total_net, 4),
            "price_net": round(total_net, 4),
            "fixed_charges_included": False,
            "energy_source": source,
            "source_status": source_status,
            "valid_from": self.data.valid_from,
            "valid_until": self.data.valid_until,
            "last_calculated": now.isoformat(),
            "last_checked": self.data.last_checked,
            "last_updated": self.data.last_updated,
            "official_last_checked": self.data.official_last_checked,
            "official_last_updated": self.data.official_last_updated,
            "errors": {
                "energy": self.data.error,
                "official": self.data.official_error,
            },
            "sources": {
                "energy": self.data.source_url,
                "distribution": self.data.distribution_source_url,
                "quality": self.data.system_source_url,
                "oze": self.data.oze_source_url,
                "cogeneration": self.data.cogeneration_source_url,
            },
        }


class MqttPublisher:
    """Publish retained service snapshots through one MQTT connection."""

    def __init__(
        self,
        config: MqttConfig,
        snapshots: Callable[[], dict[str, dict[str, Any]]],
    ) -> None:
        self.config = config
        self._snapshots = snapshots
        self._connected = False
        self._client: mqtt.Client | None = None

    def start(self) -> None:
        if not self.config.enabled:
            _LOGGER.info("Publikowanie MQTT jest wyłączone")
            return
        client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=self.config.client_id,
        )
        if self.config.username:
            client.username_pw_set(self.config.username, self.config.password)
        if self.config.tls:
            client.tls_set(
                ca_certs=self.config.ca_cert,
                cert_reqs=ssl.CERT_REQUIRED,
            )
        client.will_set(
            f"{self.config.topic_prefix}/availability",
            "offline",
            qos=self.config.qos,
            retain=True,
        )
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.connect_async(self.config.host, self.config.port, keepalive=60)
        client.loop_start()
        self._client = client

    def stop(self) -> None:
        client = self._client
        if client is None:
            return
        if self._connected:
            for profile_id in self._snapshots():
                client.publish(
                    f"{self.config.topic_prefix}/{profile_id}/availability",
                    "offline",
                    qos=self.config.qos,
                    retain=True,
                )
            client.publish(
                f"{self.config.topic_prefix}/availability",
                "offline",
                qos=self.config.qos,
                retain=True,
            ).wait_for_publish(timeout=5)
        client.disconnect()
        client.loop_stop()
        self._connected = False

    def publish_all(self) -> None:
        if not self._connected:
            return
        for profile_id, snapshot in self._snapshots().items():
            self.publish(profile_id, snapshot)

    def publish(self, profile_id: str, snapshot: dict[str, Any]) -> None:
        client = self._client
        if client is None or not self._connected:
            return
        base = f"{self.config.topic_prefix}/{profile_id}"
        self._publish(f"{base}/state", json.dumps(snapshot, ensure_ascii=False))
        scalar_keys = (
            "price_gross",
            "energy_gross",
            "network_gross",
            "quality_gross",
            "oze_gross",
            "cogeneration_gross",
            "distribution_gross",
            "excise_net",
            "vat_total",
            "price_net",
            "zone",
            "zone_name",
            "energy_source",
            "source_status",
            "last_calculated",
        )
        for key in scalar_keys:
            value = snapshot.get(key)
            self._publish(f"{base}/{key}", "" if value is None else str(value))
        self._publish(
            f"{base}/availability",
            "online" if snapshot.get("available") else "offline",
        )

    def _publish(self, topic: str, payload: str) -> None:
        assert self._client is not None
        self._client.publish(
            topic,
            payload,
            qos=self.config.qos,
            retain=self.config.retain,
        )

    def _on_connect(
        self,
        _client: mqtt.Client,
        _userdata: Any,
        _flags: mqtt.ConnectFlags,
        reason_code: mqtt.ReasonCode,
        _properties: mqtt.Properties | None,
    ) -> None:
        if reason_code.is_failure:
            _LOGGER.error("Połączenie MQTT odrzucone: %s", reason_code)
            return
        self._connected = True
        _LOGGER.info("Połączono z brokerem MQTT")
        self._publish(
            f"{self.config.topic_prefix}/availability",
            "online",
        )
        self.publish_all()

    def _on_disconnect(
        self,
        _client: mqtt.Client,
        _userdata: Any,
        _disconnect_flags: mqtt.DisconnectFlags,
        reason_code: mqtt.ReasonCode,
        _properties: mqtt.Properties | None,
    ) -> None:
        self._connected = False
        if reason_code.is_failure:
            _LOGGER.warning("Utracono połączenie MQTT: %s", reason_code)


class PriceService:
    """Coordinate source refreshes, snapshots, HTTP and MQTT."""

    def __init__(self, config: ServiceConfig) -> None:
        self.config = config
        self.data_dir = Path(config.data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.profiles = {
            item.profile_id: ProfileRuntime(item, self.data_dir)
            for item in config.profiles
        }
        self.fetcher = HttpFetcher()
        self._snapshot_lock = Lock()
        self._snapshots: dict[str, dict[str, Any]] = {}
        self._stop = asyncio.Event()
        self._http: ThreadingHTTPServer | None = None
        self._http_thread: Thread | None = None
        self.mqtt = MqttPublisher(config.mqtt, self.snapshots)

    async def run(self) -> None:
        for profile in self.profiles.values():
            profile.load_cache()
        self.recalculate()
        self._start_http()
        self.mqtt.start()
        try:
            await self.refresh_all()

            refresh_seconds = self.config.refresh_interval_hours * 3600
            next_refresh = asyncio.get_running_loop().time() + refresh_seconds
            last_hour = _hour_key(datetime.now(WARSAW))
            while not self._stop.is_set():
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=30)
                except TimeoutError:
                    pass
                now = datetime.now(WARSAW)
                hour = _hour_key(now)
                if hour != last_hour:
                    self.recalculate(now)
                    self.mqtt.publish_all()
                    last_hour = hour
                if asyncio.get_running_loop().time() >= next_refresh:
                    await self.refresh_all(now)
                    next_refresh = (
                        asyncio.get_running_loop().time() + refresh_seconds
                    )
        finally:
            self._shutdown()

    def stop(self) -> None:
        self._stop.set()

    async def refresh_all(self, now: datetime | None = None) -> None:
        checked_at = now or datetime.now(WARSAW)
        for profile_id, profile in self.profiles.items():
            _LOGGER.info("Odświeżam źródła profilu %s", profile_id)
            await profile.refresh(self.fetcher, checked_at)
        self.recalculate(checked_at)
        self.mqtt.publish_all()

    def recalculate(self, now: datetime | None = None) -> None:
        calculated_at = now or datetime.now(WARSAW)
        snapshots = {
            profile_id: profile.snapshot(calculated_at)
            for profile_id, profile in self.profiles.items()
        }
        with self._snapshot_lock:
            self._snapshots = snapshots

    def snapshots(self) -> dict[str, dict[str, Any]]:
        with self._snapshot_lock:
            return deepcopy(self._snapshots)

    def _start_http(self) -> None:
        handler = _handler_for(self)
        self._http = ThreadingHTTPServer(
            (self.config.http.host, self.config.http.port), handler
        )
        self._http_thread = Thread(
            target=self._http.serve_forever,
            name="polish-energy-prices-http",
            daemon=True,
        )
        self._http_thread.start()
        _LOGGER.info(
            "HTTP działa na %s:%d", self.config.http.host, self.config.http.port
        )

    def _shutdown(self) -> None:
        self.mqtt.stop()
        if self._http is not None:
            self._http.shutdown()
            self._http.server_close()
        if self._http_thread is not None:
            self._http_thread.join(timeout=5)


def _handler_for(service: PriceService) -> type[BaseHTTPRequestHandler]:
    class PriceRequestHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            path = self.path.split("?", 1)[0].rstrip("/") or "/"
            snapshots = service.snapshots()
            if path == "/health":
                self._json(
                    HTTPStatus.OK,
                    {"status": "ok", "profiles": len(snapshots)},
                )
                return
            if path == "/api/price":
                self._json(HTTPStatus.OK, {"profiles": snapshots})
                return
            if path == "/api/status":
                self._json(
                    HTTPStatus.OK,
                    {
                        "profiles": {
                            key: {
                                "available": value.get("available"),
                                "source_status": value.get("source_status"),
                                "last_calculated": value.get("last_calculated"),
                                "errors": value.get("errors"),
                            }
                            for key, value in snapshots.items()
                        }
                    },
                )
                return
            prefix = "/api/price/"
            if path.startswith(prefix):
                profile_id = path[len(prefix) :]
                if profile_id in snapshots:
                    self._json(HTTPStatus.OK, snapshots[profile_id])
                else:
                    self._json(
                        HTTPStatus.NOT_FOUND,
                        {"error": "Nie znaleziono profilu"},
                    )
                return
            self._json(HTTPStatus.NOT_FOUND, {"error": "Nie znaleziono zasobu"})

        def log_message(self, format: str, *args: Any) -> None:
            _LOGGER.debug("HTTP: " + format, *args)

        def _json(self, status: HTTPStatus, payload: object) -> None:
            content = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status.value)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

    return PriceRequestHandler


async def _run_sync(function: Callable[..., Any], *args: Any) -> Any:
    return await asyncio.to_thread(function, *args)


def _hour_key(value: datetime) -> tuple[int, int, int, int]:
    return value.year, value.month, value.day, value.hour
