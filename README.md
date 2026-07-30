# Polish Energy Price dla Home Assistant

Natywna integracja Home Assistant obliczająca **bieżącą cenę brutto 1 kWh** dla
gospodarstw domowych przyłączonych do jednego z pięciu największych OSD:
TAURON, PGE, ENERGA, Stoen i ENEA. Encja ma jednostkę `PLN/kWh` i może zostać
wskazana w panelu Energia jako **encja z bieżącą ceną**.

Integracja działa całkowicie lokalnie. Łączy cenę energii czynnej sprzedawcy
z urzędu z właściwym dla aktualnej godziny składnikiem sieciowym, opłatą
jakościową, OZE, kogeneracyjną i VAT-em.

> [!IMPORTANT]
> Wbudowane stawki obowiązują od **1 stycznia do 31 grudnia 2026 r.** Po tej
> dacie sensor celowo przejdzie w stan niedostępny, aby nie naliczać kosztów na
> podstawie wygasłego cennika.

## Obsługiwane taryfy

| OSD | Sprzedawca z urzędu | Taryfy z kompletną ceną energii |
|---|---|---|
| TAURON Dystrybucja | TAURON Sprzedaż | G11, G12, G12w, G13 |
| PGE Dystrybucja | PGE Obrót | G11, G12, G12w, G12n |
| ENERGA-OPERATOR | ENERGA-OBRÓT | G11, G12, G12w, G12r |
| Stoen Operator | E.ON Polska | G11, G12, G12w, G12as |
| ENEA Operator | ENEA S.A. | G11, G12, G12w |

Lista w formularzu zależy od wybranego OSD. Integracja nie pokazuje taryf, dla
których w materiałach nie ma kompletnej, regulowanej ceny energii.

## Instalacja

### HACS (repozytorium niestandardowe)

1. W HACS otwórz **Integracje** → menu → **Niestandardowe repozytoria**.
2. Dodaj `https://github.com/jchrzaniuk/PolishEnergyPrices` i wybierz kategorię
   **Integracja**.
3. Zainstaluj „Polish Energy Price” i uruchom Home Assistant ponownie.

### Ręcznie

Skopiuj katalog `custom_components/polish_energy_price` do katalogu
`config/custom_components/` swojej instalacji Home Assistant, a następnie
uruchom Home Assistant ponownie.

## Konfiguracja

1. Otwórz **Ustawienia → Urządzenia i usługi → Dodaj integrację**.
2. Wyszukaj „Polish Energy Price”.
3. Wybierz OSD i grupę taryfową z faktury.
4. Dla nowego licznika zdalnego wybierz `czas lokalny (licznik AMI)`.
5. Wybierz źródło ceny energii:
   - cennik regulowany — jeżeli masz sprzedawcę z urzędu z tabeli powyżej;
   - własne ceny brutto — jeżeli zmieniłeś sprzedawcę lub ofertę.

Dla ENEA G12 sprawdź godziny na umowie albo liczniku. Taryfa określa liczbę
godzin, ale konkretne przedziały ustala operator; domyślne `6-13,15-22` można
zmienić podczas konfiguracji albo później w opcjach integracji.

## Panel Energia

W konfiguracji źródła energii z sieci:

1. wskaż swój licznik zużycia energii (`kWh`),
2. w sekcji kosztu wybierz **Użyj encji z bieżącą ceną**,
3. wybierz sensor `Cena energii brutto` utworzony przez integrację.

Sensor ma `state_class: measurement`, jednostkę `PLN/kWh` i nie ma klasy
urządzenia `monetary`, ponieważ Home Assistant wymaga dla niej samego kodu
waluty (`PLN`), a nie ceny za kWh. Jest to ten sam wzorzec, którego używa
oficjalna integracja Nord Pool.

## Co dokładnie zawiera cena

Stan encji to koszt krańcowy:

```text
energia czynna brutto
+ (sieć zmienna + jakościowa + OZE + kogeneracyjna) × 1,23
```

Atrybuty encji pokazują aktywną strefę i rozbicie kwoty. Cena nie obejmuje opłat
miesięcznych: składnika stałego sieciowego, abonamentu, opłaty handlowej ani
ryczałtowej opłaty mocowej gospodarstwa domowego. Tych opłat nie da się uczciwie
przeliczyć na bieżącą cenę jednej dodatkowej kWh bez założenia miesięcznego
zużycia.

## Źródła danych

- taryfy dystrybucyjne pięciu OSD zatwierdzone decyzjami Prezesa URE z
  17.12.2025 r.;
- cenniki grup G na 2026 r.: TAURON Sprzedaż, PGE Obrót, ENERGA-OBRÓT,
  ENEA S.A. i E.ON Polska;
- przygotowany zbiór `taryfy_osd_2026.json` i dokumenty w
  `HEMS/dokumentacja_techniczna/taryfy_osd`;
- stawki wspólne netto: jakościowa `0,0332`, OZE `0,0073` i kogeneracyjna
  `0,0030 PLN/kWh`.

Home Assistant opisuje [encje sensorów](https://developers.home-assistant.io/docs/core/entity/sensor/)
i [konfigurację taryf w panelu Energia](https://www.home-assistant.io/docs/energy/electricity-grid/)
w oficjalnej dokumentacji.

## Testy

Testy nie wymagają instalacji Home Assistant:

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q custom_components tests
```

Sprawdzane jest m.in. pokrycie każdej godziny 2026 r. dla wszystkich 19 taryf,
zmiany sezonowe, weekendy, święta (w tym Wigilia), własne godziny ENEA G12 oraz
licznik pracujący stale według czasu zimowego.
