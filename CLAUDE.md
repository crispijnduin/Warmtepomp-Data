# Context bij dit repo

Aantekeningen voor wie hier verder werkt, menselijk of niet.

## De installatie waar dit voor gebouwd is

| | |
|---|---|
| Warmtepomp | Weheat Flint P40, via de Weheat-cloud |
| Thermostaat | Evohome, via YAML geconfigureerd (staat dus **niet** bij de config entries) |
| Home Assistant | 2026.11 op Home Assistant OS |
| App-slug | `warmtepomp_data`, geïnstalleerd als app-repository vanuit GitHub |
| Database | `/share/warmtepomp/warmtepomp.sqlite` |

## Conventies

**De Home Assistant-interface van deze gebruiker staat in het Engels.** Noem
menu's dus bij hun Engelse naam: *Settings → Add-ons*, *Developer tools →
Actions*, *Repositories*, *Integrations*, *Extra entities*. De omringende tekst
blijft Nederlands.

**Versienummer en changelog horen bij elkaar.** `warmtepomp_data/config.yaml`
moet de bovenste uitgave in `CHANGELOG.md` noemen, anders biedt Home Assistant
de update niet aan. Er is een test die dit bewaakt.

**Dit repo is openbaar.** Zet er geen persoonsgegevens in: geen e-mailadressen,
geen tokens, ook niet in commit-berichten.

## De database bevragen zonder terminal

De app luistert op `logbook_entry`-events. Een leesvraag stel je zo:

```yaml
action: logbook.log
data:
  name: warmtepomp-query
  message: SELECT COUNT(*) FROM readings
```

Het antwoord verschijnt in de attributen van `sensor.warmtepomp_query_result`.
Alleen SELECT en WITH worden uitgevoerd.

**Een tijdvenster moet een `T` hebben.** `datetime('now','-48 hours')` levert
`2026-11-11 15:08:04`, met een spatie, terwijl `readings.ts` een ISO-tijdstempel
met een `T` is. SQLite vergelijkt die twee als tekst, en daarin is `'...T02:00'`
groter dan `'... 15:08'`. Het venster rekt dan stil op tot het begin van die
dag: een vraag over 48 uur leverde in de praktijk 63 uur op, zonder foutmelding.
Neem dus een grens in hetzelfde formaat:

```sql
WHERE ts > strftime('%Y-%m-%dT%H:%M:%S','now','-48 hours')
```

Een rekensom die over de hele periode hoort te kloppen — de som van de
bedrijfsstanden in minuten, bijvoorbeeld — verraadt zo'n opgerekt venster
meteen. De code in `analyse.py` geeft zijn grenzen expliciet mee en heeft dit
probleem niet.

## Dingen die in de praktijk misgingen

**De velden voor een eigen URL en token horen leeg te blijven.** Een
wachtwoordmanager vulde ze ooit met een e-mailadres, waarna de app bij elke
start omviel. Sinds 1.2.2 wordt een onbruikbare URL genegeerd en draait de app
door via de Supervisor.

**De COP-sensor is onbruikbaar als gemiddelde.** Hij staat op 0 in standby en
wordt negatief tijdens ontdooien, tot −12. Reken de COP altijd uit de
energietellers over een periode, niet uit deze sensor.

**De buitentemperatuursensor zit in de buitenunit** en loopt op door zon op de
behuizing: op een novemberdag van 11 °C gaf hij 30 °C aan. Voor
weersafhankelijke analyse hoort daar een losse sensor bij.

**Home Assistant bewaart geen statistiek van climate-entiteiten.** De
kamertemperatuur is een attribuut, geen state. Daarom worden gevolgde numerieke
attributen als eigen meetreeks opgeslagen, onder `entity_id#attribuut`.

**De Evohome-integratie belast de Honeywell-API.** Standaard elke 300 s, met een
minimum van 60 s, plus één schema-ophaal per zone per uur. Loopt die server
tegen een limiet aan, dan is `scan_interval` in `configuration.yaml` de enige
knop. De capture zelf doet geen enkel verzoek aan Honeywell.
