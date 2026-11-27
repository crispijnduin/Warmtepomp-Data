# Gasmeterstanden

De warmtepomp vervangt gas, maar het gas is er nog. Zolang beide meelopen is de
handmatig opgenomen meterstand de enige manier om te zien wat er naast de pomp
nog verstookt wordt. Daarom staan de standen hier, met datum, in
[`../data/gasmeter.csv`](../data/gasmeter.csv).

## De meter

Een balgenmeter van het type G6. Vijf zwarte cijfers zijn hele kubieke meters,
de drie rode zijn de decimalen: `69093` en `917` lees je als `69093,917 m³`.
Het laatste wieltje draait continu mee en staat vaak tussen twee cijfers in;
neem dan het cijfer dat er volledig staat.

## De indeling van het bestand

| kolom | betekenis |
|---|---|
| `datum` | `JJJJ-MM-DD`, de dag van opnemen |
| `tijd` | `UU:MM` lokale tijd; leeg laten telt als middernacht |
| `stand_m3` | de volledige stand in m³, decimalen met een punt |
| `bron` | hoe de stand is vastgesteld: `foto`, `handmatig`, `jaarnota` |
| `notitie` | vrij veld, mag leeg blijven |

De tijd staat erbij omdat een halve dag bij dit verbruik al bijna een kuub
scheelt: zonder tijdstip wordt het dagverbruik over een korte periode een slag
in de lucht. Neem de standen daarom het liefst rond hetzelfde tijdstip op.

Wordt een stand doorgegeven zonder datum of tijd erbij, dan geldt het moment
van doorgeven, in Nederlandse tijd. Een foto helpt daar niet bij: de EXIF-tijd
overleeft het uploaden niet.

Eén rij per opname, oplopend in tijd. De stand zelf wordt bewaard, niet het
verbruik: verbruik is een verschil tussen twee rijen en kan altijd opnieuw
uitgerekend worden, een overgeschreven stand niet.

## Verbruik tussen twee opnames

```bash
python3 scripts/gasverbruik.py
```

Geeft per interval het verbruik, het aantal dagen en het gemiddelde per dag,
plus een totaalregel over alle opnames samen.

## Gas naast de warmtepomp leggen

De vraag achter deze standen is wat de pomp overneemt. Dat vergt de warmte- en
stroomtellers van de pomp over precies dezelfde perioden. `gasvraag.py` maakt
die vraag uit het CSV-bestand:

```bash
python3 scripts/gasvraag.py            # gas tegenover warmte, stroom en COP
python3 scripts/gasvraag.py --ketel    # hoe lang de gasketel mocht bijstoken
```

Het drukt een `logbook.log`-blok af dat je plakt in *Developer tools →
Actions*, YAML-modus. Het antwoord staat daarna in de attributen van
`sensor.warmtepomp_query_result`.

Het is een script en geen vastgelegde query, omdat de perioden met elke nieuwe
meterstand veranderen: opgeslagen SQL zou na de eerstvolgende opname al over de
verkeerde dagen gaan.

| kolom | betekenis |
|---|---|
| `gas_m3` | verbruik uit de meterstanden |
| `warmte_kwh` | wat de pomp aan warmte afgaf (`total_energy_output`) |
| `stroom_kwh` | wat de pomp daarvoor opnam (`electricity_used`) |
| `cop` | warmte gedeeld door stroom over de hele periode |
| `gas_kwh` | het gas omgerekend naar nuttige warmte, à 8,8 kWh/m³ |
| `pomp_pct` | aandeel van de pomp in warmte plus gas |

Twee dingen die dit cijfer niet weet. Het gas bevat ook koken en warm water,
die hier als verwarming meetellen; en 8,8 kWh/m³ is de bovenwaarde van
aardgas maal een aangenomen ketelrendement van 90 %, geen meting. Voor de
verhouding tussen de perioden maakt dat weinig uit, voor een absoluut getal
wel.

## Waarom niet in de database

`warmtepomp.sqlite` bevat wat Home Assistant zelf heeft gezien. Deze standen
komen van een meter zonder enige koppeling: ze worden met de hand afgelezen en
horen daarom bij de broncode, in git, met versiegeschiedenis. Wie ze samen met
de pompdata wil analyseren, leest het CSV-bestand ernaast in.
