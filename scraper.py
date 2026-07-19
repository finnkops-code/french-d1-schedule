import json
import re
import sys
from datetime import datetime, timezone, timedelta

SCHEDULE_URL = "https://ffbs.wbsc.org/fr/events/2026-championnat-de-france-division-1-baseball/schedule-and-results"
FALLBACK_URL = "https://ffbs.wbsc.org/fr/events/2026-championnat-de-france-division-1-baseball/home"

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

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# Wedstrijdblok in de platgeslagen HTML-tekst van ffbs.wbsc.org, bv.:
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


# ── Tier 1: curl_cffi ─────────────────────────────────────────────────────────

def fetch_curl_cffi(url):
    from curl_cffi import requests
    resp = requests.get(
        url,
        impersonate="chrome",
        timeout=30,
        headers={"Accept-Language": "fr-FR,fr;q=0.9"},
    )
    resp.raise_for_status()
    return resp.text


# ── Tier 2 + 3: Playwright / window.fetch ─────────────────────────────────────

def fetch_playwright(url):
    """
    Tier 2: pagina direct laden in headless Chromium.
    Tier 3: als dat een 403/challenge geeft, eerst de site-root laden en
    daarna de doelpagina via window.fetch vanuit de browsercontext ophalen —
    die request komt dan met alle cookies/fingerprint van een echte pagina.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
        )
        context = browser.new_context(
            user_agent=UA,
            locale="fr-FR",
            viewport={"width": 1366, "height": 900},
        )
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        page = context.new_page()

        # Tier 2: directe navigatie
        try:
            resp = page.goto(url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(3000)
            if resp and resp.status == 200:
                html = page.content()
                if GAME_RE.search(strip_tags(html)):
                    browser.close()
                    return html
            print(f"   Tier 2: status {resp.status if resp else '?'} of geen wedstrijden, door naar tier 3")
        except Exception as e:
            print(f"   Tier 2 mislukt ({e}), door naar tier 3")

        # Tier 3: window.fetch vanuit de paginacontext
        try:
            page.goto("https://ffbs.wbsc.org/fr/calendar",
                      wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(3000)
            html = page.evaluate(
                """async (url) => {
                    const r = await fetch(url, {
                        credentials: 'include',
                        headers: {'Accept-Language': 'fr-FR,fr;q=0.9'}
                    });
                    if (!r.ok) throw new Error('HTTP ' + r.status);
                    return await r.text();
                }""",
                url,
            )
            browser.close()
            return html
        except Exception as e:
            browser.close()
            raise RuntimeError(f"Tier 3 (window.fetch) mislukt: {e}")


def fetch_html(url):
    print(f"   Tier 1: curl_cffi...")
    try:
        html = fetch_curl_cffi(url)
        if GAME_RE.search(strip_tags(html)):
            return html
        print("   Tier 1: 200 maar geen wedstrijden gevonden, door naar Playwright")
    except Exception as e:
        print(f"   Tier 1 mislukt ({e}), door naar Playwright")

    print(f"   Tier 2/3: Playwright...")
    return fetch_playwright(url)


# ── Parsing ───────────────────────────────────────────────────────────────────

def strip_tags(html):
    html = re.sub(r"<script.*?</script>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r"<style.*?</style>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", text)


def speelronde_bounds():
    """
    Geeft de zaterdag en zondag van de meest recente speelronde.
    Speeldagen Division 1 2026: weekendseries op zaterdag + zondag.

    Logica:
    - Ma t/m vr → vorig weekend za + zo
    - Za t/m zo → dit weekend za + zo
    """
    now = datetime.now(timezone.utc) + timedelta(hours=2)
    today = now.date()
    weekday = today.weekday()  # 0=ma … 6=zo

    # Dagen terug tot de meest recente zaterdag
    days_since_saturday = (weekday - 5) % 7
    saturday = today - timedelta(days=days_since_saturday)
    sunday = saturday + timedelta(days=1)
    return saturday, sunday


def format_dutch_day(dt):
    days = ["maandag", "dinsdag", "woensdag", "donderdag",
            "vrijdag", "zaterdag", "zondag"]
    months = ["", "januari", "februari", "maart", "april", "mei", "juni",
              "juli", "augustus", "september", "oktober", "november", "december"]
    return f"{days[dt.weekday()]} {dt.day} {months[dt.month]}"


def parse_game(m):
    away_code, away_runs, home_runs, home_code, game_code, date_str, time_str, final = m.groups()

    try:
        start_dt = datetime.strptime(f"{date_str} {time_str}", "%d/%m/%Y %H:%M")
    except ValueError:
        start_dt = None

    played = final is not None

    return {
        "id":            game_code,
        "datum":         start_dt.strftime("%Y-%m-%d") if start_dt else None,
        "tijdstip":      start_dt.strftime("%H:%M") if start_dt else None,
        "dag":           format_dutch_day(start_dt) if start_dt else None,
        "thuis":         TEAM_NAMES.get(home_code, home_code),
        "thuis_code":    home_code,
        "uit":           TEAM_NAMES.get(away_code, away_code),
        "uit_code":      away_code,
        "score_thuis":   int(home_runs) if played else None,
        "score_uit":     int(away_runs) if played else None,
        # Innings per wedstrijd staan alleen op de box-score-pagina's,
        # niet op het schema; lege lijsten houden het schema gelijk
        # aan de Hoofdklasse-output.
        "thuis_innings": [],
        "uit_innings":   [],
        "innings":       None,
        "gamestatus":    "F" if played else "",
        "locatie":       None,
        "stadion":       None,
        "gespeeld":      played,
    }


def main():
    print(f"Ophalen van {SCHEDULE_URL}...")
    try:
        html = fetch_html(SCHEDULE_URL)
    except Exception as e:
        print(f"⚠️  Schemapagina mislukt ({e}), terugvallen op {FALLBACK_URL}")
        html = fetch_html(FALLBACK_URL)

    text = strip_tags(html)
    matches = list(GAME_RE.finditer(text))
    print(f"Wedstrijden gevonden: {len(matches)}")

    saturday, sunday = speelronde_bounds()
    today = (datetime.now(timezone.utc) + timedelta(hours=2)).date()
    print(f"Meest recente speelronde: {saturday} (za) t/m {sunday} (zo)")

    uitslagen = []
    programma = []
    seen = set()

    for m in matches:
        game = parse_game(m)
        if not game["datum"] or game["id"] in seen:
            continue
        seen.add(game["id"])
        game_date = datetime.strptime(game["datum"], "%Y-%m-%d").date()

        if game["gespeeld"] and saturday <= game_date <= sunday:
            uitslagen.append(game)
        elif not game["gespeeld"] and game_date > today:
            # Programma: alleen toekomstige wedstrijden
            programma.append(game)

    uitslagen.sort(key=lambda g: (g["datum"], g["tijdstip"] or ""))
    programma.sort(key=lambda g: (g["datum"], g["tijdstip"] or ""))
    programma = programma[:10]

    # Debug: print wat we gevonden hebben
    print(f"\nGespeelde wedstrijden in speelronde:")
    for u in uitslagen:
        print(f"  {u['datum']} {u['thuis']} {u['score_thuis']}-{u['score_uit']} {u['uit']}")

    if not matches:
        print("  ⚠️  Geen wedstrijden gevonden — eerste 2000 tekens van de pagina:")
        print(text[:2000])
        sys.exit(1)

    output = {
        "bijgewerkt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "bron": SCHEDULE_URL,
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
    print(f"   Uitslagen deze speelronde : {len(uitslagen)}")
    print(f"   Aankomende wedstrijden    : {len(programma)}")


if __name__ == "__main__":
    main()
