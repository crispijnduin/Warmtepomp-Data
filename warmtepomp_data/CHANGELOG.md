# Changelog

## 1.2.3

- De controle op schrijfopdrachten weigerde ook `replace()`, de gewone
  tekstfunctie van SQLite. Een leesvraag die entiteitsnamen inkortte kwam
  daardoor niet door, met een melding over schrijven terwijl er niets werd
  geschreven. De verbodslijst kijkt nu op `REPLACE INTO`; de schrijfvorm
  `INSERT OR REPLACE` ketst al op `insert` af.

## 1.2.2

- Een onbruikbare waarde in 'Custom Home Assistant URL' legde de hele app
  plat: bij elke start een onleesbare fout op de WebSocket-URL, en dus geen
  capture meer. Dit gebeurde in de praktijk doordat een wachtwoordmanager het
  veld invulde. De app controleert nu op http:// of https://, negeert een
  onbruikbare waarde met uitleg in het logboek, en gaat door via de
  Supervisor-verbinding. Vastleggen gaat voor.
- Het tokenveld is geen wachtwoordveld meer. Dat type was precies wat
  wachtwoordmanagers aantrok: die zagen een inlogformulier en vulden bij elke
  paginalading opnieuw een e-mailadres en wachtwoord in, ook nadat de
  gebruiker de velden had leeggemaakt. Als gewoon tekstveld gebeurt dat niet.
  Het veld hoort leeg te blijven, dus er valt niets te verbergen.

## 1.2.0

Meetwaarden die in een attribuut zitten worden nu als eigen reeks vastgelegd.

Aanleiding: een Evohome-thermostaat levert per zone een climate-entiteit
waarvan de state 'heat' of 'off' is; de kamertemperatuur staat in het
attribuut `current_temperature`. Home Assistant bewaart daar geen enkele
statistiek van, dus die reeks verdwijnt na de bewaartermijn volledig.

- `current_temperature`, `temperature` en een paar verwante attributen worden
  opgeslagen als eigen meetreeks, met eenheid en state_class, zodat ze net als
  elke sensor te bevragen, te exporteren en uit te zetten zijn tegen de
  aanvoertemperatuur van de warmtepomp.
- De backfill haalt deze reeksen met terugwerkende kracht op. Dat vraagt de
  historie mét attributen op, wat een veel zwaardere vraag is, dus het gebeurt
  alleen voor de entiteiten die zulke attributen werkelijk hebben.
- Instelbaar met `attribute_series`; leeg zetten schakelt het uit.

Om een thermostaat mee te nemen moet hij wel bij Integraties staan, bijvoorbeeld
`weheat,evohome`.

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
