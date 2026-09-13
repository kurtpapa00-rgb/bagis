#!/usr/bin/env python3
"""
NEXUS ODDS — Günlük Veri Çekme Scripti
========================================
Bu script GitHub Actions üzerinde her gün otomatik çalışır ve:
  1) API-Football'dan (v3.football.api-sports.io) o günün TÜM dünya
     fikstürlerini tek istekte çeker, bizim 15 ligimize filtreler.
  2) football-data.org'un ücretsiz planında olan 7 ligimiz için
     standings endpoint'inden takım formunu (son 5 maç) çeker.
  3) football-data.org'da OLMAYAN 8 ligimiz için formu API-Football'un
     standings endpoint'inden çeker.
  4) Sonucu data/daily.json dosyasına yazar. Bu dosya GitHub Actions
     tarafından otomatik commit'lenir ve raw.githubusercontent.com
     üzerinden web uygulamasına servis edilir.

Gerekli ortam değişkenleri (GitHub repo secrets olarak eklenmeli):
  API_FOOTBALL_KEY   -> https://dashboard.api-football.com (ücretsiz kayıt)
  FOOTBALL_DATA_KEY  -> https://www.football-data.org/client/register (ücretsiz kayıt)

Toplam günlük API kullanımı (normal şartlarda):
  API-Football : 1 (fikstür) + 8 (standings) = 9 istek  (limit: 100/gün)
  football-data.org : 7 istek (limit: 10/dakika, günlük sabit limit yok)
"""

import os
import sys
import json
import time
import datetime
from zoneinfo import ZoneInfo

try:
    import requests
except ImportError:
    print("HATA: 'requests' kütüphanesi bulunamadı. 'pip install requests' ile kurun.")
    sys.exit(1)

# ------------------------------------------------------------------
# AYARLAR
# ------------------------------------------------------------------

API_FOOTBALL_KEY = os.environ.get("API_FOOTBALL_KEY", "").strip()
FOOTBALL_DATA_KEY = os.environ.get("FOOTBALL_DATA_KEY", "").strip()

if not API_FOOTBALL_KEY:
    print("UYARI: API_FOOTBALL_KEY tanımlı değil — fikstür ve bazı form verileri çekilemeyecek.")
if not FOOTBALL_DATA_KEY:
    print("UYARI: FOOTBALL_DATA_KEY tanımlı değil — 7 lig için form verisi çekilemeyecek.")

TZ = ZoneInfo("Europe/Istanbul")
NOW = datetime.datetime.now(TZ)
TODAY_STR = NOW.strftime("%Y-%m-%d")

AF_BASE = "https://v3.football.api-sports.io"
AF_HEADERS = {"x-apisports-key": API_FOOTBALL_KEY}

FD_BASE = "https://api.football-data.org/v4"
FD_HEADERS = {"X-Auth-Token": FOOTBALL_DATA_KEY}

# our_code -> {af_id, fd_code (None if not covered by football-data.org free plan), display name}
# NOT: API-Football lig ID'leri her sezon değişmez ama olası bir hata durumunda
# aşağıdaki id'leri https://dashboard.api-football.com/soccer/ids adresinden
# doğrulayabilirsin. Script çalışınca konsola eşleşen lig adlarını da basar,
# bir uyuşmazlık görürsen orada fark edersin.
LEAGUE_MAP = {
    "E0":  {"af_id": 39,  "fd_code": "PL",  "name": "İngiltere Premier Lig"},
    "E1":  {"af_id": 40,  "fd_code": "ELC", "name": "İngiltere Championship"},
    "E2":  {"af_id": 41,  "fd_code": None,  "name": "İngiltere League One"},
    "E3":  {"af_id": 42,  "fd_code": None,  "name": "İngiltere League Two"},
    "D1":  {"af_id": 78,  "fd_code": "BL1", "name": "Almanya Bundesliga"},
    "D2":  {"af_id": 79,  "fd_code": None,  "name": "Almanya 2. Bundesliga"},
    "I1":  {"af_id": 135, "fd_code": "SA",  "name": "İtalya Serie A"},
    "I2":  {"af_id": 136, "fd_code": None,  "name": "İtalya Serie B"},
    "SP1": {"af_id": 140, "fd_code": "PD",  "name": "İspanya La Liga"},
    "SP2": {"af_id": 141, "fd_code": None,  "name": "İspanya Segunda"},
    "N1":  {"af_id": 88,  "fd_code": "DED", "name": "Hollanda Eredivisie"},
    "P1":  {"af_id": 94,  "fd_code": "PPL", "name": "Portekiz Primeira Liga"},
    "B1":  {"af_id": 144, "fd_code": None,  "name": "Belçika 1. Lig"},
    "SC0": {"af_id": 179, "fd_code": None,  "name": "İskoçya Premiership"},
    "T1":  {"af_id": 203, "fd_code": None,  "name": "Türkiye Süper Lig"},
}

AF_ID_TO_CODE = {v["af_id"]: k for k, v in LEAGUE_MAP.items()}


def current_season_year(today: datetime.date) -> int:
    """Avrupa liglerinde sezon Ağustos civarı başlar. Ocak-Haziran arasıysa
    bir önceki yılın sezonu (örn. Mart 2026 -> sezon 2025) sayılır."""
    return today.year if today.month >= 7 else today.year - 1


SEASON = current_season_year(NOW.date())


def safe_get(url, headers, params=None, retries=2, timeout=20):
    for attempt in range(retries + 1):
        try:
            r = requests.get(url, headers=headers, params=params, timeout=timeout)
            if r.status_code == 200:
                return r.json()
            print(f"  [!] {url} -> HTTP {r.status_code}: {r.text[:200]}")
        except requests.RequestException as e:
            print(f"  [!] {url} -> istisna: {e}")
        if attempt < retries:
            time.sleep(2)
    return None


def parse_form_string(form_str: str, source: str):
    """'WWDLW' gibi bir string alır, en fazla son 5 sonucu kullanır."""
    if not form_str:
        return None
    results = [c for c in form_str.strip().upper() if c in ("W", "D", "L")][-5:]
    if not results:
        return None
    pts = sum(3 if r == "W" else 1 if r == "D" else 0 for r in results)
    return {
        "form": "".join(results),
        "points5": pts,
        "matches": len(results),
        "source": source,
    }


# ------------------------------------------------------------------
# 1) BUGÜNÜN FİKSTÜRLERİ (tek istekte tüm dünya, sonra filtrele)
# ------------------------------------------------------------------

def fetch_today_fixtures():
    if not API_FOOTBALL_KEY:
        return []
    print(f"[1/3] API-Football: {TODAY_STR} tarihli tüm fikstürler çekiliyor...")
    data = safe_get(f"{AF_BASE}/fixtures", AF_HEADERS, params={"date": TODAY_STR})
    if not data:
        print("  [!] Fikstür verisi alınamadı.")
        return []
    fixtures = []
    for item in data.get("response", []):
        lid = item.get("league", {}).get("id")
        code = AF_ID_TO_CODE.get(lid)
        if not code:
            continue
        fx = item.get("fixture", {})
        teams = item.get("teams", {})
        fixtures.append({
            "league": code,
            "league_name": LEAGUE_MAP[code]["name"],
            "kickoff_utc": fx.get("date"),
            "status": fx.get("status", {}).get("short"),
            "home": teams.get("home", {}).get("name"),
            "away": teams.get("away", {}).get("name"),
            "home_id": teams.get("home", {}).get("id"),
            "away_id": teams.get("away", {}).get("id"),
            "venue": fx.get("venue", {}).get("name"),
        })
    print(f"  -> {len(fixtures)} maç bulundu (bizim liglerimizden).")
    return fixtures


# ------------------------------------------------------------------
# 2) FORM VERİSİ — football-data.org (7 lig) + API-Football (8 lig)
# ------------------------------------------------------------------

def fetch_form_football_data():
    team_form = {}
    if not FOOTBALL_DATA_KEY:
        return team_form
    print("[2/3] football-data.org: standings/form çekiliyor (7 lig)...")
    for code, meta in LEAGUE_MAP.items():
        if not meta["fd_code"]:
            continue
        data = safe_get(f"{FD_BASE}/competitions/{meta['fd_code']}/standings", FD_HEADERS)
        time.sleep(6)  # 10 istek/dk limiti için güvenli bekleme
        if not data:
            continue
        for table in data.get("standings", []):
            if table.get("type") != "TOTAL":
                continue
            for row in table.get("table", []):
                name = row.get("team", {}).get("name")
                form = parse_form_string(row.get("form", ""), source="football-data.org")
                if name and form:
                    team_form[name] = {**form, "league": code, "position": row.get("position")}
        print(f"  -> {meta['name']} ({meta['fd_code']}) tamam.")
    return team_form


def fetch_form_api_football():
    team_form = {}
    if not API_FOOTBALL_KEY:
        return team_form
    print("[3/3] API-Football: standings/form çekiliyor (8 lig)...")
    for code, meta in LEAGUE_MAP.items():
        if meta["fd_code"]:
            continue  # bu ligler football-data.org'dan alındı
        data = safe_get(f"{AF_BASE}/standings", AF_HEADERS,
                         params={"league": meta["af_id"], "season": SEASON})
        if not data:
            continue
        try:
            standings_groups = data["response"][0]["league"]["standings"]
        except (IndexError, KeyError):
            print(f"  [!] {meta['name']}: standings formatı beklenmedik.")
            continue
        for group in standings_groups:
            for row in group:
                name = row.get("team", {}).get("name")
                form = parse_form_string(row.get("form", ""), source="api-football")
                if name and form:
                    team_form[name] = {**form, "league": code, "position": row.get("rank")}
        print(f"  -> {meta['name']} tamam.")
    return team_form


# ------------------------------------------------------------------
# ANA AKIŞ
# ------------------------------------------------------------------

def main():
    fixtures = fetch_today_fixtures()
    form_fd = fetch_form_football_data()
    form_af = fetch_form_api_football()

    team_form = {}
    team_form.update(form_af)
    team_form.update(form_fd)  # aynı isimde çakışma olursa football-data öncelikli

    output = {
        "generated_at": NOW.isoformat(),
        "date": TODAY_STR,
        "season": SEASON,
        "fixtures": fixtures,
        "team_form": team_form,
        "meta": {
            "fixtures_count": len(fixtures),
            "teams_with_form": len(team_form),
        },
    }

    os.makedirs("data", exist_ok=True)
    out_path = os.path.join("data", "daily.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\nTamamlandı -> {out_path}")
    print(f"  Fikstür sayısı : {len(fixtures)}")
    print(f"  Form bulunan takım sayısı : {len(team_form)}")

    if not fixtures and not team_form:
        print("HATA: Hiçbir veri çekilemedi (API anahtarları doğru mu kontrol et).")
        sys.exit(1)


if __name__ == "__main__":
    main()
