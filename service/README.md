# Usługa Docker dla openHAB i innych systemów

Kontener używa tego samego silnika taryf i parserów urzędowych co integracja
Home Assistant. Udostępnia bieżącą cenę przez HTTP oraz retained MQTT. Nie
potrzebuje dostępu do Home Assistanta ani openHAB.

## Uruchomienie

1. Skopiuj przykładową konfigurację:

   ```bash
   cp service/config.example.yaml service/config.yaml
   ```

2. Ustaw operatora, taryfę i adres brokera MQTT w `service/config.yaml`.
3. Uruchom usługę:

   ```bash
   docker compose up -d
   ```

   Obraz jest publikowany jako
   `ghcr.io/jchrzaniuk/polish-energy-prices:latest`. Po pierwszej publikacji
   pakiet GHCR musi mieć widoczność publiczną, aby użytkownicy nie potrzebowali
   logowania do rejestru.

4. Sprawdź odpowiedź:

   ```bash
   curl http://localhost:8080/api/price/dom
   curl http://localhost:8080/health
   ```

Plik `service/config.yaml` nie powinien trafiać do repozytorium, jeżeli zawiera
hasło MQTT. Dane logowania można wstawić przez `${MQTT_USERNAME}` i
`${MQTT_PASSWORD}`, a ich wartości umieścić w pliku `.env` obok
`compose.yaml`.

## Konfiguracja profilu

Każdy wpis w `profiles` ma własny identyfikator używany w adresie HTTP i
tematach MQTT:

```yaml
profiles:
  dom:
    operator: tauron
    tariff: G13s
    price_source: tauron_g13s
    meter_clock: local_time
```

Obsługiwane źródła ceny energii:

- `regulated`: automatyczna cena sprzedawcy z urzędu z arkusza URE;
- `tauron_g13s`: automatyczna cena aktualnej oferty G13s TAURON;
- `custom`: ceny brutto wpisane w `custom_prices`.

Dla starszego licznika pracującego przez cały rok według czasu zimowego ustaw
`meter_clock: fixed_winter_time`. ENEA G12 może dodatkowo używać własnych
przedziałów, np. `day_hours: "6-13,15-22"`.

## HTTP

| Zasób | Zawartość |
|---|---|
| `/health` | stan procesu i liczba profili |
| `/api/price` | ceny wszystkich profili |
| `/api/price/<profil>` | pełna cena i składniki jednego profilu |
| `/api/status` | stan źródeł i ostatnie błędy |

## MQTT

Usługa publikuje wiadomości retained. Dla profilu `dom` i domyślnego prefiksu
powstają między innymi:

```text
polish_energy_prices/availability
polish_energy_prices/dom/availability
polish_energy_prices/dom/state
polish_energy_prices/dom/price_gross
polish_energy_prices/dom/energy_gross
polish_energy_prices/dom/distribution_gross
polish_energy_prices/dom/zone
polish_energy_prices/dom/source_status
```

Temat `state` zawiera cały obiekt JSON. Pozostałe tematy mają pojedyncze
wartości wygodne dla kanałów openHAB. Usługa publikuje ceny po uruchomieniu, po
każdym odświeżeniu źródeł i po zmianie godziny.

Globalny temat `polish_energy_prices/availability` jest komunikatem LWT procesu.
Temat `<profil>/availability` informuje osobno, czy taryfa profilu jest nadal
ważna. Przykładowy Thing openHAB używa globalnego LWT do wykrywania awarii
kontenera, a ważność taryfy udostępnia jako osobny kanał.

## openHAB

Zainstaluj MQTT Binding, a następnie skopiuj i dostosuj pliki:

```text
openhab/polish-energy-prices.things.example
openhab/polish-energy-prices.items.example
```

W pliku Thing zmień adres brokera oraz, jeśli trzeba, `dom` na identyfikator
własnego profilu. Kanały ceny używają jednostki `PLN/kWh` i mogą zostać
połączone z Itemami `Number:EnergyPrice`.

## Cache i awarie źródeł

Ostatni poprawny zestaw danych jest przechowywany w woluminie `/data`. Chwilowa
awaria URE, OSD albo brokera MQTT nie usuwa ceny. Pole `source_status` oraz
`/api/status` pokazują użycie cache i treść ostatniego błędu. Wygasła taryfa
powoduje `available: false`; usługa nie podaje jej jako aktualnej ceny.

Obraz działa jako użytkownik o UID `10001`. Przy użyciu katalogu hosta zamiast
woluminu nazwanego nadaj mu prawo zapisu, np. `chown -R 10001:10001 ./data`.
Brak prawa zapisu wyłącza cache, ale nie zatrzymuje HTTP ani MQTT.
