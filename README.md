# Polish Energy Prices dla Home Assistant

Natywna integracja Home Assistant obliczająca **bieżącą cenę brutto 1 kWh** dla
gospodarstw domowych przyłączonych do jednego z pięciu największych OSD:
TAURON, PGE, ENERGA, Stoen i ENEA. Encja ma jednostkę `PLN/kWh` i może zostać
wskazana w panelu Energia jako **encja z bieżącą ceną**.

Integracja łączy cenę energii czynnej sprzedawcy z urzędu z właściwym dla
aktualnej godziny składnikiem sieciowym, opłatą jakościową, OZE,
kogeneracyjną i VAT-em. Co 12 godzin sprawdza cenę sprzedaży energii oraz
urzędowe źródła wszystkich zmiennych składników rachunku. Ostatni poprawny,
kompletny zestaw jest zapisywany lokalnie; przy awarii źródła używany jest
cache, a przy pierwszym uruchomieniu — zweryfikowane stawki wbudowane.

> [!IMPORTANT]
> Stawki wbudowane są bezpiecznym punktem startowym na okres **1.01–31.12.2026**,
> ale nie są już jedynym źródłem. Integracja wykrywa dokument na bieżący rok,
> sprawdza kompletność wszystkich stref i dopiero wtedy aktywuje nowy zestaw.
> Jeżeli nowa taryfa nie zostanie opublikowana albo zmieni się układ dokumentu,
> wygasły zestaw nie jest używany po swojej dacie końcowej — sensor staje się
> niedostępny i pokazuje błąd źródła w atrybutach.

## Obsługiwane taryfy

| OSD | Sprzedawca z urzędu | Taryfy z kompletną ceną energii |
|---|---|---|
| TAURON Dystrybucja | TAURON Sprzedaż | G11, G12, G12w, G13, G13s |
| PGE Dystrybucja | PGE Obrót | G11, G12, G12w, G12n |
| ENERGA-OPERATOR | ENERGA-OBRÓT | G11, G12, G12w, G12r |
| Stoen Operator | E.ON Polska | G11, G12, G12w, G12as |
| ENEA Operator | ENEA S.A. | G11, G12, G12w |

Lista w formularzu zależy od wybranego OSD. Dla G13s cena energii pochodzi z
cennika ofertowego TAURON, a nie z taryfy sprzedawcy z urzędu w arkuszu URE.

## Instalacja

### HACS (repozytorium niestandardowe)

1. W HACS otwórz **Integracje** → menu → **Niestandardowe repozytoria**.
2. Dodaj `https://github.com/jchrzaniuk/PolishEnergyPrices` i wybierz kategorię
   **Integracja**.
3. Zainstaluj „Polish Energy Prices” i uruchom Home Assistant ponownie.

### Ręcznie

Skopiuj katalog `custom_components/polish_energy_price` do katalogu
`config/custom_components/` swojej instalacji Home Assistant, a następnie
uruchom Home Assistant ponownie.

## Konfiguracja

1. Otwórz **Ustawienia → Urządzenia i usługi → Dodaj integrację**.
2. Wyszukaj „Polish Energy Prices”.
3. Wybierz OSD i grupę taryfową z faktury.
4. Dla nowego licznika zdalnego wybierz `czas lokalny (licznik AMI)`.
5. Wybierz źródło ceny energii:
   - automatyczne ceny URE — jeżeli masz sprzedawcę z urzędu z tabeli powyżej;
   - własne ceny brutto — jeżeli zmieniłeś sprzedawcę lub ofertę.

### TAURON G13s

G13s rozróżnia lato i zimę, dzień roboczy i wolny oraz trzy strefy w każdym z
tych okresów. Integracja uwzględnia wszystkie 12 kombinacji stawek, polskie
święta ustawowe i następujące godziny opisane przez
[TAURON Dystrybucja](https://www.tauron.pl/tauron/tauron-dystrybucja%2C-d-%2Cpl/grupa-taryfowa-g13s):

| Okres | Dzienna pozaszczytowa | Dzienna szczytowa | Nocna |
|---|---|---|---|
| 1.04–30.09 | 9:00–17:00 | 7:00–9:00, 17:00–21:00 | 21:00–7:00 |
| 1.10–31.03 | 10:00–15:00 | 7:00–10:00, 15:00–21:00 | 21:00–7:00 |

W formularzu G13s są dwa źródła ceny energii:

- **mój cennik** — bezpieczny wybór dla trwającej umowy; należy przepisać 12
  cen brutto z własnego cennika TAURON;
- **najnowsza oferta G13s TAURON** — integracja co 12 godzin odczytuje tabelę
  cen z [oficjalnej strony produktu](https://www.tauron.pl/dla-domu/prad/prad-z-usluga/tanie-godziny).

TAURON gwarantuje stawki G13s przez okres obowiązywania konkretnego cennika.
Publikacja nowszej oferty dla nowych umów nie zmienia automatycznie ceny już
zawartej umowy, dlatego źródło automatyczne wybieraj tylko wtedy, gdy nazwa i
stawki aktualnej oferty odpowiadają Twojemu cennikowi.

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

Home Assistant pokazuje w oknie szczegółów sensora liczbowego wykres i nie
pozwala integracji backendowej zastąpić go własną tabelą. Dlatego integracja
tworzy na tym samym urządzeniu osobne polskie encje diagnostyczne:

- Energia czynna brutto;
- Składnik sieciowy brutto;
- Opłata jakościowa brutto;
- Opłata OZE brutto;
- Opłata kogeneracyjna brutto;
- Dystrybucja brutto;
- Akcyza netto;
- VAT łącznie;
- Cena łączna netto.

Pierwszych pięć składników brutto sumuje się dokładnie do stanu encji **Cena
energii brutto**. Wszystkie encje mają jednostkę `PLN/kWh` i są widoczne w
sekcji diagnostycznej urządzenia utworzonego przez integrację.

Aby stale widzieć całe rozbicie, dodaj do pulpitu zwykłą kartę **Encje**, a
następnie wybierz główną cenę i wymienione wyżej składniki. Nie wymaga to HACS,
karty niestandardowej ani ręcznych sensorów szablonowych.

### Liczniki udostępniające tylko statystyki zewnętrzne

Niektóre importery danych — w szczególności godzinowy importer TAURON AMIplus —
udostępniają zużycie bez encji sensora, jako identyfikatory statystyk zewnętrznych.
Home Assistant nie pozwala przypisać do nich encji z bieżącą ceną. Wymaga osobnej,
narastającej statystyki kosztu w walucie ustawionej w Home Assistant.

#### Wymagania dla statystyki źródłowej

Każda wskazana statystyka zużycia musi:

- przechowywać energię, a nie moc chwilową;
- mieć jednostkę zgodną z energią, np. `kWh`;
- zawierać narastającą sumę (`has_sum: true`);
- odpowiadać dokładnie jednej strefie taryfowej; tej samej statystyki nie można
  przypisać do dwóch stref.

Identyfikatory i metadane można sprawdzić w **Narzędzia deweloperskie →
Statystyki**. Wybieraj statystyki poboru (`consumption`), a nie oddawania energii
do sieci (`generation`).

#### Tworzenie statystyk kosztu

1. Otwórz **Ustawienia → Urządzenia i usługi → Polish Energy Prices →
   Konfiguruj**.
2. Włącz **Utwórz koszty dla zewnętrznych statystyk zużycia** i przejdź dalej.
3. Dla każdej strefy wybierz odpowiadającą jej narastającą statystykę zużycia.
   W taryfie jednostrefowej będzie to jedno pole, a w taryfach dwu- i
   trójstrefowych odpowiednio dwa lub trzy pola.
4. Zapisz ustawienia. Pierwsze przeliczenie historii może potrwać kilka minut,
   zależnie od liczby rekordów w bazie rejestratora.
5. W **Narzędzia deweloperskie → Statystyki** poczekaj na nowe identyfikatory:

   ```text
   polish_energy_price:<id_wpisu>_cost_<strefa>
   ```

   Każda z nich ma jednostkę waluty, np. `PLN`, oraz narastającą sumę kosztu.

Przykład mapowania dla TAURON G13:

| Rejestr/importer | Strefa w Polish Energy Prices | Wynikowa statystyka kosztu |
|---|---|---|
| T1 — szczyt przedpołudniowy | `szczyt_przedpoludniowy` | `…_cost_szczyt_przedpoludniowy` |
| T2 — szczyt popołudniowy | `szczyt_popoludniowy` | `…_cost_szczyt_popoludniowy` |
| T3 — pozostałe godziny | `pozostale` | `…_cost_pozostale` |

#### Przypisanie kosztu w panelu Energia

Dla każdej strefy dodanej jako osobne źródło energii z sieci:

1. pozostaw zewnętrzną statystykę `consumption` jako statystykę zużycia;
2. w sposobie śledzenia kosztu wybierz **statystykę kosztu**;
3. wskaż odpowiadającą strefie statystykę
   `polish_energy_price:…_cost_…`;
4. nie wybieraj sensora `Cena energii brutto` jako statystyki kosztu — ma on
   jednostkę `PLN/kWh`, a koszt musi być narastającą wartością w `PLN`;
5. zapisz konfigurację i sprawdź, czy panel Energia nie zgłasza błędów dla tego
   źródła.

Most aktualizuje koszt co godzinę i ponownie analizuje ostatnie 7 dni, dzięki
czemu uwzględnia opóźnione importy. Po zmianie ceny lub mapowania przelicza dane
od początku okresu obowiązywania taryfy. Koszt obejmuje te same zmienne składniki
brutto co sensor ceny; nie zawiera miesięcznych opłat stałych.

Ta opcja jest odseparowana od zwykłej encji ceny. Liczniki udostępniające encję
`kWh` z `state_class: total` albo `total_increasing` — np. WM-Bus, ESPHome czy
własny podlicznik — nadal korzystają bezpośrednio z sensora bieżącej ceny i nie
wymagają włączania mostu.

Most statystyk strefowych nie jest oferowany dla G13s. W tej grupie cena zmienia
się jednocześnie według godziny, pory roku i rodzaju dnia, a importer musiałby
udostępniać godzinowe zużycie zamiast zwykłych trzech rejestrów strefowych.
Encjowy licznik `kWh` nadal działa prawidłowo z sensorem bieżącej ceny w panelu
Energia.

## Co dokładnie zawiera cena

Stan encji to koszt krańcowy:

```text
energia czynna brutto = (cena netto + 0,005 zł/kWh akcyzy) × 1,23
+ (sieć zmienna + jakościowa + OZE + kogeneracyjna) × 1,23
```

Atrybuty encji pokazują aktywną strefę, rozbicie kwoty, jawny składnik akcyzy,
adres użytego arkusza URE oraz czas ostatniej kontroli i aktualizacji. Ceny
energii w arkuszu URE są już cenami brutto „z akcyzą i VAT”, dlatego integracja
nie dolicza akcyzy drugi raz. Akcyza nie dotyczy dystrybucji.

Cena nie obejmuje opłat
miesięcznych: składnika stałego sieciowego, abonamentu, opłaty handlowej ani
ryczałtowej opłaty mocowej gospodarstwa domowego. Tych opłat nie da się uczciwie
przeliczyć na bieżącą cenę jednej dodatkowej kWh bez założenia miesięcznego
zużycia.

## Źródła danych

Automatyzacja rozdziela źródła zgodnie z tym, kto ustala daną opłatę:

- cena energii czynnej — najnowszy arkusz XLSX dla gospodarstw domowych na
  stronie URE „Masz wybór”; dla automatycznego wariantu G13s — tabela cen
  najnowszej oferty na oficjalnej stronie TAURON;
- składnik zmienny sieciowy i stawka jakościowa — aktualna taryfa lub wyciąg
  opublikowany przez właściwego OSD; dla ENEA i ENERGA używany jest również
  oficjalny serwis dokumentów ENERGA, a dla PGE jego dokument operatora;
- opłata OZE — tabela „Stawki opłaty OZE” w BIP URE;
- opłata kogeneracyjna — rozporządzenie wyszukiwane przez oficjalne API ELI i
  pobierane z Dziennika Ustaw.

Taryfa PSE nie jest źródłem ceny dla klienta końcowego. Stawka jakościowa jest
odczytywana z dokumentu OSD, który stosuje ją w rozliczeniach swoich odbiorców.

Każdy PDF musi potwierdzać właściwy rok i zawierać kompletną liczbę stref, a
wartości muszą przejść kontrolę zakresów. Zmiana adresu dokumentu nie wystarcza
do aktualizacji ceny. W atrybutach sensora dostępne są osobne adresy źródeł,
czas ostatniej kontroli, czas ostatniej poprawnej aktualizacji i ostatni błąd.

Arkusz URE zawiera wyłącznie oferty **sprzedaży energii** i nie obejmuje kosztów
dystrybucji; integracja nie przypisuje mu danych, których w nim nie ma.

Home Assistant opisuje [encje sensorów](https://developers.home-assistant.io/docs/core/entity/sensor/)
i [konfigurację taryf w panelu Energia](https://www.home-assistant.io/docs/energy/electricity-grid/)
w oficjalnej dokumentacji.

## Testy

Testy nie wymagają instalacji Home Assistant:

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q custom_components tests
```

Sprawdzane jest m.in. pokrycie każdej godziny 2026 r. dla wszystkich 20 taryf,
zmiany sezonowe, weekendy, święta (w tym Wigilia), własne godziny ENEA G12 oraz
licznik pracujący stale według czasu zimowego.
