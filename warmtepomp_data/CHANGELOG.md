# Changelog

## 1.0.0

Eerste versie.

- Legt elke statewijziging van de warmtepomp vast in SQLite, op volle resolutie.
- Neemt bij het starten de bestaande historie uit Home Assistant over.
- Vindt entiteiten op integratie en op apparaat, dus nieuwe sensoren lopen
  automatisch mee.
- Markeert perioden zonder verbinding als gat, zodat zichtbaar is waar data mist.
- Database in `/share/warmtepomp/`, dus hij overleeft updates en gaat mee in de
  Home Assistant-back-up.
