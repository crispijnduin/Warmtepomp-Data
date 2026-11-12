# Beschikbare data van de Weheat Flint P40

Dit is de volledige lijst zoals de Weheat-integratie hem op 12 november 2026
aanbood in Home Assistant 2026.11: **36 entiteiten** op één apparaat
(`Flint P40 heat pump`, fabrikant Weheat). De waarden in de kolom *voorbeeld*
komen uit een draaiende installatie, op een moment dat de pomp aan het
verwarmen was.

De lijst is bedoeld als naslag. Het systeem leest hem bij elke ronde zelf
opnieuw uit, dus je hoeft hier niets bij te werken als Weheat sensoren
toevoegt.

## Energie (tellers)

Alle tellers lopen op en worden nooit gereset; `total_energy_output` en
`energy_output_defrost` kunnen omlaag omdat er tijdens ontdooien warmte aan het
water wordt onttrokken.

| Entiteit | Eenheid | Voorbeeld | Betekenis |
|---|---|---|---|
| `..._electricity_used` | kWh | 106,88 | totale stroomopname |
| `..._electricity_used_heating` | kWh | 105,09 | stroom voor verwarmen |
| `..._electricity_used_cooling` | kWh | 0,00 | stroom voor koelen |
| `..._electricity_used_defrost` | kWh | 0,43 | stroom voor ontdooien |
| `..._electricity_used_standby` | kWh | 1,36 | stroom in standby |
| `..._electricity_used_indoor_unit` | kWh | 5,52 | stroom van de binnenunit |
| `..._total_energy_output` | kWh | 329,79 | totaal afgegeven warmte |
| `..._energy_output_heating` | kWh | 333,11 | warmte voor verwarmen |
| `..._energy_output_cooling` | kWh | 0,00 | geleverde koeling |
| `..._energy_output_defrost` | kWh | −3,31 | warmte onttrokken bij ontdooien |

De uitsplitsing per functie is waardevol: hiermee is te zien hoeveel het
standbyverbruik en het ontdooien werkelijk kosten. In de eerste week was dat
1,36 kWh standby en 0,43 kWh ontdooien op 106,88 kWh totaal.

## Vermogen en rendement

| Entiteit | Eenheid | Voorbeeld | Betekenis |
|---|---|---|---|
| `..._input_power` | W | 1172 | opgenomen elektrisch vermogen |
| `..._output_power` | W | 5128 | afgegeven thermisch vermogen |
| `..._cop` | — | 4,38 | momentane COP (output ÷ input) |

> **Let op bij de COP.** Deze sensor is 0 in standby en wordt negatief tijdens
> ontdooien (waarden tot −12 zijn normaal). Een gemiddelde over deze reeks is
> daarom niet bruikbaar. Bereken de COP uit de energietellers over een periode.

## Temperaturen

| Entiteit | Eenheid | Voorbeeld | Betekenis |
|---|---|---|---|
| `..._water_inlet_temperature` | °C | 31,1 | water dat de pomp in gaat |
| `..._water_outlet_temperature` | °C | 36,5 | water dat de pomp uit komt |
| `..._central_heating_inlet_temperature` | °C | 35,9 | retour uit de cv |
| `..._water_target_temperature` | °C | 44,4 | doeltemperatuur van de regeling |
| `..._outside_temperature` | °C | 13,2 | buitenlucht bij de buitenunit |
| `..._current_room_temperature` | °C | 20,0 | kamertemperatuur |
| `..._room_temperature_setpoint` | °C | 20,0 | gewenste kamertemperatuur |

Het verschil tussen in- en uitlaat (hier 5,4 K) maal het debiet geeft het
afgegeven vermogen; daarmee is `output_power` na te rekenen.

> **Let op bij de buitentemperatuur.** De sensor zit in de buitenunit en loopt
> op door zon op de behuizing en door ontdooicycli. Gemeten op 9 november:
> 30,3 °C, terwijl het buiten een graad of tien was. Gebruik voor
> weersafhankelijke analyse een losse buitensensor.

De laatste twee entiteiten komen van een andere integratie dan `weheat`, maar
horen bij hetzelfde apparaat. Het systeem pakt ze op omdat het niet alleen op
integratie maar ook op apparaat selecteert.

## Compressor en circulatie

| Entiteit | Eenheid | Voorbeeld | Betekenis |
|---|---|---|---|
| `..._compressor_speed` | rpm | 4740 | toerental van de compressor |
| `..._compressor_usage` | % | 87 | belasting ten opzichte van maximum |
| `..._central_heating_pump_flow` | m³/h | 0,99 | debiet door de cv |

Hiermee is modulatiegedrag te zien: een pomp die voortdurend aan- en uitslaat
in plaats van laag te moduleren, verbruikt onnodig veel.

## Bedrijfstoestanden (tekst)

Deze worden door Home Assistant **niet** in de langetermijnstatistiek bewaard.
Ze zijn een belangrijke reden om zelf vast te leggen.

| Entiteit | Voorbeeld | Mogelijke waarden |
|---|---|---|
| `sensor...._heat_pump` (bedrijfsmodus) | `heating` | `offline`, `standby`, `water_check`, `heating`, `cooling`, `dhw`, `legionella_prevention`, `defrosting`, `self_test`, `manual_control`, `updating` |
| `..._cooling_state` | `waiting` | `idle`, `starting`, `active`, `stopping`, `standby`, `pausing`, `water_check`, `standby_run_cp`, `paused`, `stopped`, `waiting` |
| `..._cooling_blocked_by` | `control_method` | `none`, `control_method`, `dtc`, `outside_air_temperature`, `inside_temperature`, `indoor_unit_connected`, `water_to_air`, `demand`, `water_temperature`, `contact_not_blocked`, `exponential_backoff`, `heat_cool_delay` |
| `..._cooling_pause_reason` | `none` | — |
| `..._cooling_stop_reason` | `none` | — |
| `..._cooling_conditions_met` | `5` (van 9) | 0 tot 9 |
| `..._cooling_wait_until` | tijdstip | — |
| `..._last_cooling` | tijdstip | — |

`cooling_blocked_by` is diagnostisch goud: het zegt precies waarom de
koelfunctie niet aanslaat.

## Schakelaars en relais

| Entiteit | Voorbeeld | Betekenis |
|---|---|---|
| `binary_sensor...._indoor_unit_water_pump` | `on` | hoofdcirculatiepomp |
| `binary_sensor...._indoor_unit_auxiliary_water_pump` | `on` | hulppomp |
| `binary_sensor...._indoor_unit_dhw_valve_or_water_pump` | `off` | tapwaterventiel |
| `binary_sensor...._indoor_unit_electric_heater` | `off` | elektrisch bijverwarmingselement |
| `binary_sensor...._gas_boiler_heating_allowed` | `off` | mag de gasketel bijstoken |

Het elektrische element en de gasketel zijn de twee duurste manieren om warmte
te maken. Vastleggen hoe vaak ze aanstaan, is de eenvoudigste controle of de
installatie goed staat ingeregeld.

## Hoe deze gegevens worden opgeslagen

| Soort | Tabel | Vorm |
|---|---|---|
| Elke wijziging | `readings` | `entity_id`, `ts`, `value_num`, `value_text`, `source` |
| Attributen bij wijziging | `attributes` | `entity_id`, `ts`, `attrs_json` |
| Metadata | `entities` | naam, eenheid, device_class, state_class, apparaat |
| Recorder-statistiek | `statistics` | per periode: mean, min, max, state, sum, change |
| Onderbrekingen | `gaps` | `started_at`, `ended_at`, `reason` |
| Capture-sessies | `runs` | starttijd, events, herverbindingen, laatste fout |

`value_num` is de numerieke waarde als die bestaat; `value_text` is altijd de
ruwe tekst van Home Assistant. Een bedrijfsmodus als `defrosting` krijgt
daarom geen cijfer — dat zou suggereren dat de elf toestanden op een schaal
liggen — maar blijft volledig bewaard. `binary_sensor`-waarden worden wel
omgezet (`on` → 1, `off` → 0), zodat ze te plotten zijn.
