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

   Publiczny obraz jest dostępny bez logowania dla architektur `amd64` i
   `arm64`. Tag `latest` wskazuje najnowszą wersję:

   ```text
   ghcr.io/jchrzaniuk/polish-energy-prices:latest
   ```

   Aby przypiąć konfigurację do konkretnego obrazu, ustaw w `compose.yaml`
   tag `sha-` ze skrótem commita, z którego obraz powstał:

   ```text
   ghcr.io/jchrzaniuk/polish-energy-prices:sha-541e5ec
   ```

   Obrazy nie są etykietowane numerem wydania, więc tag `v1.6.2` nie
   istnieje. Listę dostępnych tagów pokaże:

   ```bash
   curl -s https://ghcr.io/v2/jchrzaniuk/polish-energy-prices/tags/list \
     -H "Authorization: Bearer $(curl -s 'https://ghcr.io/token?scope=repository:jchrzaniuk/polish-energy-prices:pull' | jq -r .token)"
   ```

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
- `tauron_g14dynamic`: automatyczna cena oferty G14dynamic TAURON oraz strefy
  z Energetycznego Kompasu PSE;
- `custom`: ceny brutto wpisane w `custom_prices`.

PSE rewiduje harmonogram doby handlowej wielokrotnie, aż do wieczora danej
doby. Usługa sprawdza Energetyczny Kompas co 15 minut i podmienia strefy
doby, gdy tylko pojawi się nowszy znacznik publikacji niż zapisany;
republikacja tych samych stref nie liczy się jako zmiana. Skrócony,
15-minutowy interwał dotyczy wyłącznie profili z dynamicznym źródłem stref —
pozostałe profile w tej samej usłudze (np. URE) nadal odświeżają się zgodnie
z `refresh_interval_hours`. Dokumenty z rocznymi stawkami oraz cennik
G14dynamic nadal sprawdza co 12 godzin.

Dla starszego licznika pracującego przez cały rok według czasu zimowego ustaw
`meter_clock: fixed_winter_time`. ENEA G12 może dodatkowo używać własnych
przedziałów, np. `day_hours: "6-13,15-22"`.

Rozliczenie energii wprowadzonej do sieci (net-billing) włącza `export_settlement`:

- `off` (domyślnie): brak rozliczenia eksportu;
- `rce`: 15-minutowa rynkowa cena energii z PSE;
- `rcem`: miesięczna rynkowa cena energii z PSE.

`export_correction` to ustawowy współczynnik korygujący depozytu prosumenckiego,
w zakresie 0.5–2.0, domyślnie `1.0`. Od 1.02.2025 ustawa o OZE przewiduje
wartość `1.23`; usługa jej nie narzuca automatycznie. Integracja Home Assistant
pozwala wybrać z listy tylko `1,0` albo `1,23`; plik YAML tej usługi przyjmuje
dowolną liczbę z zakresu 0.5–2.0.

## HTTP

| Zasób | Zawartość |
|---|---|
| `/health` | stan procesu i liczba profili |
| `/api/price` | ceny wszystkich profili |
| `/api/price/<profil>` | pełna cena i składniki jednego profilu |
| `/api/forecast` | prognozy wszystkich profili |
| `/api/forecast/<profil>` | prognoza jednego profilu |
| `/api/forecast/<profil>?hours=24` | prognoza o długości od 1 do 168 godzin |
| `/api/status` | stan źródeł i ostatnie błędy |
| `/api/export` | cena eksportu i nadchodzące okresy dla wszystkich profili z włączonym rozliczeniem (blok zawiera też klucz `negative` — `true`/`false` w trybie RCE, `null` w trybie RCEm) |
| `/api/export/<profil>` | cena eksportu i nadchodzące okresy jednego profilu; `404`, jeżeli profil nie ma `export_settlement` |
| `/api/export/<profil>?hours=24` | jak wyżej, z listą okresów o długości od 1 do 168 godzin |

Endpoint prognozy nie odświeża źródeł. Korzysta ze spójnego snapshotu danych
wyliczonego po starcie, po zmianie godziny i po planowym odświeżeniu. Odpowiedź
zawiera stabilne metadane `provider`, `tariff_id`, `operator_id`,
`tariff_code`, `tariff_name` i `unit`. Pole `complete` ma wartość `false`,
jeżeli okres ważności stawek kończy się przed końcem żądanego horyzontu.

## MQTT

Usługa publikuje wiadomości retained. Dla profilu `dom` i domyślnego prefiksu
powstają między innymi:

```text
polish_energy_prices/availability
polish_energy_prices/dom/availability
polish_energy_prices/dom/state
polish_energy_prices/dom/forecast
polish_energy_prices/dom/price_gross
polish_energy_prices/dom/energy_gross
polish_energy_prices/dom/distribution_gross
polish_energy_prices/dom/zone
polish_energy_prices/dom/source_status
polish_energy_prices/dom/export
polish_energy_prices/dom/export_price_net
polish_energy_prices/dom/export_raw_net
polish_energy_prices/dom/export_period
polish_energy_prices/dom/export_settlement
polish_energy_prices/dom/export_negative
```

Temat `state` zawiera bieżący obiekt JSON. Temat `forecast` zawiera retained
prognozę 48 godzin. Pozostałe tematy mają pojedyncze wartości wygodne dla
kanałów openHAB. Usługa publikuje ceny i prognozę po uruchomieniu, po każdym
odświeżeniu źródeł i po zmianie godziny.

Temat `export` (retained) zawiera cenę eksportu wraz z nadchodzącymi okresami,
tak jak `forecast`; skalary `export_price_net`, `export_raw_net`,
`export_period` i `export_settlement` ułatwiają podłączenie kanałów openHAB.
Temat `export_negative` mówi, czy surowa cena bieżącego okresu jest ujemna
(`true`/`false` w trybie RCE, pusty łańcuch w trybie RCEm). Profile bez
włączonego rozliczenia eksportu (`export_settlement: off`) nie otrzymują
żadnego z tych tematów.

Jeżeli dane wygasną, `forecast` zostaje zastąpiony obiektem z pustą listą
slotów, `complete: false` i `source_status: expired`. Broker nie zachowuje w ten
sposób starej prognozy jako aktualnej.

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
powoduje `available: false`; usługa nie podaje jej jako aktualnej ceny. Błąd
pobrania RCE albo RCEm zachowuje ostatnie znane ceny eksportu i trafia do
`errors.export` oraz do pola `error` bloku eksportu.

Obraz działa jako użytkownik o UID `10001`. Przy użyciu katalogu hosta zamiast
woluminu nazwanego nadaj mu prawo zapisu, np. `chown -R 10001:10001 ./data`.
Brak prawa zapisu wyłącza cache, ale nie zatrzymuje HTTP ani MQTT.
