# Warmtepomp Data

Legt **alle** beschikbare data van de warmtepomp vast in een eigen database, op
volle resolutie en zonder bewaartermijn.

Gebouwd voor de **Weheat Flint P40** die via de Weheat-cloud in Home Assistant
hangt, maar het werkt voor elke warmtepomp die in Home Assistant zichtbaar is
(Quatt, Daikin Onecta, Nibe, Modbus, MQTT): je wijst de integratie of het
apparaat aan, de rest gaat automatisch.

## Waarom dit nodig is

Home Assistant bewaart niet alles wat de warmtepomp vertelt:

| | Home Assistant | dit systeem |
|---|---|---|
| Volle resolutie (elke wijziging) | ~10 dagen, dan weg | onbeperkt |
| 5-minuutwaarden | ~10 dagen | onbeperkt |
| Uurwaarden | onbeperkt | onbeperkt |
| Niet-numerieke sensoren (bedrijfsmodus, storingsoorzaak) | geen langetermijnopslag | onbeperkt |
| Attributen (de lijst mogelijke toestanden) | alleen de huidige | elke wijziging |

De recorder van Home Assistant gooit na tien dagen de losse metingen weg en
houdt alleen uurgemiddelden over. Voor een warmtepomp is dat jammer: juist het
gedrag binnen een uur — starten, moduleren, ontdooien — bepaalt of de
installatie goed staat ingeregeld. En de bedrijfsmodus, de reden waarom koelen
geblokkeerd is en de storingscodes zijn tekst, dus die worden helemaal niet
bewaard.

Dit systeem abonneert zich op elke statewijziging en schrijft hem weg in SQLite.
Eén bestand, zonder server, met SQL te bevragen en als geheel te back-uppen.

## Installeren in Home Assistant (de makkelijkste weg)

Je hebt geen terminal, netwerkschijf of toegangstoken nodig. Home Assistant kan
dit repo rechtstreeks installeren.

1. Ga in Home Assistant naar **Settings → Add-ons** (op nieuwere versies heet
   dit **Apps**) en open de **store**.
2. Klik op het **menu rechtsboven (⋮) → Repositories**.
3. Plak deze URL en klik op **Toevoegen**:

   ```
   https://github.com/crispijnduin/Warmtepomp-Data
   ```

4. Sluit het venster. **Warmtepomp Data** staat nu onderaan in de store.
   Klik erop → **Installeren**.
5. Zet **Starten bij opstarten** en **Automatisch bijwerken** aan, en klik op
   **Starten**.

Heb je een Weheat-warmtepomp? Dan hoef je niets in te stellen. Kijk na het
starten op het tabblad **Logboek**; daar hoort te staan hoeveel entiteiten zijn
gevonden en hoeveel historie is overgenomen.

De database komt in `/share/warmtepomp/warmtepomp.sqlite` en gaat mee in je
gewone Home Assistant-back-up.

## Zelf draaien vanaf de opdrachtregel

Alleen nodig als je het buiten Home Assistant wilt gebruiken.

```bash
git clone https://github.com/crispijnduin/Warmtepomp-Data.git
cd Warmtepomp-Data/warmtepomp_data
python3 -m venv .venv && . .venv/bin/activate
pip install -e .

cp ../warmtepomp.yaml.example warmtepomp.yaml
# Vul ha_url in. Maak een token in Home Assistant: je profielpagina,
# onderaan, "Langlevende toegangstokens".
export WARMTEPOMP_HA_TOKEN='...'

warmtepomp check      # staat de verbinding?
warmtepomp discover   # wat wordt er vastgelegd?
warmtepomp run        # vastleggen, blijft draaien
```

`run` haalt eerst de bestaande historie uit Home Assistant op en gaat daarna
live verder. Stoppen met Ctrl-C; de laatste metingen worden nog weggeschreven.

## Commando's

| Commando | Wat het doet |
|---|---|
| `warmtepomp check` | test de verbinding en het token |
| `warmtepomp discover` | toont welke entiteiten worden vastgelegd |
| `warmtepomp run` | legt continu alles vast (de hoofdtaak) |
| `warmtepomp run --once` | één snapshot, bijvoorbeeld vanuit cron |
| `warmtepomp backfill` | haalt bestaande historie uit Home Assistant |
| `warmtepomp status` | wat zit er in de database (`--per-entity` voor detail) |
| `warmtepomp report` | energie, COP en SCOP per dag |
| `warmtepomp export` | schrijft CSV (en Parquet met `--parquet`) |
| `warmtepomp query '<SQL>'` | eigen SELECT op de database |

Elk commando kent `--json` voor machineleesbare uitvoer.

## Wat er wordt vastgelegd

Voor de Flint P40 zijn dat 36 entiteiten. Van elke entiteit gaat elke wijziging
de database in, met tijdstempel, numerieke waarde én de ruwe tekst:

* **energie** — stroomverbruik totaal en per functie (verwarmen, koelen,
  ontdooien, standby, binnenunit), warmteopbrengst totaal en per functie;
* **vermogen en rendement** — ingaand vermogen, uitgaand vermogen, momentane COP;
* **temperaturen** — water in/uit, cv-retour, doeltemperatuur, buitenlucht,
  kamertemperatuur en setpoint;
* **compressor en pomp** — toerental, belasting, cv-pompdebiet;
* **toestanden** — bedrijfsmodus (11 mogelijke waarden), koelstatus, waarom
  koelen geblokkeerd is, pauze- en stopredenen;
* **schakelaars** — waterpomp, hulppomp, tapwaterventiel, elektrisch
  bijverwarmingselement, of de gasketel mag bijstoken.

De volledige lijst met eenheden staat in [docs/entiteiten.md](docs/entiteiten.md).

Nieuwe sensoren hoef je niet toe te voegen: het systeem kijkt elk uur opnieuw
welke entiteiten bij de warmtepomp horen en neemt wat erbij komt automatisch mee.

## Hoe het werkt

```
Home Assistant ──WebSocket (state_changed)──┐
                                            ├──► SQLite ──► CSV / Parquet
Home Assistant ──REST + recorder-statistiek─┘
```

* **Event-gestuurd.** Een abonnement op `state_changed` levert elke wijziging op
  het moment dat hij gebeurt. Geen pollinterval, dus geen gemiste pieken.
* **Snapshot als vangnet.** Elke vijf minuten wordt de volledige stand
  opgehaald. Dat vangt op wat tijdens een herverbinding is gemist. Dubbele
  tijdstempels worden genegeerd, dus het kost geen extra rijen.
* **Herverbinden met oplopende wachttijd.** Valt Home Assistant weg, dan wordt
  de periode zonder data als *gat* in de tabel `gaps` vastgelegd. Zo is later te
  zien dat daar echt niets was, in plaats van dat je het moet gokken.
* **Idempotent.** Alles schrijft met de sleutel (entiteit, tijdstip). Backfillen
  of herstarten levert nooit dubbele metingen op.
* **Niets wordt weggegooid.** De capture slaat op wat Home Assistant levert,
  inclusief `unavailable` en onwaarschijnlijke waarden. Opschonen is een keuze
  bij de analyse, niet bij het vastleggen.

## Andere manieren om het te laten draaien

### Met Docker

```bash
cp .env.example .env     # vul HA_URL en HA_TOKEN in
docker compose up -d
docker compose exec warmtepomp warmtepomp status
```

### Met systemd

Zie de instructies boven in
[`deploy/systemd/warmtepomp.service`](deploy/systemd/warmtepomp.service). De
bijgeleverde timer exporteert elke nacht naar CSV.

## De data opvragen vanuit Home Assistant

Draait het als app, dan is de database bereikbaar zonder terminal.

De app publiceert zelf terug wat Home Assistant niet kan bewaren —
`sensor.warmtepomp_compressor_starts_1h` (pendelt de pomp?), de minuten per
bedrijfsmodus, en de staat van de capture. Die krijgen een `state_class`, dus
de recorder bewaart ze als langetermijnstatistiek en ze blijven jaren
opvraagbaar.

Een eigen vraag stel je met de actie `logbook.log`:

```yaml
action: logbook.log
data:
  name: warmtepomp-query
  message: SELECT COUNT(*) FROM readings
```

Het antwoord komt in `sensor.warmtepomp_query_result`, met de rijen in de
attributen. Alleen SELECT en WITH worden uitgevoerd.

## De data gebruiken

Direct met SQL:

```bash
# Hoeveel stookte de pomp per uur, en bij welke buitentemperatuur?
warmtepomp query "
  SELECT substr(ts,1,13) AS uur,
         ROUND(AVG(CASE WHEN entity_id LIKE '%_input_power'  THEN value_num END)) AS watt_in,
         ROUND(AVG(CASE WHEN entity_id LIKE '%_output_power' THEN value_num END)) AS watt_uit,
         ROUND(AVG(CASE WHEN entity_id LIKE '%_outside_temperature' THEN value_num END),1) AS buiten
  FROM readings WHERE value_num IS NOT NULL
  GROUP BY uur ORDER BY uur DESC" --limit 24
```

Of exporteren en elders verder:

```bash
warmtepomp export --what wide --resample 1min --parquet
```

De brede tabel heeft één rij per tijdstap en één kolom per sensor — direct
inleesbaar in pandas, Excel of Grafana.

```python
import pandas as pd
df = pd.read_csv("data/export/wide_1min.csv", index_col="ts", parse_dates=True)
```

## Twee dingen die je over deze data moet weten

Uit de eerste week meetdata van een echte Flint P40:

**De COP-sensor geeft negatieve waarden.** Tijdens ontdooien draait de cyclus
om: de pomp haalt warmte uit het cv-water om het buitendeel ijsvrij te maken.
Waarden tot −12 zijn normaal. In standby staat de COP op 0. Een gemiddelde over
die momentwaarden is daarom zinloos. Reken de COP altijd uit de energietellers:
warmte gedeeld door stroom over dezelfde periode. `warmtepomp report` doet dat zo,
en `--cop-curve` ook.

**De buitentemperatuursensor loopt op.** Hij zit in de buitenunit en meet in de
zon of na een ontdooicyclus veel te hoog — op een novemberdag met 11 °C gaf hij
30 °C aan. Voor weersafhankelijke analyse hoort daar een losse buitensensor bij;
voeg die toe onder `extra_entities`.

`warmtepomp report` meldt zulke dingen zelf, inclusief sensoren die stil zijn
gevallen en perioden waarin de verbinding wegviel.

## Opslaggrootte

Gemeten op een draaiende installatie met 54 reeksen (een Weheat Flint P40 plus
vijf Evohome-zones):

| | |
|---|---|
| Metingen | 265 per uur, 6.350 per dag |
| Per rij | ~173 bytes, inclusief index |
| Groei | **ongeveer 0,5 GB per jaar** |

Dat is minder dan je zou denken, om twee redenen: er wordt alleen weggeschreven
als een waarde verandert, en de meeste sensoren verversen elke twee minuten,
niet elke seconde. De groei schaalt mee met het aantal entiteiten en hoe vaak
ze veranderen, dus een snelle energiemeter erbij tikt harder aan dan tien trage
temperatuursensoren.

Op een gewone Home Assistant-machine is dit geen punt: met 10 GB vrij duurt het
ruim twintig jaar voor het vol is. Toch minder nodig? Zet
`store_attributes: false` en verhoog `snapshot_interval`.

## Ontwikkelen

```bash
cd warmtepomp_data
pip install -e '.[dev]'
pytest                    # 99 tests, waaronder een nep-Home Assistant
```

De tests draaien een echte WebSocket-server die Home Assistant naspeelt, zodat
herverbinden, gemiste events en nieuwe sensoren werkelijk worden uitgeprobeerd
en niet alleen nagebootst. In `tests/data/` staan echte meetreeksen van een
Flint P40, inclusief de negatieve COP en de te hoge buitentemperatuur, als
regressietest. `tests/test_addon_manifest.py` bindt `config.yaml` aan de code,
zodat een instelling die Home Assistant aanbiedt ook echt iets doet.

## Indeling van dit repo

```
repository.yaml          <- hierdoor kan Home Assistant dit repo installeren
warmtepomp_data/         <- de app zelf
  config.yaml            <- instellingen die Home Assistant aanbiedt
  Dockerfile             <- hoe de app wordt gebouwd
  rootfs/                <- het startscript
  src/warmtepomp/        <- de code
  tests/
docs/entiteiten.md       <- alle 36 sensoren met eenheden
deploy/systemd/          <- draaien buiten Home Assistant
```

## Licentie

MIT
