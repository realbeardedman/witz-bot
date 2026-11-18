#!/usr/bin/env python3
"""Deadpan Desk: aktuelle Schlagzeile -> Witz -> Text-Reel (MP4) -> Instagram.

Sprache über "language" in config.json ("en" oder "de"; deutsche Vorlage in presets/config.de.json).

Befehle:
  python deadpan.py prepare            wählt eine Schlagzeile, schreibt den Witz, rendert das Reel nach out/
  python deadpan.py publish            veröffentlicht das vorbereitete Reel auf Instagram
  python deadpan.py startpaket [N]     erzeugt N Reels (Standard 6) in den Ordner queue/ zur Durchsicht
  python deadpan.py queue              veröffentlicht alle Reels aus queue/ nacheinander
  python deadpan.py check            prüft den Instagram-Zugang und zeigt die IG_USER_ID
  python deadpan.py weekly [--dry]     Karussell "Die besten Witze der Woche" (--dry: nur rendern)

Umgebungsvariablen (als GitHub-Secrets hinterlegen):
  CF_ACCOUNT_ID, CF_API_TOKEN     Cloudflare Workers AI (Textmodell)
  IG_USER_ID, IG_ACCESS_TOKEN     Instagram (nur für publish)
"""
import calendar
import datetime as dt
import html
import json
import os
import random
import re
import subprocess
import sys
import time
import urllib.parse
from functools import lru_cache
from pathlib import Path

import feedparser
import requests
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent
CFG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
STATE_FILE = ROOT / "state" / "posted.json"
OUT = ROOT / "out"
PENDING_FILE = OUT / "pending.json"
VIDEO_FILE = OUT / "reel.mp4"
PREVIEW_FILE = OUT / "vorschau.jpg"
QUEUE = ROOT / "queue"
MEDIA = ROOT / "media"

LANG = CFG.get("language", "de")
TEXTS = {
    "de": {
        "label": "NEWS DES TAGES", "source": "Quelle", "loading": "Pointe kommt …", "send": "Schick das",
        "default_send": "jemandem, der heute ein Lachen braucht", "default_question": "Wie seht ihr das?",
        "weekly_label": "WITZE DER WOCHE", "weekly_title": "Die {n} besten Witze der Woche",
        "weekly_sub": "Aus den Nachrichten dieser Woche, von Platz {n} bis 1", "swipe": "Wisch nach links  →",
        "rank": "PLATZ {rank}", "end_label": "UND JETZT DU", "end_title": "Welcher war dein Favorit?",
        "end_sub": "Schreib die Nummer in die Kommentare.", "follow": "Folge {handle} für jeden Tag einen Witz",
        "weekly_caption": "Die {n} besten Witze dieser Woche, von Platz {n} bis 1.\n\n"
                          "Welcher ist dein Favorit? Schreib die Nummer in die Kommentare.\n"
                          "📩 Schick das jemandem, der diese Woche ein Lachen verdient hat.",
        "date": "%d.%m.",
    },
    "en": {
        "label": "TODAY'S HEADLINE", "source": "Source", "loading": "Punchline loading …", "send": "Send this to",
        "default_send": "someone who needs a laugh today", "default_question": "Agree or disagree?",
        "weekly_label": "JOKES OF THE WEEK", "weekly_title": "The {n} best jokes of the week",
        "weekly_sub": "From this week's headlines, counting down from #{n} to #1", "swipe": "Swipe left  →",
        "rank": "NUMBER {rank}", "end_label": "YOUR TURN", "end_title": "Which one was your favorite?",
        "end_sub": "Drop the number in the comments.", "follow": "Follow {handle} for one joke a day",
        "weekly_caption": "The {n} best jokes of the week, counting down from #{n} to #1.\n\n"
                          "Which one's your favorite? Drop the number in the comments.\n"
                          "📩 Send this to someone who earned a laugh this week.",
        "date": "%b %-d",
    },
}
T_ = TEXTS[LANG]

W, H, FPS = 1080, 1920, 30
LEFT, RIGHT = 96, 120          # rechts mehr Rand wegen der Instagram-Buttons
TEXT_W = W - LEFT - RIGHT
MAIN_BOTTOM = 1390             # darunter liegen CTA und Instagram-Bedienelemente
FONT_BLACK = ROOT / "fonts" / "InterDisplay-Black.otf"
FONT_BOLD = ROOT / "fonts" / "InterDisplay-Bold.otf"
FONT_SEMI = ROOT / "fonts" / "Inter-SemiBold.otf"


def env(name):
    value = os.environ.get(name, "").strip()
    if not value:
        sys.exit(f"Fehlende Umgebungsvariable: {name}")
    return value


def load_state():
    return json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.exists() else []


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


EMOJI = re.compile("[\U00010000-\U0010FFFF☀-➿️‍]")


def clean(text):
    """Text fürs Video: ohne Emojis, ohne Anführungszeichen außen, einfache Leerzeichen."""
    text = EMOJI.sub("", str(text or ""))
    return re.sub(r"\s+", " ", text).strip().strip('"„“').strip()


def words(text):
    return len(str(text).split())


# ---------------------------------------------------------------- Textmodell

def parse_json(text):
    if isinstance(text, dict):
        return text
    decoder = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch == "{":
            try:
                obj, _ = decoder.raw_decode(text[i:])
                return obj
            except json.JSONDecodeError:
                continue
    raise ValueError(f"Keine JSON-Antwort: {text[:300]}")


def extract_text(data):
    """Workers AI liefert je nach Modell unterschiedliche Formen; alle bekannten abdecken."""
    r = data.get("result", data) if isinstance(data, dict) else data
    if isinstance(r, dict):
        if "response" in r and r["response"]:
            return r["response"]
        if r.get("choices"):
            return r["choices"][0]["message"]["content"]
        if r.get("output_text"):
            return r["output_text"]
        for item in r.get("output", []) or []:
            if item.get("type") == "message":
                for part in item.get("content", []):
                    if part.get("text"):
                        return part["text"]
    raise ValueError(f"Unbekanntes Antwortformat: {str(data)[:300]}")


def llm(system, user):
    last = ""
    for model in (CFG["text_model"], CFG.get("fallback_text_model")):
        if not model:
            continue
        url = f"https://api.cloudflare.com/client/v4/accounts/{env('CF_ACCOUNT_ID')}/ai/run/{model}"
        try:
            r = requests.post(
                url,
                headers={"Authorization": f"Bearer {env('CF_API_TOKEN')}"},
                json={
                    "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                    "max_tokens": 4000,
                    "temperature": 0.9,
                },
                timeout=240,
            )
            if r.status_code == 200:
                return parse_json(extract_text(r.json()))
            last = f"{model}: HTTP {r.status_code} {r.text[:300]}"
        except (requests.RequestException, ValueError, KeyError, IndexError) as exc:
            last = f"{model}: {exc}"
        print(f"Textmodell nicht nutzbar ({last}), versuche Ausweichmodell.")
    sys.exit(f"Kein Textmodell erreichbar. Letzter Fehler: {last}")


# --------------------------------------------------------------------- Feed

def blocked(text):
    """Kurze Begriffe nur als ganzes Wort, längere auch als Wortanfang/-ende (deutsche Komposita)."""
    tokens = re.findall(r"[a-zäöüß]+", text.lower())
    for term in CFG["blocklist"]:
        for tok in tokens:
            if tok == term or (len(term) >= 5 and (tok.startswith(term) or tok.endswith(term))):
                return True
    return False


def fetch_candidates(topic, seen):
    query = urllib.parse.quote(f"{topic['query']} when:2d")
    f = CFG.get("feed", {"hl": "de", "gl": "DE", "ceid": "DE:de"})
    feed = feedparser.parse(f"https://news.google.com/rss/search?q={query}&hl={f['hl']}&gl={f['gl']}&ceid={f['ceid']}")
    limit = time.time() - CFG["max_article_age_hours"] * 3600
    out = []
    for e in feed.entries:
        title = html.unescape(e.get("title", "")).strip()
        source = (e.get("source") or {}).get("title", "")
        if source and title.endswith(f" - {source}"):
            title = title[: -len(source) - 3].strip()
        link = e.get("link", "")
        published = e.get("published_parsed")
        if not title or not link or link in seen or title.lower() in seen:
            continue
        if published and calendar.timegm(published) < limit:
            continue
        if blocked(title):
            continue
        out.append({"title": title, "source": source or "Nachrichtenquelle", "link": link})
        if len(out) == 12:
            break
    return out


# ------------------------------------------------------------------- Texte

AUTHOR_DE = """Du schreibst für einen deutschsprachigen Instagram-Kanal, der jeden Tag einen kurzen Witz zu einer aktuellen Schlagzeile als Reel zeigt. Ziel: Menschen lachen und schicken das Reel einer bestimmten Person weiter.

Du bekommst ein Format und nummerierte Schlagzeilen. Wähle die Schlagzeile mit dem größten Witzpotenzial für ein breites Publikum.

Humor: kurz, alltagsnah, überraschend, eher frech als böse. Gute Pointen sind konkret (Situationen und Typen wie Chef, Mama, Ex, Kollegin, Vermieter) statt abstrakt. Wortspiele nur, wenn sie wirklich sitzen.

Tabu: Witze auf Kosten von Gruppen (Herkunft, Religion, Geschlecht, Sexualität, Behinderung, Aussehen, Alter); Tod, Gewalt, Krankheit, Unglücke, Straftaten; Privatpersonen; parteipolitische Seitenhiebe; erfundene Tatsachen über reale Personen oder Firmen. Fakten nur aus der Schlagzeile, der Witz darf offensichtlich übertreiben.

Liefere:
- 3 Witz-Varianten im vorgegebenen Format: "setup" höchstens 18 Wörter, "punchline" höchstens 14 Wörter. Die Schlagzeile steht bereits oben im Video: das Setup wiederholt sie nicht wörtlich, sondern führt direkt zum Witz hin und verrät die Pointe nicht.
- 5 Hook-Varianten, je höchstens 8 Wörter. Ein Hook stoppt das Scrollen in der ersten Sekunde: provokant oder kontraintuitiv, macht neugierig auf die Pointe, verrät sie aber nicht. Muster: kontraintuitive Behauptung ("Diese Nachricht ist besser als jeder Witz"), Ansage mit Lücke ("Mein Chef darf das nicht sehen"), direkte Ansprache ("Wenn du Single bist: nicht weiterwischen").
- "send_to": Fortsetzung von "Schick das ...", beginnt mit "jemandem, der" oder "deinem"/"deiner", konkret und wiedererkennbar, höchstens 9 Wörter.
- "comment_question": kurze, leicht zu beantwortende Frage an die Community (Ja/Nein, Team A oder B, eine Zahl).
- "teaser": erste Zeile der Caption, höchstens 10 Wörter, ohne die Pointe.
Keine Emojis, keine Hashtags.

Antworte ausschließlich mit JSON:
{"choice": <Nummer der Schlagzeile oder 0, wenn keine taugt>, "jokes": [{"setup": "...", "punchline": "..."}, {...}, {...}], "hooks": ["...", "...", "...", "...", "..."], "send_to": "...", "comment_question": "...", "teaser": "..."}"""

EDITOR_DE = """Du bist die Schlussredaktion eines Humor-Kanals auf Instagram. Prüfe streng.
1. Sicherheit: Verwirf jeden Witz, der Gruppen herabsetzt (Herkunft, Religion, Geschlecht, Sexualität, Behinderung, Aussehen, Alter), Tod, Gewalt, Krankheit, Unglücke oder Straftaten betrifft, Privatpersonen zeigt, parteipolitisch austeilt oder Tatsachen über reale Personen oder Firmen erfindet.
2. Wähle den lustigsten verbleibenden Witz: überraschend, sofort verständlich, man will ihn weiterschicken. Bewerte ihn ehrlich von 1 bis 10.
3. Wähle den stärksten Hook, der zum Witz passt und die Pointe nicht verrät.
4. Kürze und schärfe, ohne den Witz zu verändern: Hook höchstens 8 Wörter, Setup höchstens 18 Wörter (wiederholt die Schlagzeile nicht wörtlich), Pointe höchstens 14 Wörter, "send_to" höchstens 9 Wörter. Keine Emojis, keine Hashtags.
Setze "ok" auf false, wenn kein Witz die Sicherheitsprüfung besteht oder der beste unter 6 Punkten liegt.

Antworte ausschließlich mit JSON:
{"ok": true, "score": 7, "reason": "...", "hook": "...", "setup": "...", "punchline": "...", "send_to": "..."}"""


AUTHOR_EN = """You write for an English-language Instagram account called Deadpan Desk that posts one short joke a day about a real, current headline, as a Reel. Goal: people laugh and send the Reel to one specific person.

You get a format and numbered headlines. Pick the headline with the most comedic potential for a broad international audience.

Tone: deadpan. Dry, understated, observational, delivered with a straight face. Short and concrete (bosses, landlords, exes, group chats, Mondays) rather than abstract. Puns only if they genuinely land. Think late-night monologue joke, not meme caption.

Off-limits: jokes at the expense of groups (origin, religion, gender, sexuality, disability, looks, age); death, violence, illness, disasters, crime; private individuals; partisan politics; invented facts about real people or companies. Use only facts from the headline; the joke may exaggerate obviously.

Deliver:
- 3 joke variants in the given format: "setup" max 18 words, "punchline" max 14 words. The headline is already shown at the top of the video: the setup does not repeat it word for word, it leads straight into the joke without giving away the punchline.
- 5 hook variants, max 8 words each. A hook stops the scroll in the first second: provocative or counterintuitive, makes people curious about the punchline without revealing it. Patterns: counterintuitive claim ("This headline is funnier than any joke"), open loop ("My boss cannot see this"), direct address ("If you're single, don't scroll").
- "send_to": continuation of "Send this to ...", starts with "someone who" or "your", concrete and recognisable, max 9 words.
- "comment_question": short, easy-to-answer question for the comments (yes/no, team A or B, a number).
- "teaser": first line of the caption, max 10 words, no punchline.
No emojis, no hashtags. American English spelling.

Reply with JSON only:
{"choice": <headline number or 0 if none works>, "jokes": [{"setup": "...", "punchline": "..."}, {...}, {...}], "hooks": ["...", "...", "...", "...", "..."], "send_to": "...", "comment_question": "...", "teaser": "..."}"""

EDITOR_EN = """You are the final editor of a deadpan humor account on Instagram. Be strict.
1. Safety: discard any joke that demeans groups (origin, religion, gender, sexuality, disability, looks, age), touches death, violence, illness, disasters or crime, features private individuals, takes partisan political shots or invents facts about real people or companies.
2. Pick the funniest remaining joke: surprising, instantly understood, something you would forward. Rate it honestly from 1 to 10.
3. Pick the strongest hook that fits the joke and does not give away the punchline.
4. Tighten without changing the joke: hook max 8 words, setup max 18 words (does not repeat the headline word for word), punchline max 14 words, "send_to" max 9 words. No emojis, no hashtags.
Set "ok" to false if no joke passes the safety check or the best one scores below 6.

Reply with JSON only:
{"ok": true, "score": 7, "reason": "...", "hook": "...", "setup": "...", "punchline": "...", "send_to": "..."}"""

AUTHOR = AUTHOR_EN if LANG == "en" else AUTHOR_DE
EDITOR = EDITOR_EN if LANG == "en" else EDITOR_DE


def write_joke(topic, fmt, candidates, recent):
    listing = "\n".join(f"{i}. {c['title']} ({c['source']})" for i, c in enumerate(candidates, 1))
    draft = llm(
        AUTHOR,
        f"Topic / Themenfeld: {topic['name']}\nFormat: {fmt['name']} – {fmt['instruction']}\n\n"
        f"Headlines / Schlagzeilen:\n{listing}\n\nRecent punchlines, do not repeat / nicht wiederholen: {recent}",
    )
    choice = int(draft.get("choice") or 0)
    if not 1 <= choice <= len(candidates) or not draft.get("jokes") or not draft.get("hooks"):
        return None, None
    article = candidates[choice - 1]
    jokes = "\n".join(
        f"{i}. Setup: {j.get('setup', '')} | Punchline: {j.get('punchline', '')}" for i, j in enumerate(draft["jokes"], 1)
    )
    hooks = "\n".join(f"{i}. {h}" for i, h in enumerate(draft["hooks"], 1))
    final = llm(
        EDITOR,
        f"Headline: {article['title']} ({article['source']})\nFormat: {fmt['name']}\n\n"
        f"Jokes:\n{jokes}\n\nHooks:\n{hooks}\n\n'{T_['send']} ...': {draft.get('send_to', '')}",
    )
    return article, {"draft": draft, "final": final}


def valid(final):
    if not final.get("ok") or int(final.get("score") or 0) < 6:
        return False
    hook, setup, punch = clean(final.get("hook")), clean(final.get("setup")), clean(final.get("punchline"))
    if not hook or not setup or not punch:
        return False
    if words(hook) > 11 or words(setup) > 26 or words(punch) > 20:
        return False
    return not any(blocked(t) for t in (hook, setup, punch))


# ---------------------------------------------------------------- Rendering

@lru_cache(maxsize=None)
def font(path, size):
    return ImageFont.truetype(str(path), size)


def wrap(text, fnt, max_w):
    lines, cur = [], ""
    for word in text.split():
        trial = f"{cur} {word}".strip()
        if fnt.getlength(trial) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


def fit(text, path, max_size, min_size, max_w, max_lines):
    for size in range(max_size, min_size - 1, -2):
        fnt = font(path, size)
        lines = wrap(text, fnt, max_w)
        if len(lines) <= max_lines and all(fnt.getlength(l) <= max_w for l in lines):
            return fnt, lines
    fnt = font(path, min_size)
    return fnt, wrap(text, fnt, max_w)


def lh(fnt):
    return int(fnt.size * 1.2)


def draw_text(draw, lines, fnt, y, fill, x=LEFT, center=False, width=TEXT_W):
    for i, line in enumerate(lines):
        lx = x + (width - fnt.getlength(line)) / 2 if center else x
        draw.text((lx, y + i * lh(fnt)), line, font=fnt, fill=fill)
    return y + lh(fnt) * len(lines)


def boxed(text, path, max_size, min_size, bg, fg, pad=34, max_lines=4):
    """Text in abgerundeter Box als RGBA-Bild."""
    fnt, lines = fit(text, path, max_size, min_size, TEXT_W - 2 * pad, max_lines)
    h = lh(fnt) * len(lines) + 2 * pad - int(fnt.size * 0.2)
    img = Image.new("RGBA", (TEXT_W, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, TEXT_W - 1, h - 1), radius=30, fill=bg)
    draw_text(d, lines, fnt, pad - int(fnt.size * 0.05), fg, x=pad)
    return img


def layout(c, scale=1.0):
    """Erzeugt die Bildebenen der vier Szenen. scale < 1 verkleinert Setup und Pointe, falls zu lang."""
    D = CFG["design"]
    base = Image.new("RGB", (W, H), D["background"])
    d = ImageDraw.Draw(base)

    label = font(FONT_SEMI, 34)
    d.text((LEFT, 196), T_["label"], font=label, fill=D["muted"])
    handle = CFG["handle"]
    d.text((W - RIGHT - label.getlength(handle) + 24, 196), handle, font=label, fill=D["muted"])

    head = boxed(c["headline"], FONT_SEMI, 44, 32, D["headline_box"], D["headline_text"], pad=28, max_lines=3)
    base.paste(head, (LEFT, 256), head)
    y = 256 + head.height + 14
    src = font(FONT_SEMI, 30)
    d.text((LEFT + 4, y), f"{T_['source']}: {c['source']}", font=src, fill=D["muted"])
    y_main = y + lh(src) + 56

    # Szene 1: Hook groß und mittig
    hook = base.copy()
    hf, hl = fit(c["hook"], FONT_BLACK, 104, 62, TEXT_W, 4)
    block = lh(hf) * len(hl)
    draw_text(ImageDraw.Draw(hook), hl, hf, y_main + max(0, (MAIN_BOTTOM - y_main - block) // 2 - 60), D["text"], center=True)

    # Szene 2: Setup + Platz für Fortschrittsbalken
    setup = base.copy()
    sf, sl = fit(c["setup"], FONT_BOLD, int(70 * scale), int(44 * scale), TEXT_W, 6)
    y_after_setup = draw_text(ImageDraw.Draw(setup), sl, sf, y_main, D["text"])

    # Szene 3: Pointe als weiße Box
    punch = boxed(c["punchline"], FONT_BLACK, int(78 * scale), int(48 * scale), D["punchline_box"], D["punchline_text"])
    punch_y = y_after_setup + 52
    if punch_y + punch.height > MAIN_BOTTOM and scale > 0.6:
        return layout(c, scale - 0.08)

    final = setup.copy()
    final.paste(punch, (LEFT, punch_y), punch)

    # Szene 4: Pointe + Aufforderung zum Weiterschicken
    cta = final.copy()
    pill = boxed(f"{T_['send']} {c['send_to']}", FONT_BOLD, 44, 32, D["accent"], "#FFFFFF", pad=26, max_lines=2)
    cta.paste(pill, (LEFT, max(MAIN_BOTTOM + 30, punch_y + punch.height + 40)), pill)

    return {"hook": hook, "setup": setup, "punch": punch, "punch_y": punch_y,
            "final": final, "cta": cta, "bar_y": y_after_setup + 70}


def timeline(c):
    t = CFG["timing"]
    return {
        "hook": t["hook_seconds"],
        "setup": max(t["min_setup_seconds"], words(c["setup"]) / t["words_per_second"] + 0.8),
        "punch": max(t["min_punchline_seconds"], words(c["punchline"]) / t["words_per_second"] + 1.6),
        "cta": t["cta_seconds"],
    }


def frames(c):
    """Liefert alle Einzelbilder als Bytes (RGB, 1080x1920)."""
    D = CFG["design"]
    L, T = layout(c), timeline(c)
    hook_bytes = L["hook"].tobytes()
    for _ in range(round(T["hook"] * FPS)):
        yield hook_bytes

    n = round(T["setup"] * FPS)
    label = font(FONT_SEMI, 36)
    for i in range(n):
        img = L["setup"].copy()
        d = ImageDraw.Draw(img)
        d.text((LEFT, L["bar_y"]), T_["loading"], font=label, fill=D["muted"])
        y = L["bar_y"] + 62
        d.rounded_rectangle((LEFT, y, LEFT + TEXT_W, y + 18), radius=9, fill=D["muted"])
        d.rounded_rectangle((LEFT, y, LEFT + max(18, int(TEXT_W * (i + 1) / n)), y + 18), radius=9, fill=D["text"])
        yield img.tobytes()

    pop = 8
    for i in range(pop):
        k = 1 - (1 - (i + 1) / pop) ** 3
        s = 0.82 + 0.18 * k
        img = L["setup"].copy()
        p = L["punch"].resize((max(1, int(L["punch"].width * s)), max(1, int(L["punch"].height * s))))
        img.paste(p, (LEFT + (L["punch"].width - p.width) // 2, L["punch_y"] + (L["punch"].height - p.height) // 2), p)
        yield img.tobytes()
    final_bytes = L["final"].tobytes()
    for _ in range(round(T["punch"] * FPS) - pop):
        yield final_bytes

    cta_bytes = L["cta"].tobytes()
    for _ in range(round(T["cta"] * FPS)):
        yield cta_bytes


def render(c, video_path=VIDEO_FILE, preview_path=PREVIEW_FILE):
    video_path.parent.mkdir(parents=True, exist_ok=True)
    T = timeline(c)
    duration = sum(T.values())
    music = sorted(p for p in (ROOT / "musik").glob("*") if p.suffix.lower() in (".mp3", ".m4a", ".wav", ".aac"))
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
           "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-"]
    if music:
        track = random.Random(c["date"] + c["headline"]).choice(music)
        print(f"Musik: {track.name}")
        cmd += ["-stream_loop", "-1", "-i", str(track), "-af",
                f"volume={CFG['music_volume']},afade=t=out:st={max(0, duration - 1):.2f}:d=1"]
    else:
        cmd += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
    cmd += ["-map", "0:v", "-map", "1:a", "-t", f"{duration:.2f}",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
            "-profile:v", "high", "-g", "60", "-c:a", "aac", "-b:a", "128k", "-ar", "48000",
            "-movflags", "+faststart", str(video_path)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    for frame in frames(c):
        proc.stdin.write(frame)
    proc.stdin.close()
    if proc.wait() != 0:
        sys.exit("ffmpeg ist fehlgeschlagen.")

    if preview_path:
        L = layout(c)
        sheet = Image.new("RGB", (W, 640), "#FFFFFF")
        for i, key in enumerate(("hook", "setup", "cta")):
            sheet.paste(L[key].resize((360, 640)), (i * 360, 0))
        sheet.save(preview_path, "JPEG", quality=88)
    return duration


# ------------------------------------------------------------ Karussell-Folien

SW, SH = 1080, 1350          # 4:5, empfohlenes Karussell-Format
CX = (SW - TEXT_W) // 2


def slide_base(top_left, top_right=None):
    D = CFG["design"]
    img = Image.new("RGB", (SW, SH), D["background"])
    d = ImageDraw.Draw(img)
    label = font(FONT_SEMI, 34)
    d.text((CX, 96), top_left, font=label, fill=D["muted"])
    right = top_right or CFG["handle"]
    d.text((SW - CX - label.getlength(right), 96), right, font=label, fill=D["muted"])
    return img, d


def pill(img, text, y, color=None):
    p = boxed(text, FONT_BOLD, 44, 32, color or CFG["design"]["accent"], "#FFFFFF", pad=26, max_lines=2)
    img.paste(p, (CX, y), p)
    return y + p.height


def slide_cover(n, period):
    img, d = slide_base(T_["weekly_label"], period)
    D = CFG["design"]
    tf, tl = fit(T_["weekly_title"].format(n=n), FONT_BLACK, 132, 84, TEXT_W, 4)
    y = draw_text(d, tl, tf, 300, D["text"], x=CX, center=True)
    sf, sl = fit(T_["weekly_sub"].format(n=n), FONT_BOLD, 46, 34, TEXT_W, 3)
    draw_text(d, sl, sf, y + 50, D["muted"], x=CX, center=True)
    pill(img, T_["swipe"], 1090)
    return img


def slide_joke(rank, j, scale=1.0):
    img, d = slide_base(T_["rank"].format(rank=rank))
    D = CFG["design"]
    nf = font(FONT_BLACK, 150)
    d.text((CX - 6, 150), f"#{rank}", font=nf, fill=D["accent"])
    head = boxed(j["headline"], FONT_SEMI, 36, 28, D["headline_box"], D["headline_text"], pad=24, max_lines=3)
    img.paste(head, (CX, 360), head)
    top, bottom = 360 + head.height + 50, SH - 110
    sf, sl = fit(j["setup"], FONT_BOLD, int(74 * scale), int(40 * scale), TEXT_W, 6)
    punch = boxed(j["punchline"], FONT_BLACK, int(84 * scale), int(44 * scale), D["punchline_box"], D["punchline_text"])
    block = lh(sf) * len(sl) + 50 + punch.height
    if top + block > bottom and scale > 0.6:
        return slide_joke(rank, j, scale - 0.08)
    y = top + max(0, (bottom - top - block) // 3)   # leicht oberhalb der Mitte
    y = draw_text(d, sl, sf, y, D["text"], x=CX)
    img.paste(punch, (CX, y + 50), punch)
    return img


def slide_end():
    img, d = slide_base(T_["end_label"])
    D = CFG["design"]
    tf, tl = fit(T_["end_title"], FONT_BLACK, 124, 84, TEXT_W, 3)
    y = draw_text(d, tl, tf, 330, D["text"], x=CX, center=True)
    sf, sl = fit(T_["end_sub"], FONT_BOLD, 50, 36, TEXT_W, 2)
    draw_text(d, sl, sf, y + 50, D["muted"], x=CX, center=True)
    pill(img, T_["follow"].format(handle=CFG["handle"]), 1060)
    return img


# ------------------------------------------------------------------ Prepare

def caption(c, topic):
    tags = []
    for tag in topic["hashtags"] + CFG["always_hashtags"]:
        if tag.lower() not in (t.lower() for t in tags):
            tags.append(tag)
    parts = [
        c["teaser"],
        f"📰 {c['headline']} ({c['source']})",
        f"{c['comment_question']}\n📩 {T_['send']} {c['send_to']}",
    ]
    if CFG.get("disclosure"):
        parts.append(CFG["disclosure"])
    parts.append(" ".join(tags[:5]))  # Instagram wertet höchstens fünf Hashtags
    return "\n\n".join(p for p in parts if p)[:2200]


def seen_and_recent(state):
    seen = {p.get("link", "") for p in state} | {p.get("headline", "").lower() for p in state}
    seen.discard("")
    recent = [p["punchline"] for p in state if p.get("punchline")][-10:]
    return seen, recent


def build_joke(topic, fmt, seen, recent):
    candidates = fetch_candidates(topic, seen)
    if not candidates:
        print(f"{topic['name']}: keine passenden Schlagzeilen.")
        return None
    article, result = write_joke(topic, fmt, candidates, recent)
    if not article or not valid(result["final"]):
        reason = result["final"].get("reason", "zu schwach") if result else "keine Schlagzeile ausgewählt"
        print(f"{topic['name']} / {fmt['name']}: verworfen ({reason}).")
        return None
    f, d = result["final"], result["draft"]
    c = {
        "date": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d-%H%M"),
        "topic": topic["name"],
        "format": fmt["name"],
        "headline": clean(article["title"]),
        "source": clean(article["source"]),
        "link": article["link"],
        "hook": clean(f["hook"]),
        "setup": clean(f["setup"]),
        "punchline": clean(f["punchline"]),
        "send_to": clean(f.get("send_to") or d.get("send_to") or T_["default_send"]).rstrip("."),
        "comment_question": clean(d.get("comment_question") or T_["default_question"]),
        "teaser": clean(d.get("teaser") or f["hook"]),
        "score": f.get("score"),
        "editor_note": f.get("reason", ""),
        "alternatives": {"hooks": d.get("hooks", []), "jokes": d.get("jokes", [])},
    }
    c["caption"] = caption(c, topic)
    return c


def show(c):
    print(f"\nSchlagzeile: {c['headline']}\nHook:   {c['hook']}\nSetup:  {c['setup']}\n"
          f"Pointe: {c['punchline']}\nNote:   {c['score']}/10\nLänge:  {c.get('duration')} s\n\n{c['caption']}\n")


def prepare():
    OUT.mkdir(exist_ok=True)
    for f in (PENDING_FILE, VIDEO_FILE, PREVIEW_FILE):
        f.unlink(missing_ok=True)
    seen, recent = seen_and_recent(load_state())
    topics, formats = CFG["topics"], CFG["formats"]
    day = dt.date.today().toordinal()
    fmt = formats[day % len(formats)]
    for offset in range(len(topics)):
        c = build_joke(topics[(day + offset) % len(topics)], fmt, seen, recent)
        if c:
            c["duration"] = round(render(c), 2)
            save_json(PENDING_FILE, c)
            show(c)
            return
    print("Heute kein Witz, der die Prüfung besteht. Es wird nichts gepostet.")


def startpaket(n=6):
    """Erzeugt bis zu n Reels mit wechselnden Themen und Formaten in queue/."""
    QUEUE.mkdir(exist_ok=True)
    OUT.mkdir(exist_ok=True)
    queued = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(QUEUE.glob("*.json"))]
    seen, recent = seen_and_recent(load_state() + queued)
    topics, formats = CFG["topics"], CFG["formats"]
    made = 0
    for i in range(len(topics) * len(formats)):
        if made >= n:
            break
        c = build_joke(topics[i % len(topics)], formats[i % len(formats)], seen, recent)
        if not c:
            continue
        stem = f"{c['date']}-{len(queued) + made + 1:02d}"
        c["duration"] = round(render(c, QUEUE / f"{stem}.mp4", OUT / f"{stem}.jpg"), 2)
        save_json(QUEUE / f"{stem}.json", c)
        seen |= {c["link"], c["headline"].lower()}
        recent.append(c["punchline"])
        made += 1
        print(f"--- Startpaket {made}/{n}: {stem}")
        show(c)
    print(f"{made} Reels liegen in queue/. Unpassende .mp4-Dateien dort löschen, dann 'warteschlange_posten'.")


# ------------------------------------------------------------------ Instagram

def ig():
    host = CFG.get("graph_host", "graph.facebook.com")   # Unified Login; Altbestand: graph.instagram.com
    return (f"https://{host}/{CFG['graph_api_version']}", env("IG_USER_ID"),
            {"Authorization": f"Bearer {env('IG_ACCESS_TOKEN')}"})


def check():
    """Prüft den Instagram-Zugang und zeigt die zugewiesenen Instagram-Konten mit ihrer ID."""
    host = CFG.get("graph_host", "graph.facebook.com")
    base = f"https://{host}/{CFG['graph_api_version']}"
    headers = {"Authorization": f"Bearer {env('IG_ACCESS_TOKEN')}"}

    def show_get(label, path, params=None):
        r = requests.get(f"{base}/{path}", headers=headers, params=params or {}, timeout=60)
        print(f"\n{label}: HTTP {r.status_code}")
        print(json.dumps(r.json(), ensure_ascii=False, indent=2)[:1500])
        return r

    me = show_get("Token gehört zu", "me", {"fields": "id,name"})
    if me.status_code != 200:
        sys.exit("Das Token wird nicht akzeptiert. Token neu erzeugen und Secret IG_ACCESS_TOKEN ersetzen.")
    show_get("Zugewiesene Instagram-Konten (die 'id' ist die IG_USER_ID)", "me/assigned_instagram_accounts",
             {"fields": "id,username"})
    ig_id = os.environ.get("IG_USER_ID", "").strip()
    if ig_id:
        acc = show_get("Konto aus IG_USER_ID", ig_id, {"fields": "id,username"})
        lim = show_get("Veröffentlichungs-Kontingent (zeigt, ob Posten erlaubt ist)",
                       f"{ig_id}/content_publishing_limit", {"fields": "quota_usage,config"})
        if acc.status_code == 200 and lim.status_code == 200:
            print(f"\nAlles bereit: @{acc.json().get('username')} kann veröffentlichen.")
        else:
            sys.exit("IG_USER_ID oder Berechtigungen passen noch nicht (siehe Ausgabe oben).")
    else:
        print("\nSecret IG_USER_ID fehlt noch: die 'id' von @deadpan.desk aus der Liste oben eintragen.")


def git(*args):
    subprocess.run(["git", *args], cwd=ROOT, check=True)


def push_public(paths, message):
    """Legt Dateien ins Repository und liefert öffentliche URLs (Repository muss öffentlich sein)."""
    git("add", *[str(p.relative_to(ROOT)) for p in paths])
    git("commit", "-m", message)
    git("pull", "--rebase", "--autostash", "-q")
    git("push")
    repo, branch = env("GITHUB_REPOSITORY"), os.environ.get("GITHUB_REF_NAME", "main")
    urls = [f"https://raw.githubusercontent.com/{repo}/{branch}/{p.relative_to(ROOT).as_posix()}" for p in paths]
    for url in urls:
        for _ in range(20):
            if requests.head(url, timeout=30).status_code == 200:
                break
            time.sleep(15)
        else:
            sys.exit(f"Datei nicht öffentlich erreichbar: {url}\nIst das Repository öffentlich?")
    return urls


def create_container(body):
    base, ig_id, headers = ig()
    r = requests.post(f"{base}/{ig_id}/media", headers=headers, json=body, timeout=120)
    if r.status_code != 200:
        sys.exit(f"Container-Fehler {r.status_code}: {r.text[:500]}")
    return r.json()


def wait_container(container, minutes=15):
    base, _, headers = ig()
    for _ in range(minutes * 3):
        s = requests.get(f"{base}/{container}", headers=headers,
                         params={"fields": "status_code,status"}, timeout=60).json()
        if s.get("status_code") == "FINISHED":
            return
        if s.get("status_code") in ("ERROR", "EXPIRED"):
            sys.exit(f"Instagram konnte den Inhalt nicht verarbeiten: {s}")
        time.sleep(20)
    sys.exit("Zeitüberschreitung bei der Verarbeitung durch Instagram.")


def publish_container(container):
    base, ig_id, headers = ig()
    r = requests.post(f"{base}/{ig_id}/media_publish", headers=headers, json={"creation_id": container}, timeout=120)
    if r.status_code != 200:
        sys.exit(f"Veröffentlichung fehlgeschlagen {r.status_code}: {r.text[:500]}")
    return r.json()["id"]


def publish_reel(c, video_path):
    body = {"media_type": "REELS", "caption": c["caption"], "share_to_feed": True}
    if CFG.get("audio_name"):
        body["audio_name"] = CFG["audio_name"]
    if CFG.get("upload_mode") == "url":
        target = MEDIA / f"{video_path.stem}.mp4"
        MEDIA.mkdir(exist_ok=True)
        target.write_bytes(video_path.read_bytes())
        body["video_url"] = push_public([target], f"Reel {video_path.stem}")[0]
        container = create_container(body)["id"]
    else:
        body["upload_type"] = "resumable"
        resp = create_container(body)
        container = resp["id"]
        upload_url = resp.get("uri") or \
            f"https://rupload.facebook.com/ig-api-upload/{CFG['graph_api_version']}/{container}"
        data = video_path.read_bytes()
        u = requests.post(upload_url, data=data, timeout=300, headers={
            "Authorization": f"OAuth {env('IG_ACCESS_TOKEN')}", "offset": "0", "file_size": str(len(data))})
        if u.status_code != 200:
            sys.exit(f"Video-Upload fehlgeschlagen {u.status_code}: {u.text[:500]}\n"
                     "Tipp: in config.json \"upload_mode\": \"url\" setzen.")
    wait_container(container)
    return publish_container(container)


def record(c, media_id):
    state = load_state()
    state.append({k: c.get(k) for k in ("date", "topic", "format", "headline", "source", "link",
                                        "hook", "setup", "punchline", "score")}
                 | {"kind": "reel", "media_id": media_id,
                    "posted": dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes")})
    save_json(STATE_FILE, state)


def publish():
    if not PENDING_FILE.exists() or not VIDEO_FILE.exists():
        print("Kein vorbereitetes Reel vorhanden.")
        return
    c = json.loads(PENDING_FILE.read_text(encoding="utf-8"))
    media_id = publish_reel(c, VIDEO_FILE)
    record(c, media_id)
    PENDING_FILE.unlink()
    print(f"Veröffentlicht, Media-ID {media_id}")


def post_queue():
    items = sorted(QUEUE.glob("*.json")) if QUEUE.exists() else []
    posted = 0
    for meta in items:
        video = meta.with_suffix(".mp4")
        if not video.exists():          # .mp4 gelöscht = Witz aussortiert
            meta.unlink()
            continue
        if posted:
            time.sleep(60)
        c = json.loads(meta.read_text(encoding="utf-8"))
        media_id = publish_reel(c, video)
        record(c, media_id)
        video.unlink()
        meta.unlink()
        posted += 1
        print(f"Veröffentlicht ({posted}): {c['hook']} -> Media-ID {media_id}")
    print(f"{posted} Reels aus der Warteschlange veröffentlicht.")


# ------------------------------------------------------------- Wochen-Karussell

def weekly(dry=False):
    now = dt.datetime.now(dt.timezone.utc)
    reels = []
    for p in load_state():
        if p.get("kind", "reel") != "reel" or not p.get("media_id") or not p.get("punchline"):
            continue
        when = dt.datetime.fromisoformat(p["posted"]) if p.get("posted") else \
            dt.datetime.strptime(p["date"], "%Y-%m-%d-%H%M").replace(tzinfo=dt.timezone.utc)
        if now - when <= dt.timedelta(days=7):
            reels.append(p)
    if len(reels) < 3:
        print(f"Nur {len(reels)} Witze in den letzten 7 Tagen, kein Wochen-Karussell.")
        return

    if not dry:  # nach echter Resonanz sortieren: Kommentare zählen dreifach
        base, _, headers = ig()
        for p in reels:
            try:
                m = requests.get(f"{base}/{p['media_id']}", headers=headers,
                                 params={"fields": "like_count,comments_count"}, timeout=60).json()
                p["_rank"] = m.get("like_count", 0) + 3 * m.get("comments_count", 0)
            except requests.RequestException:
                p["_rank"] = 0
    for p in reels:
        p.setdefault("_rank", 0)
    top = sorted(reels, key=lambda p: (p["_rank"], p.get("score") or 0), reverse=True)[:5]
    n = len(top)

    period = f"{(now - dt.timedelta(days=6)).strftime(T_['date'])} – {now.strftime(T_['date'])}"
    slides = [slide_cover(n, period)]
    slides += [slide_joke(rank, top[rank - 1]) for rank in range(n, 0, -1)]   # Countdown bis Platz 1
    slides.append(slide_end())

    OUT.mkdir(exist_ok=True)
    stamp = now.strftime("%Y-%m-%d")
    paths = []
    for i, img in enumerate(slides, 1):
        path = (OUT if dry else MEDIA / f"woche-{stamp}") / f"folie-{i:02d}.jpg"
        path.parent.mkdir(parents=True, exist_ok=True)
        img.save(path, "JPEG", quality=92)
        paths.append(path)

    tags = " ".join(CFG.get("weekly_hashtags", CFG["always_hashtags"])[:5])
    cap = f"{T_['weekly_caption'].format(n=n)}\n\n{tags}"
    print(cap)
    if dry:
        print(f"\n{len(paths)} Folien in out/ gerendert (Testlauf, nichts gepostet).")
        return

    urls = push_public(paths, f"Wochen-Karussell {stamp}")
    children = []
    for url in urls:
        cid = create_container({"image_url": url, "is_carousel_item": True})["id"]
        wait_container(cid, minutes=5)
        children.append(cid)
    container = create_container({"media_type": "CAROUSEL", "children": ",".join(children), "caption": cap})["id"]
    wait_container(container, minutes=5)
    media_id = publish_container(container)
    state = load_state()
    state.append({"kind": "carousel", "date": stamp, "media_id": media_id,
                  "items": [p["media_id"] for p in top]})
    save_json(STATE_FILE, state)
    print(f"Wochen-Karussell veröffentlicht, Media-ID {media_id}")


if __name__ == "__main__":
    args = sys.argv[1:]
    if args[:1] == ["prepare"]:
        prepare()
    elif args[:1] == ["publish"]:
        publish()
    elif args[:1] == ["startpaket"]:
        startpaket(int(args[1]) if len(args) > 1 else 6)
    elif args[:1] == ["queue"]:
        post_queue()
    elif args[:1] == ["check"]:
        check()
    elif args[:1] == ["weekly"]:
        weekly(dry="--dry" in args)
    else:
        sys.exit(__doc__)
