# Witz-Bot

Holt jeden Tag eine aktuelle Schlagzeile, schreibt einen kurzen Witz dazu, rendert daraus ein
Text-Reel (9:16, ca. 10–15 Sekunden) und veröffentlicht es auf Instagram. Keine KI-Bilder, keine
Personen, nur Typografie. Läuft kostenlos über GitHub Actions und das Freikontingent von
Cloudflare Workers AI.

## Ablauf eines Laufs

1. Themenfeld des Tages (rotiert: Geld & Karriere, Dating & Beziehungen, Luxus & Konsum, Technik & KI, Kurioses)
   und Format des Tages (rotiert: Witz zur Schlagzeile, Ehrliche Schlagzeile, Wer kennt's).
2. Schlagzeilen der letzten 48 Stunden aus Google News laden; Unglücke, Gewalt, Krankheit usw. aussortieren.
3. **Autor:** Das Sprachmodell schreibt 3 Witz-Varianten und 5 Hooks.
4. **Schlussredaktion:** Ein zweiter Durchgang prüft die Tabus, wählt den besten Witz und Hook,
   kürzt und vergibt eine Note. Unter 6 von 10 wird nichts gepostet.
5. Reel rendern: Hook → Setup mit Fortschrittsbalken → Pointe → „Schick das …“.
6. Upload direkt an Instagram.

Zusätzlich jeden Sonntag: Karussell „Die 5 besten Witze der Woche“ (Countdown von Platz 5 bis 1,
sortiert nach Likes und Kommentaren der Woche).

## Einrichtung

Die Schritt-für-Schritt-Anleitung steht im Chat; kurz:

1. Instagram-Konto (Creator) für den Kanal anlegen, `handle` in `config.json` eintragen.
2. Repository bei GitHub anlegen (**öffentlich**, weil Instagram die Karussell-Bilder über eine
   öffentliche Adresse abholt; Zugangsdaten liegen nur in den Secrets), Dateien hochladen,
   Settings → Actions → General → Workflow permissions: **Read and write**.
3. Cloudflare-Konto + API-Token (Vorlage „Workers AI“).
4. Meta-Entwickler-App mit „API setup with Instagram login“, Token + Instagram-User-ID.
5. Secrets: `CF_ACCOUNT_ID`, `CF_API_TOKEN`, `IG_USER_ID`, `IG_ACCESS_TOKEN`, `GH_PAT`.
6. Actions → **Witz des Tages** → Run workflow → `testlauf`; Reel unten im Lauf unter „Artifacts“ herunterladen.
7. Startpaket: `startpaket` erzeugen, durchsehen, schwache Witze aussortieren, `warteschlange_posten`.
8. Ab dann täglich automatisch, sonntags zusätzlich das Wochen-Karussell.

## Modi (Actions → Witz des Tages → Run workflow)

| Modus | Wirkung |
|---|---|
| `testlauf` | 1 Reel erzeugen, als Download bereitstellen, nichts posten |
| `posten` | 1 Reel erzeugen und veröffentlichen (läuft täglich automatisch) |
| `startpaket` | mehrere Reels (Feld „Anzahl“, Standard 6) im Ordner `queue/` ablegen, nichts posten |
| `warteschlange_posten` | alle Reels aus `queue/` im Minutenabstand veröffentlichen |
| `wochenrueckblick_test` | Karussell rendern, als Download bereitstellen, nichts posten |
| `wochenrueckblick` | Karussell veröffentlichen (läuft sonntags automatisch) |

**Aussortieren im Startpaket:** Im Ordner `queue/` die `.mp4`-Datei des Witzes löschen, der nicht
gepostet werden soll (Datei öffnen → Drei-Punkte-Menü → „Delete file“ → Commit). Die zugehörige
`.json`-Datei wird beim Posten automatisch entfernt.

## Formatvorlage

**Video (Textoverlay)**

| Zeit | Ebene | Regel |
|---|---|---|
| 0–2 s | Hook, groß, mittig | max. 8 Wörter, ab dem ersten Bild sichtbar, provokant oder kontraintuitiv, verrät die Pointe nicht |
| ganze Zeit | Kopfzeile „News des Tages“ + Schlagzeile + Quelle | Kontext und Glaubwürdigkeit |
| 2–ca. 8 s | Setup + Balken „Pointe kommt …“ | max. 18 Wörter; der Balken hält bis zur Pointe |
| danach | Pointe in weißer Box | max. 14 Wörter, kurzer Pop-Effekt |
| letzte 2 s | „Schick das jemandem, der …“ | konkreter Personentyp, löst Weiterleiten aus |

Danach beginnt das Reel von vorn; kurze Reels werden öfter mehrfach gesehen.

**Caption**

```
[Teaser, max. 10 Wörter, ohne Pointe]

📰 [Schlagzeile] ([Quelle])

[Frage an die Community: Ja/Nein, Team A oder B]
📩 Schick das [jemandem, der …]

[max. 5 Hashtags]
```

## Anpassen (`config.json`)

| Feld | Wirkung |
|---|---|
| `handle` | Kontoname im Video |
| `topics` | Themenfelder, Suchbegriffe, 2 Hashtags je Thema |
| `formats` | Witz-Formate und ihre Regeln |
| `design` | Farben (Hintergrund, Boxen, Akzent) |
| `timing` | Dauer der Szenen, Lesegeschwindigkeit |
| `blocklist` | Begriffe, bei denen eine Schlagzeile verworfen wird |
| `weekly_hashtags` | Hashtags des Wochen-Karussells (max. 5) |
| `disclosure` | optionaler Hinweis am Ende der Caption, z. B. „Witze mit KI-Unterstützung“ |
| `upload_mode` | `resumable` (Standard) oder `url` (Ausweichweg, siehe unten) |

**Musik:** Lizenzfreie Tracks (mit Erlaubnis zur kommerziellen Nutzung auf Social Media) als `.mp3`
in den Ordner `musik/` legen; der Bot wählt täglich einen. Ohne Musik bleibt die Tonspur stumm.
Instagram-Musik aus der App lässt sich über die API nicht hinzufügen.

**Uhrzeiten:** Zeilen `cron` in `.github/workflows/witz.yml` (Angabe in UTC). Wird die Uhrzeit des
Karussells geändert, muss derselbe Wert auch in der Zeile `MODUS:` angepasst werden.

**Pausieren:** Actions → Witz des Tages → „Disable workflow“.

## Fehlerbehebung

| Meldung im Protokoll | Lösung |
|---|---|
| `Video-Upload fehlgeschlagen` | In `config.json` `"upload_mode": "url"` setzen. |
| `Datei nicht öffentlich erreichbar` | Repository auf öffentlich stellen (Settings → General → Danger Zone). |
| `Container-Fehler 190` | Instagram-Token abgelaufen: neues Token erzeugen und Secret ersetzen. |
| `Kein Textmodell erreichbar` | Cloudflare-Token oder Account-ID prüfen. |
| „Heute kein Witz, der die Prüfung besteht“ | Kein Fehler: an dem Tag war nichts gut genug. |

Schriften: Inter (SIL Open Font License, siehe `fonts/LICENSE-Inter.txt`).
