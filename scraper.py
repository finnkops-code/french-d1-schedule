import html as html_mod
import json
import os
import re
import sys
import time as time_mod
import urllib.parse
from datetime import datetime, timezone, timedelta, date as date_cls

from curl_cffi import requests

# ── Bronnen ───────────────────────────────────────────────────────────────────
# Primair:  baseballtv.fr/en/scores/?date=YYYYMMDD — WordPress, server-
#           gerenderd, geen bot-detectie, direct bereikbaar vanaf GitHub
#           Actions. Eén pagina per dag; toont ALLE Franse wedstrijden
#           (ook D2/softball), dus er wordt gefilterd op de 8 D1-teams.
# Fallback: WBSC-schemapagina — WAF blokkeert GitHub Actions-IP's; alleen
#           bereikbaar via de eigen Cloudflare Worker (zie worker.js) of,
#           met wat geluk, een publieke proxy.

BTV_SCORES_URL = "https://baseballtv.fr/en/scores/"
SCHEDULE_URL = "https://ffbs.wbsc.org/fr/events/2026-championnat-de-france-division-1-baseball/schedule-and-results"
FALLBACK_URL = "https://ffbs.wbsc.org/fr/events/2026-championnat-de-france-division-1-baseball/home"

# Cloudflare Worker fetch-proxy (zie worker.js). Zolang WORKER_URL leeg is
# wordt die tier overgeslagen. Instellen via env-variabelen in de workflow.
WORKER_URL = os.environ.get("WORKER_URL", "").rstrip("/")
WORKER_TOKEN = os.environ.get("WORKER_TOKEN", "")

TEAM_NAMES = {
    "BEZ": "Pirates de Béziers",
    "LAR": "Boucaniers de La Rochelle",
    "MTP": "Barracudas de Montpellier",
    "PUC": "Paris Université Club",
    "ROU": "Huskies de Rouen",
    "SAV": "Lions de Savigny-sur-Orge",
    "SEN": "Templiers de Sénart",
    "TOU": "Tigers de Toulouse",
}

# Teamnamen zoals baseballtv.fr ze schrijft → teamcode. Alleen wedstrijden
# waarvan BEIDE namen exact in deze lijst staan tellen mee — dat filtert
# D2-teams (Eysines, Anglet, "La Rochelle 2 Boucaniers", ...) er automatisch
# uit, ook al deelt bv. La Rochelle 2 hetzelfde logo als het D1-team.
BTV_D1_TEAMS = {
    "Montpellier Barracudas":    "MTP",
    "La Rochelle Boucaniers":    "LAR",
    "Sénart Templiers":          "SEN",
    "Paris Université Club":     "PUC",
    "Béziers Pirates":           "BEZ",
    "Savigny-sur-Orge Lions":    "SAV",
    "Rouen Huskies":             "ROU",
    "Stade Toulousain Tigers":   "TOU",
}

_names_alt = "|".join(re.escape(n) for n in sorted(BTV_D1_TEAMS, key=len, reverse=True))
# Wedstrijdblok op een baseballtv-dagpagina (platgeslagen tekst), bv.:
#   Baseball 13:00 Rouen Huskies — Stade Toulousain Tigers — Toulouse     (gepland)
#   Baseball 13:00 Rouen Huskies 5 Stade Toulousain Tigers 3 Toulouse     (gespeeld)
# Volgorde: uitteam eerst, thuisteam tweede (de locatie hoort bij team 2).
BTV_GAME_RE = re.compile(
    rf"Baseball\s+(\d{{1,2}}:\d{{2}})\s+"
    rf"({_names_alt})\s+(\d{{1,2}}|[—–-])\s+"
    rf"({_names_alt})\s+(\d{{1,2}}|[—–-])"
)

# Wedstrijdblok in de platgeslagen tekst van ffbs.wbsc.org (fallback), bv.:
#   Visiteurs BEZ 4 : 5 Recevant SEN D10505 09/05/2026 16:00 (UTC +2) - Score final
GAME_RE = re.compile(
    r"(?:Visiteurs|Away|Visitantes)\s+"
    r"([A-Z]{2,4})\s+"                      # uitcode
    r"(\d{1,2})\s*:\s*(\d{1,2})\s+"         # score uit : thuis
    r"(?:Recevant|Home|Local)\s+"
    r"([A-Z]{2,4})\s+"                      # thuiscode
    r"([A-Z][A-Z0-9]{3,11})\s+"             # wedstrijdcode, bv. D10505
    r"(\d{2}/\d{2}/\d{4})\s+(\d{2}:\d{2})"  # datum + tijd
    r"\s*\(UTC\s*\+?\d+\)"
    r"(\s*-\s*(?:Score final|Final score|Final))?",
    re.IGNORECASE,
)


def normalize_text(content):
    """
    Maakt zowel HTML als markdown plat naar doorzoekbare tekst:
    scripts/styles/tags eruit, markdown-afbeeldingen en -links eruit,
    HTML-entities gedecodeerd, whitespace samengevouwen.
    """
    content = re.sub(r"<script.*?</script>", " ", content, flags=re.DOTALL | re.IGNORECASE)
    content = re.sub(r"<style.*?</style>", " ", content, flags=re.DOTALL | re.IGNORECASE)
    content = re.sub(r"<[^>]+>", " ", content)
    content = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", content)   # markdown afbeeldingen
    content = re.sub(r"\[([^\]]*)\]\([^)]*\)", r" \1 ", content)  # markdown links
    content = re.sub(r"https?://\S+", " ", content)           # kale URLs
    content = html_mod.unescape(content)
    return re.sub(r"\s+", " ", content)


# ── Datums & helpers ──────────────────────────────────────────────────────────

def nu_fr():
    return datetime.now(timezone.utc) + timedelta(hours=2)


def speelronde_bounds():
    """
    Geeft de zaterdag en zondag van de meest recente speelronde.
    Speeldagen Division 1 2026: weekendseries op zaterdag en/of zondag.

    Logica:
    - Ma t/m vr → vorig weekend za + zo
    - Za t/m zo → dit weekend za + zo
    """
    today = nu_fr().date()
    days_since_saturday = (today.weekday() - 5) % 7
    saturday = today - timedelta(days=days_since_saturday)
    return saturday, saturday + timedelta(days=1)


def format_dutch_day(dt):
    days = ["maandag", "dinsdag", "woensdag", "donderdag",
            "vrijdag", "zaterdag", "zondag"]
    months = ["", "januari", "februari", "maart", "april", "mei", "juni",
              "juli", "augustus", "september", "oktober", "november", "december"]
    return f"{days[dt.weekday()]} {dt.day} {months[dt.month]}"


def maak_game(game_date, tijd, uit_code, thuis_code, score_uit, score_thuis, game_id, gestatus):
    dt = datetime.combine(game_date, datetime.strptime(tijd, "%H:%M").time()) if tijd else None
    played = score_uit is not None and score_thuis is not None
    return {
        "id":            game_id,
        "datum":         game_date.strftime("%Y-%m-%d"),
        "tijdstip":      tijd,
        "dag":           format_dutch_day(dt) if dt else format_dutch_day(datetime.combine(game_date, datetime.min.time())),
        "thuis":         TEAM_NAMES.get(thuis_code, thuis_code),
        "thuis_code":    thuis_code,
        "uit":           TEAM_NAMES.get(uit_code, uit_code),
        "uit_code":      uit_code,
        "score_thuis":   score_thuis,
        "score_uit":     score_uit,
        # Innings staan alleen op box-score-pagina's; lege lijsten houden
        # het schema gelijk aan de Hoofdklasse-output.
        "thuis_innings": [],
        "uit_innings":   [],
        "innings":       None,
        "gamestatus":    gestatus if played else "",
        "locatie":       None,
        "stadion":       None,
        "gespeeld":      played,
    }


# ── Primaire route: baseballtv.fr ────────────────────────────────────────────

def fetch_btv_day(d):
    """Haalt de scores-pagina van één dag op (platgeslagen tekst)."""
    url = f"{BTV_SCORES_URL}?date={d.strftime('%Y%m%d')}"
    resp = requests.get(
        url,
        impersonate="chrome",
        timeout=30,
        headers={"Accept-Language": "en-US,en;q=0.9"},
    )
    resp.raise_for_status()
    text = normalize_text(resp.text)
    if "Baseball" not in text and "Scores" not in text:
        raise RuntimeError(f"onverwachte inhoud op {url}")
    return text


def parse_btv_day(text, game_date):
    games = []
    for m in BTV_GAME_RE.finditer(text):
        tijd, uit_naam, s_uit, thuis_naam, s_thuis = m.groups()
        uit_code = BTV_D1_TEAMS[uit_naam]
        thuis_code = BTV_D1_TEAMS[thuis_naam]
        score_uit = int(s_uit) if s_uit.isdigit() else None
        score_thuis = int(s_thuis) if s_thuis.isdigit() else None
        # Alleen 'gespeeld' als beide scores er staan
        if score_uit is None or score_thuis is None:
            score_uit = score_thuis = None
        game_id = f"BTV-{game_date.strftime('%Y%m%d')}-{uit_code}-{thuis_code}-{tijd.replace(':', '')}"
        games.append(maak_game(game_date, tijd if len(tijd) == 5 else f"0{tijd}",
                               uit_code, thuis_code, score_uit, score_thuis,
                               game_id, "F"))
    return games


def btv_route():
    """
    Bouwt uitslagen + programma uit de per-dag-pagina's van baseballtv.fr.
    - Uitslagen: de za + zo van de meest recente speelronde
    - Programma: vr/za/zo-dagen in de komende 4 weken (D1 speelt in het
      weekend; vrijdag zit erbij voor eventuele playoff-series), tot er
      10 wedstrijden gevonden zijn
    """
    saturday, sunday = speelronde_bounds()
    today = nu_fr().date()

    uitslagen = []
    for d in (saturday, sunday):
        if d > today:
            continue
        print(f"   baseballtv {d}...")
        games = parse_btv_day(fetch_btv_day(d), d)
        uitslagen += [g for g in games if g["gespeeld"]]
        time_mod.sleep(0.4)

    programma = []
    d = today + timedelta(days=1)
    einde = today + timedelta(days=28)
    while d <= einde and len(programma) < 10:
        if d.weekday() in (4, 5, 6):  # vr, za, zo
            print(f"   baseballtv {d}...")
            games = parse_btv_day(fetch_btv_day(d), d)
            programma += [g for g in games if not g["gespeeld"]]
            time_mod.sleep(0.4)
        d += timedelta(days=1)

    uitslagen.sort(key=lambda g: (g["datum"], g["tijdstip"] or ""))
    programma.sort(key=lambda g: (g["datum"], g["tijdstip"] or ""))
    return uitslagen, programma[:10], BTV_SCORES_URL


# ── Fallback-route: WBSC via fetch-tiers ──────────────────────────────────────

def fetch_direct(url):
    resp = requests.get(
        url,
        impersonate="chrome",
        timeout=30,
        headers={"Accept-Language": "fr-FR,fr;q=0.9"},
    )
    resp.raise_for_status()
    return resp.text


def fetch_worker(url):
    worker = f"{WORKER_URL}/?url={urllib.parse.quote(url, safe='')}"
    if WORKER_TOKEN:
        worker += f"&token={urllib.parse.quote(WORKER_TOKEN, safe='')}"
    resp = requests.get(worker, timeout=60)
    resp.raise_for_status()
    return resp.text


def fetch_jina(url):
    resp = requests.get(f"https://r.jina.ai/{url}", timeout=60,
                        headers={"Accept": "text/plain"})
    resp.raise_for_status()
    return resp.text


def fetch_allorigins(url):
    resp = requests.get(
        f"https://api.allorigins.win/raw?url={urllib.parse.quote(url, safe='')}",
        timeout=60,
    )
    resp.raise_for_status()
    return resp.text


TIERS = [("direct (curl_cffi)", fetch_direct)]
if WORKER_URL:
    TIERS.append(("cloudflare worker", fetch_worker))
TIERS += [
    ("r.jina.ai reader", fetch_jina),
    ("allorigins",       fetch_allorigins),
]


def fetch_wbsc(url):
    for naam, fetcher in TIERS:
        print(f"   Tier: {naam}...")
        try:
            content = fetcher(url)
            if GAME_RE.search(normalize_text(content)):
                print(f"   ✓ Gelukt via {naam}")
                return content
            print(f"   ✗ {naam}: response zonder herkenbare wedstrijden")
        except Exception as e:
            print(f"   ✗ {naam} mislukt: {e}")
    raise RuntimeError("Alle WBSC fetch-tiers mislukt")


def parse_wbsc_game(m):
    away_code, away_runs, home_runs, home_code, game_code, date_str, time_str, final = m.groups()
    try:
        game_date = datetime.strptime(date_str, "%d/%m/%Y").date()
    except ValueError:
        return None
    played = final is not None
    return maak_game(
        game_date, time_str, away_code, home_code,
        int(away_runs) if played else None,
        int(home_runs) if played else None,
        game_code, "F",
    )


def wbsc_route():
    try:
        content = fetch_wbsc(SCHEDULE_URL)
        bron = SCHEDULE_URL
    except Exception as e:
        print(f"⚠️  Schemapagina mislukt ({e}), terugvallen op {FALLBACK_URL}")
        content = fetch_wbsc(FALLBACK_URL)
        bron = FALLBACK_URL

    text = normalize_text(content)
    saturday, sunday = speelronde_bounds()
    today = nu_fr().date()

    uitslagen, programma, seen = [], [], set()
    for m in GAME_RE.finditer(text):
        game = parse_wbsc_game(m)
        if not game or game["id"] in seen:
            continue
        seen.add(game["id"])
        game_date = datetime.strptime(game["datum"], "%Y-%m-%d").date()
        if game["gespeeld"] and saturday <= game_date <= sunday:
            uitslagen.append(game)
        elif not game["gespeeld"] and game_date > today:
            programma.append(game)

    uitslagen.sort(key=lambda g: (g["datum"], g["tijdstip"] or ""))
    programma.sort(key=lambda g: (g["datum"], g["tijdstip"] or ""))
    return uitslagen, programma[:10], bron


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    saturday, sunday = speelronde_bounds()
    print(f"Meest recente speelronde: {saturday} (za) t/m {sunday} (zo)")

    uitslagen, programma, bron = [], [], None

    print("Route 1: baseballtv.fr...")
    try:
        uitslagen, programma, bron = btv_route()
    except Exception as e:
        print(f"⚠️  baseballtv.fr-route mislukt: {e}")

    if not uitslagen and not programma:
        print("Route 2: WBSC (ffbs.wbsc.org)...")
        try:
            uitslagen, programma, bron = wbsc_route()
        except Exception as e:
            print(f"⚠️  WBSC-route mislukt: {e}")

    print(f"\nGespeelde wedstrijden in speelronde:")
    for u in uitslagen:
        print(f"  {u['datum']} {u['thuis']} {u['score_thuis']}-{u['score_uit']} {u['uit']}")
    print(f"Aankomende wedstrijden: {len(programma)}")

    if not uitslagen and not programma:
        print("⚠️  Geen wedstrijden gevonden via welke route dan ook")
        sys.exit(1)

    output = {
        "bijgewerkt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "bron": bron,
        "speelronde": {
            "van": str(saturday),
            "tot": str(sunday),
        },
        "uitslagen": uitslagen,
        "programma": programma,
    }

    with open("schedule.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\n✅ schedule.json opgeslagen")
    print(f"   Bron                      : {bron}")
    print(f"   Uitslagen deze speelronde : {len(uitslagen)}")
    print(f"   Aankomende wedstrijden    : {len(programma)}")


if __name__ == "__main__":
    main()
