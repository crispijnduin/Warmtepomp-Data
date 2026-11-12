# Warmtepomp Data

Legt alle beschikbare data van de warmtepomp vast in een eigen database, op
volle resolutie en zonder bewaartermijn.

## Waarom

Home Assistant gooit losse metingen na ongeveer tien dagen weg en houdt alleen
uurgemiddelden over. Tekstuele sensoren — de bedrijfsmodus, de reden waarom
koelen geblokkeerd is, storingscodes — worden helemaal niet langdurig bewaard.
Juist het gedrag binnen een uur (starten, moduleren, ontdooien) laat zien of de
installatie goed staat ingeregeld.

## Installeren

1. Zet de app aan en laat de instellingen zoals ze staan als je een
   Weheat-warmtepomp hebt. Een token is niet nodig.
2. Start de app en kijk in het logboek. Daar staat hoeveel entiteiten zijn
   gevonden en hoeveel historie is overgenomen.

## Instellingen

**Integraties** — van welke integraties alles wordt vastgelegd. Voor Weheat is
`weheat` genoeg. Komma's ertussen voor meerdere.

**Apparaten** — gebruik dit als de warmtepomp niet via een eigen integratie
binnenkomt maar bijvoorbeeld via Modbus of MQTT. Vul de naam van het apparaat in
zoals die in Home Assistant staat.

**Losse entiteiten** — entiteiten die er inhoudelijk bij horen. Een eigen
buitentemperatuursensor is sterk aan te raden: de sensor in de buitenunit loopt
op door zon op de behuizing en meet dan veel te hoog.

**Snapshot-interval** — hoe vaak de volledige stand wordt opgehaald als vangnet
voor gemiste wijzigingen. 300 seconden is ruim genoeg.

**Historie ophalen** — hoeveel dagen bestaande historie bij het starten uit Home
Assistant wordt overgenomen. Laat dit op 400 staan bij de eerste start; daarna
maakt het niets meer uit, want dubbele metingen worden genegeerd.

**Eigen Home Assistant-URL en token** — normaal leeg laten. De app praat via de
Supervisor met Home Assistant en heeft geen token nodig. Werkt dat onverhoopt
niet, vul dan beide in: de URL van Home Assistant en een langlevend token dat je
onderaan je profielpagina aanmaakt.

## De data opvragen zonder terminal

De database staat in de container, maar de inhoud is bereikbaar via de gewone
Home Assistant-API. Twee wegen.

**Samenvatting als sensoren.** De app publiceert elke vijf minuten wat Home
Assistant zelf niet bewaart:

| Sensor | Wat het zegt |
|---|---|
| `sensor.warmtepomp_compressor_starts_1h` | hoe vaak de compressor aansloeg; hoog betekent pendelen |
| `sensor.warmtepomp_minutes_heating_1h` | minuten verwarmen in het afgelopen uur |
| `sensor.warmtepomp_minutes_defrosting_1h` | minuten ontdooien |
| `sensor.warmtepomp_minutes_standby_1h` | minuten stilstand |
| `sensor.warmtepomp_mode_last_hour` | de hoofdmodus, met de hele verdeling als attribuut |
| `sensor.warmtepomp_capture_readings` | hoeveel er is vastgelegd en hoe vers het is |

Deze krijgen een state_class, dus de recorder bewaart ze als
langetermijnstatistiek. Over een jaar zijn ze nog opvraagbaar, ook al zijn de
losse metingen waaruit ze zijn berekend dan allang opgeruimd.

**Zelf een vraag stellen.** Roep de actie `logbook.log` aan met de naam
`warmtepomp-query` en een SELECT als bericht:

```yaml
action: logbook.log
data:
  name: warmtepomp-query
  message: >-
    SELECT substr(ts,1,10) AS dag, COUNT(*) AS metingen
    FROM readings GROUP BY dag ORDER BY dag DESC
```

Het antwoord verschijnt binnen een paar seconden in
`sensor.warmtepomp_query_result`: de kolommen en rijen staan in de attributen.
Alleen SELECT en WITH worden uitgevoerd; alles wat zou wijzigen wordt geweigerd
en de reden staat in het attribuut `error`.

## Thermostaten en andere attribuut-sensoren

Bij sommige entiteiten zit de interessante waarde niet in de state maar in een
attribuut. Een thermostaatzone heeft als state `heat` of `off`; de gemeten
kamertemperatuur staat in `current_temperature`. Home Assistant bewaart daar
geen statistiek van, dus die reeks is na de bewaartermijn onherroepelijk weg.

Deze app legt zulke attributen vast als eigen meetreeks, onder de naam
`climate.woonkamer#current_temperature`. Daarmee staan ze in de export netjes
als kolom naast de aanvoertemperatuur van de warmtepomp, en zijn ze gewoon met
SQL te bevragen.

Zet de thermostaat-integratie daarvoor bij **Integraties**, bijvoorbeeld
`weheat,evohome`. Bij het eerstvolgende starten haalt de backfill ook de
historie op die Home Assistant nog heeft.

## Waar staat de data

In `/share/warmtepomp/warmtepomp.sqlite`. Dat is één bestand dat meegaat in de
gewone Home Assistant-back-up en dat je met elk SQLite-programma kunt openen.

Exports komen in `/share/warmtepomp/export/`.

## Wat er in het logboek hoort te staan

Bij een goede start:

```
Warmtepomp Data start; database: /share/warmtepomp/warmtepomp.sqlite
Verbonden met Home Assistant 2026.11.0
Discovery: 36 entiteiten gevonden over 1 apparaten
Backfill bij starten: 12345 statistiekrijen, 67890 meetwaarden
```

Staat er `Geen entiteiten gevonden`, dan klopt de instelling bij Integraties
niet. Kijk in Home Assistant bij Instellingen, Apparaten en diensten welke
integratie je warmtepomp levert.

## Twee dingen over deze data

**De COP-sensor wordt negatief.** Tijdens ontdooien draait de cyclus om: de pomp
haalt warmte uit het cv-water. Waarden tot −12 zijn normaal, en in standby staat
de sensor op 0. Een gemiddelde daarover zegt niets. Reken de COP uit de
energietellers: warmte gedeeld door stroom over dezelfde periode.

**De buitentemperatuursensor loopt op.** Op een novemberdag van 11 °C gaf hij
30 °C aan, door zon op de behuizing. Voeg voor weersafhankelijke analyse een
losse buitensensor toe onder Losse entiteiten.

De app slaat alles onbewerkt op. Opschonen is een keuze bij de analyse, niet bij
het vastleggen.
