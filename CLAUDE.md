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

## Gasmeterstanden

De gebruiker geeft meterstanden door in het gesprek, vaak als foto. Zet elke
stand als nieuwe regel in `data/gasmeter.csv` en commit die. Zie
`docs/gasmeter.md` voor het aflezen en de kolommen. Reken geen verbruik in het
bestand uit; dat doet `scripts/gasverbruik.py`.

Noemt de gebruiker geen datum of tijd, neem dan het moment van uploaden: de
klok nu, omgerekend naar Europe/Amsterdam (`TZ=Europe/Amsterdam date
'+%Y-%m-%d %H:%M'`). Een geüploade foto heeft geen EXIF meer om uit te lezen,
die wordt bij het uploaden gestript. Noemt de gebruiker wél een datum of tijd,
dan gaat die voor. Zet het gekozen tijdstip in je antwoord, zodat een
vergissing opvalt.

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
