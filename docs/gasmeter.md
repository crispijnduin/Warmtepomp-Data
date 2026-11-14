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
| `stand_m3` | de volledige stand in m³, decimalen met een punt |
| `bron` | hoe de stand is vastgesteld: `foto`, `handmatig`, `jaarnota` |
| `notitie` | vrij veld, mag leeg blijven |

Eén rij per opname, oplopend op datum. De stand zelf wordt bewaard, niet het
verbruik: verbruik is een verschil tussen twee rijen en kan altijd opnieuw
uitgerekend worden, een overgeschreven stand niet.

## Verbruik tussen twee opnames

```bash
python3 scripts/gasverbruik.py
```

Geeft per interval het verbruik, het aantal dagen en het gemiddelde per dag,
plus een totaalregel over alle opnames samen.

## Waarom niet in de database

`warmtepomp.sqlite` bevat wat Home Assistant zelf heeft gezien. Deze standen
komen van een meter zonder enige koppeling: ze worden met de hand afgelezen en
horen daarom bij de broncode, in git, met versiegeschiedenis. Wie ze samen met
de pompdata wil analyseren, leest het CSV-bestand ernaast in.
