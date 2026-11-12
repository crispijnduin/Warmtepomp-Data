# Changelog

## 1.1.1

- Een bedrijfsmodus die uit het tijdvenster liep bleef op zijn laatste waarde
  staan in plaats van op nul te gaan. Daardoor telde de verdeling op tot meer
  dan een uur: verwarmen stond op 60 minuten terwijl standby nog 0,6 minuten
  van een kwartier eerder liet zien. Modi die deze installatie kent worden nu
  altijd gepubliceerd, met nul als ze dit uur niet voorkwamen.

## 1.1.0

De database is nu ook bereikbaar voor wie alleen de Home Assistant-API heeft.

- **Samenvatting terug naar Home Assistant.** Elke vijf minuten worden cijfers
  gepubliceerd die Home Assistant zelf niet kan bewaren: hoe vaak de compressor
  aanslaat (pendelen) en hoeveel minuten de pomp in elke bedrijfsmodus stond.
  Omdat ze een state_class krijgen, bewaart de recorder ze als
  langetermijnstatistiek en blijven ze jaren opvraagbaar.
- **Vragen stellen via het logboek.** Een aanroep van `logbook.log` met de naam
  `warmtepomp-query` en een SELECT als bericht voert die vraag uit tegen de
  database; het antwoord verschijnt in `sensor.warmtepomp_query_result`. Alleen
  lezen is toegestaan.
- Twee telfouten verholpen die bij het bouwen hiervan aan het licht kwamen: een
  compressor die bij het begin van het uur al draaide telde als nieuwe start, en
  een bedrijfsmodus die al langer ongewijzigd was telde helemaal niet mee.

## 1.0.0

Eerste versie.

- Legt elke statewijziging van de warmtepomp vast in SQLite, op volle resolutie.
- Neemt bij het starten de bestaande historie uit Home Assistant over.
- Vindt entiteiten op integratie en op apparaat, dus nieuwe sensoren lopen
  automatisch mee.
- Markeert perioden zonder verbinding als gat, zodat zichtbaar is waar data mist.
- Database in `/share/warmtepomp/`, dus hij overleeft updates en gaat mee in de
  Home Assistant-back-up.
