import os
import io
import csv
import json
import logging
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import gspread
import dotenv
from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectTooManyRequestsError,
    GarminConnectConnectionError,
)

dotenv.load_dotenv()

days_back_to_fetch = 5  # Liczba dni wstecz do pobrania danych z Garmina

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN_DIR = "./.garmin_tokens"
WARSAW_TZ = ZoneInfo("Europe/Warsaw")

def _activity_covers_date(act, check_date):
    """Czy aktywność (mogąca trwać kilka dni) obejmuje swoim czasem trwania podany dzień."""
    start_str = act.get('startTimeLocal', '')
    if not start_str:
        return False
    try:
        start_dt = datetime.strptime(start_str[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return False
    end_dt = start_dt + timedelta(seconds=act.get('duration', 0) or 0)
    check_dt = datetime.strptime(check_date, "%Y-%m-%d")
    return start_dt.date() <= check_dt.date() <= end_dt.date()


def get_garmin_client():
    email = os.getenv("GARMIN_EMAIL")
    password = os.getenv("GARMIN_PASSWORD")
    client = Garmin(email=email, password=password)
    client.login(tokenstore=TOKEN_DIR)
    return client

def fetch_garmin_data(client, daty_list):
    activities = client.get_activities_by_date(min(daty_list), max(daty_list), sortorder="asc")
    activities.sort(key=lambda act: act.get('startTimeLocal', ''))

    dzienne_statystyki = {}
    for data_badania in daty_list:
        hrv_summary = "Brak"
        rhr = "Brak"
        total_kcal = "Brak"
        active_kcal = "Brak"
        bmr_kcal = "Brak"
        avg_stress = "Brak"
        body_battery_min = "Brak"

        try:
            hrv = client.get_hrv_data(data_badania)
            if hrv:
                hrv_summary = (
                    hrv.get('hrvSummary', {}).get('lastNightAvg', "Brak")
                    if 'hrvSummary' in hrv
                    else hrv.get('lastNightAvg', "Brak")
                )
        except Exception as e:
            logger.warning(f"Nie udało się pobrać HRV dla {data_badania}: {e}")

        try:
            summary = client.get_user_summary(data_badania)
            rhr = summary.get('restingHeartRate', "Brak")
            total_kcal = summary.get('totalKilocalories', "Brak")
            active_kcal = summary.get('activeKilocalories', "Brak")
            bmr_kcal = summary.get('bmrKilocalories', "Brak")
            avg_stress = summary.get('averageStressLevel', "Brak")
            body_battery_min = summary.get('bodyBatteryLowestValue', "Brak")
        except Exception as e:
            logger.warning(f"Nie udało się pobrać podsumowania dnia dla {data_badania}: {e}")

        dzienne_statystyki[data_badania] = {
            "hrv": hrv_summary,
            "rhr": rhr,
            "garmin_load": 0.0,
            "znaleziono_load": False,
            "total_kcal": total_kcal,
            "active_kcal": active_kcal,
            "bmr_kcal": bmr_kcal,
            "avg_stress": avg_stress,
            "body_battery_min": body_battery_min
        }

    # Pobieramy aktywności z całego zakresu dat
    recent_activities = [
        act for act in activities
        if any(act.get('startTimeLocal', '').startswith(d) for d in daty_list)
    ]

    hr_zones_dict = {}
    activity_extra_metrics = {}

    for act in recent_activities:
        act_id = str(act['activityId'])
        exact_date = act.get('startTimeLocal', '')
        data_aktywnosci = exact_date[:10]

        load = act.get('activityTrainingLoad') or act.get('trainingLoad') or 0.0
        aerobic_te = 0.0
        anaerobic_te = 0.0
        z1 = z2 = z3 = z4 = z5 = 0.0
        pasek_hr = "Nie"

        try:
            zones_data = client.get_activity_hr_in_timezones(act_id)
            if isinstance(zones_data, list):
                for zone in zones_data:
                    zn = zone.get('zoneNumber')
                    mins = round(zone.get('secsInZone', 0) / 60.0, 2)
                    if zn == 1: z1 = mins
                    elif zn == 2: z2 = mins
                    elif zn == 3: z3 = mins
                    elif zn == 4: z4 = mins
                    elif zn == 5: z5 = mins
        except: pass

        try:
            act_details = client.get_activity(act_id)
            summary_dto = act_details.get('summaryDTO', {})
            if not load or load == 0.0:
                load = summary_dto.get('activityTrainingLoad') or summary_dto.get('trainingLoad') or 0.0

            aerobic_te = summary_dto.get('trainingEffect', 0.0)
            anaerobic_te = summary_dto.get('anaerobicTrainingEffect', 0.0)

            czujniki = act_details.get("metadata", {}).get("sensors", [])
            for czujnik in czujniki:
                if "HEART" in str(czujnik).upper() or "HRM" in str(czujnik).upper():
                    pasek_hr = "Tak"; break
        except: pass

        if load and float(load) > 0:
            if data_aktywnosci in dzienne_statystyki:
                dzienne_statystyki[data_aktywnosci]["garmin_load"] += float(load)
                dzienne_statystyki[data_aktywnosci]["znaleziono_load"] = True

        hr_zones_dict[act_id] = [z1, z2, z3, z4, z5, pasek_hr]
        activity_extra_metrics[act_id] = {"aerobic_te": aerobic_te or "0.0", "anaerobic_te": anaerobic_te or "0.0"}

    for d in daty_list:
        if dzienne_statystyki[d]["znaleziono_load"]:
            dzienne_statystyki[d]["garmin_load"] = str(round(dzienne_statystyki[d]["garmin_load"]))
        else:
            dzienne_statystyki[d]["garmin_load"] = "0"

    return activities, dzienne_statystyki, hr_zones_dict, activity_extra_metrics


def oblicz_fizjologie(wszystkie_dane, dzis):
    """
    Wylicza CTL, ATL, TSB, ACWR oraz Trend HRV na podstawie danych historycznych z arkusza.
    Zakładamy indeksy (liczone od 0, gdzie A=0, B=1, C=2...):
    - Data: kolumna B (indeks 1)
    - HRV: kolumna E (indeks 4)
    - Obciążenie (TSS / Garmin Load / sRPE): kolumna G (indeks 6) - DOSTOSUJ W RAZIE POTRZEBY
    """
    historia = {}

    # 1. Zbieranie i czyszczenie danych (nagłówek jest odfiltrowywany przez walidację formatu daty poniżej)
    for row in wszystkie_dane:
        if len(row) > 1 and row[1].strip():
            data_str = row[1].strip()
            try:
                # Weryfikacja formatu daty
                datetime.strptime(data_str, "%Y-%m-%d")

                # Parsowanie obciążenia (kolumna G)
                load_val = 0.0
                if len(row) > 6 and row[6].strip().replace('.', '', 1).isdigit():
                    load_val = float(row[6].strip())

                # Parsowanie HRV (kolumna E)
                hrv_val = None
                if len(row) > 4 and row[4].strip().replace('.', '', 1).isdigit():
                    hrv_val = float(row[4].strip())

                historia[data_str] = {"load": load_val, "hrv": hrv_val}
            except ValueError:
                continue

    # 2. Sortowanie dat chronologicznie, bierzemy pod uwagę wszystko aż do wczoraj
    wczoraj_str = (dzis - timedelta(days=1)).strftime("%Y-%m-%d")
    posortowane_daty = sorted([d for d in historia.keys() if d <= wczoraj_str])

    ctl = 0.0  # Chronic Training Load (Przewlekłe)
    atl = 0.0  # Acute Training Load (Ostre)
    lista_hrv = []

    # 3. Modelowanie matematyczne dzień po dniu
    for data in posortowane_daty:
        obciazenie = historia[data]["load"]

        # Wzory wykładniczej średniej kroczącej (EWMA) stosowane w kolarstwie i bieganiu
        ctl = ctl + (obciazenie - ctl) / min(42.0, len(posortowane_daty))
        atl = atl + (obciazenie - atl) / min(7.0, len(posortowane_daty))  # Ograniczamy do 7 dni dla ATL

        hrv = historia[data]["hrv"]
        if hrv:
            lista_hrv.append(hrv)

    # 4. Wyliczenia wskaźników końcowych
    tsb = ctl - atl  # Training Stress Balance (Forma)
    acwr = atl / ctl if ctl > 0 else 0.0  # Acute-to-Chronic Ratio

    # Trend HRV
    hrv_trend_str = "Brak danych"
    if len(lista_hrv) >= 2:
        # Bierzemy średnią maksymalnie z ostatnich 30 wpisów
        ostatnie_30 = lista_hrv[-30:]
        srednia_30d = sum(ostatnie_30) / len(ostatnie_30)
        ostatnie_hrv = lista_hrv[-1]

        delta_hrv = ((ostatnie_hrv - srednia_30d) / srednia_30d) * 100
        hrv_trend_str = f"{delta_hrv:+.1f}%"

    # 5. Budowanie tabeli wynikowej dla LLM
    tabela = "METRYKA,WARTOŚĆ\n"
    tabela += f"CTL (Baza/Fitness),{ctl:.1f}\n"
    tabela += f"ATL (Zmęczenie),{atl:.1f}\n"
    tabela += f"TSB (Świeżość/Forma),{tsb:.1f}\n"
    tabela += f"ACWR (Wskaźnik przeciążenia),{acwr:.2f}\n"
    tabela += f"HRV Trend (vs 30d),{hrv_trend_str}\n"

    logger.info(f"Wyliczono wskaźniki: CTL={ctl:.1f}, ATL={atl:.1f}, TSB={tsb:.1f}")
    return tabela


def plan_upcoming_days(sheet, wszystkie_dane, dzis):
    """Planuje kolejne 7 dni treningowych (Trener AI) na podstawie wskaźników fizjologicznych."""
    logger.info("Uruchamianie Trenera AI (planowanie kolejnych dni)...")

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        logger.error("Brak klucza GEMINI_API_KEY w pliku .env!")
        return

    # Do analizy (tabela fizjologii, CSV dla LLM) bierzemy tylko ostatnie 42 wiersze —
    # CTL jest liczone jako 42-dniowa średnia wykładnicza, więc potrzebuje całego tego okna.
    # Wyszukiwanie miejsca na plan (poniżej) wciąż działa na całym arkuszu, bo potrzebuje
    # prawdziwych numerów wierszy.
    historia_do_analizy = wszystkie_dane[-42:]

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerows(historia_do_analizy)
    csv_string = output.getvalue()

    try:
        with open("strategia.txt", "r", encoding="utf-8") as f:
            strategia = f.read()
    except FileNotFoundError:
        logger.error("Brak pliku strategia.txt!")
        return

    kolejne_7_dni = [(dzis + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(1, 8)]

    # MAGIA DZIEJE SIĘ TUTAJ - wywołujemy kalkulator fizjologii
    tabela_wskaznikow = oblicz_fizjologie(historia_do_analizy, dzis)

    prompt = f"""
    Jesteś profesjonalnym trenerem. Poniżej znajduje się moja ogólna strategia z konkretnymi ramami czasowymi:
    {strategia}

    To są moje matematycznie wyliczone wskaźniki fizjologiczne na stan dzisiejszy (trendy długoterminowe):
    {tabela_wskaznikow}

    A oto pełny zrzut mojego dziennika treningowego z ostatnich 30 dni w formacie CSV:
    {csv_string}

    [KRYTYCZNA HIERARCHIA ANALIZY DANYCH]
    Przed ułożeniem planu, musisz przeanalizować dane w następującej kolejności:
    1. ZDROWIE I REGENERACJA (PRIORYTET ABSOLUTNY): Przeanalizuj w pliku CSV wyłącznie ostatnie 3 do 5 dni. Szukaj anomalii w kolumnach HRV (nagły spadek), RHR (nagły wzrost) oraz 'Body Battery' i 'Stress'. 
    2. Jeśli zauważysz sygnały infekcji, silnego stresu układu nerwowego lub braku regeneracji z ostatnich 48-72h, ZIGNORUJ długoterminowe wskaźniki TSB/CTL z tabelki. Wprowadź tryb awaryjny (REST lub ekstremalnie lekkie Z1), niezależnie od tego, co mówi kalendarz i strategia.
    3. Jeśli parametry dzienne z CSV są stabilne, przejdź do tabeli wskaźników fizjologicznych (CTL, ATL, TSB, ACWR), aby ocenić ogólną formę i zaplanować obciążenia zgodnie ze strategią.

    Twoim zadaniem jest zaplanować mi treningi na kolejne 7 dni, od {kolejne_7_dni[0]} do {kolejne_7_dni[-1]}.

    WYMÓG KRYTYCZNY: Zwróć wynik WYŁĄCZNIE jako surowy JSON. 
    Struktura JSON musi wyglądać DOKŁADNIE tak:
    {{
        "{kolejne_7_dni[0]}": {{"typ": "Krótka nazwa (np. Interwały)", "opis": "Szczegółowy opis (np. 5x1km tempo 4:00)"}},
        ...
    }}
    Ten skrypt jest uruchamiany codziennie, więc przeanalizuj obecny plan na najbliższe 7 dni i w razie potrzeby go zmodyfikuj, aby był zgodny z moją strategią i adekwatny do zmęczenia.

    Jeżeli modyfikujesz plan na dany trening - dodaj komentarz dotyczący powodu zmiany (np. "Zmieniono tempo z 4:30 na 4:15 ze względu na poprawę formy").
    Jeżeli nie ma potrzeby zmiany planu na dany dzień, pozostaw go CAŁKOWICIE bez zmian.

    WYMÓG KRYTYCZNY: Zwróć wynik WYŁĄCZNIE jako surowy JSON.
    Struktura JSON musi wyglądać DOKŁADNIE tak:
    {{
        "{kolejne_7_dni[0]}": {{"typ": "Krótka nazwa (np. Interwały)", "opis": "Szczegółowy opis (np. 5x1km tempo 4:00)"}},
        ...
    }}
    """

    logger.info("Wysyłanie zintegrowanych danych (z tabelą wskaźników) do API Google...")

    url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-flash-latest:generateContent"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json"}
    }
    headers = {
        'Content-Type': 'application/json',
        'X-goog-api-key': api_key
    }

    try:
        response = requests.post(url, headers=headers, json=payload)
        response.raise_for_status()

        dane_odpowiedzi = response.json()
        tekst_ai = dane_odpowiedzi['candidates'][0]['content']['parts'][0]['text']

        surowy_json = tekst_ai.replace('```json', '').replace('```', '').strip()
        wygenerowany_plan = json.loads(surowy_json)
        logger.info("Odebrano i poprawnie rozkodowano plan od AI!")

    except requests.exceptions.RequestException as e:
        logger.error(f"Błąd komunikacji HTTP: {e}")
        if response is not None:
            logger.error(f"Szczegóły błędu: {response.text}")
        return
    except (KeyError, IndexError, json.JSONDecodeError) as e:
        logger.error(f"Błąd parsowania: {e}\nSurowy tekst: {tekst_ai if 'tekst_ai' in locals() else 'Brak'}")
        return

    for data_planu in kolejne_7_dni:
        if data_planu not in wygenerowany_plan:
            continue

        typ = wygenerowany_plan[data_planu].get("typ", "REST")
        opis = wygenerowany_plan[data_planu].get("opis", "-")

        docelowy_wiersz = None
        pusty_wiersz = None

        for i, r_data in enumerate(wszystkie_dane):
            row_idx = i + 1
            if row_idx <= 1:
                continue

            r_id = r_data[0] if len(r_data) > 0 else ""
            r_date = r_data[1] if len(r_data) > 1 else ""

            if r_date == data_planu:
                docelowy_wiersz = row_idx
                break

            if r_date == "" and r_id == "" and pusty_wiersz is None:
                pusty_wiersz = row_idx

        if docelowy_wiersz:
            logger.info(f"Aktualizowanie (nadpisywanie) planu dla dnia {data_planu} (wiersz {docelowy_wiersz})...")
            sheet.batch_update([{'range': f"C{docelowy_wiersz}:D{docelowy_wiersz}", 'values': [[typ, opis]]}])

        elif pusty_wiersz:
            logger.info(f"Wypełnianie wolnego wiersza (nr {pusty_wiersz}) planem na dzień {data_planu}...")
            sheet.batch_update([
                {'range': f"B{pusty_wiersz}:D{pusty_wiersz}", 'values': [[data_planu, typ, opis]]},
                {'range': f"V{pusty_wiersz}", 'values': [[f"{data_planu} 00:00:00"]]}
            ])
            while len(wszystkie_dane) < pusty_wiersz:
                wszystkie_dane.append([""] * 22)
            wszystkie_dane[pusty_wiersz - 1][1] = data_planu

        else:
            logger.info(f"Brak wolnych wierszy. Dodawanie nowego wiersza na plan dla dnia: {data_planu}...")
            nowy_wiersz = ["", data_planu, typ, opis] + ([""] * 17) + [f"{data_planu} 00:00:00"]
            sheet.append_row(nowy_wiersz)
            wszystkie_dane.append(nowy_wiersz)

    logger.info("Plan zaktualizowany o modele matematyczne jest w Arkuszu!")


def main():
    logger.info(f"Synchronizacja: {days_back_to_fetch} dni wstecz.")
    dzis = datetime.now(WARSAW_TZ).date()
    daty_do_pobrania = [(dzis - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days_back_to_fetch)] # [dzis, wczoraj, przedwczoraj]

    try:
        client = get_garmin_client()
        activities, dzienne_statystyki, hr_zones_dict, activity_extra_metrics = fetch_garmin_data(client, daty_do_pobrania)
    except GarminConnectAuthenticationError as e:
        logger.error(f"Błąd autoryzacji Garmina — sprawdź GARMIN_EMAIL/GARMIN_PASSWORD: {e}")
        raise
    except GarminConnectTooManyRequestsError as e:
        logger.error(f"Garmin zablokował logowanie (rate limit): {e}")
        raise
    except GarminConnectConnectionError as e:
        logger.error(f"Błąd połączenia z Garminem: {e}")
        raise

    # Tworzenie brakujących wpisów REST
    for check_date in daty_do_pobrania:
        acts_for_date = [act for act in activities if act.get('startTimeLocal', '').startswith(check_date)]

        if not acts_for_date:
            is_covered = any(_activity_covers_date(act, check_date) for act in activities)
            if is_covered:
                logger.info(f"Dzień {check_date} objęty wieloniowym treningiem — wpis kontynuacji.")
            else:
                logger.info(f"Brak treningów dla {check_date}. Tworzenie pustego wpisu z dziennymi statystykami.")
            dummy_act = {
                'activityId': f"REST_{check_date}",
                'startTimeLocal': f"{check_date} 00:00:00",
                'activityName': 'Kontynuacja treningu wieloniowego' if is_covered else 'Dzień bez treningu',
                'activityType': {'typeKey': 'w trakcie' if is_covered else '-'},
                'distance': 0,
                'duration': 0,
                'averageHR': '-',
                'maxHR': '-'
            }
            activities.append(dummy_act)

    aktywnosci_do_dodania = [
        act for act in activities
        if any(act.get('startTimeLocal', '').startswith(d) for d in daty_do_pobrania)
    ]

    aktywnosci_do_dodania.sort(key=lambda act: act.get('startTimeLocal', ''))

    logger.info(f"Przetwarzanie {len(aktywnosci_do_dodania)} wpisów z wczoraj i dzisiaj...")

    try:
        logger.info("Łączenie z Google Sheets...")
        gc = gspread.service_account(filename='credentials.json')

        sheet_id = os.environ.get("SHEET_ID")
        sheet = gc.open_by_key(sheet_id).sheet1

        wszystkie_dane = sheet.get_all_values()
        plany_dzienne1 = {}
        plany_dzienne2 = {}
        for r_data in wszystkie_dane:
            if len(r_data) >= 3:
                r_date = r_data[1]
                r_plan1 = r_data[2]
                r_plan2 = r_data[3] if len(r_data) >= 4 else ""
                if r_date and r_plan1.strip() and r_date not in plany_dzienne1:
                    plany_dzienne1[r_date] = r_plan1
                if r_date and r_plan2.strip() and r_date not in plany_dzienne2:
                    plany_dzienne2[r_date] = r_plan2
    except Exception as e:
        logger.error(f"Błąd łączenia z arkuszem: {e}")
        return

    # Utrzymujemy lokalną kopię danych arkusza i aktualizujemy ją po każdym zapisie,
    # zamiast odczytywać cały arkusz ponownie przy każdej aktywności (Google Sheets API
    # ma limit odczytów na minutę, a 40+ odczytów w ciągu sekundy go przekracza — błąd 429).
    existing_ids = [r[0] if len(r) > 0 else "" for r in wszystkie_dane]

    for activity in aktywnosci_do_dodania:
        activity_id = str(activity.get('activityId'))

        is_existing_workout = False
        if activity_id in existing_ids and not activity_id.startswith("REST_"):
            is_existing_workout = True

        exact_date = activity.get('startTimeLocal', '')
        data_aktywnosci = exact_date[:10]
        staty_dnia = dzienne_statystyki.get(data_aktywnosci, {})

        dane_hr = hr_zones_dict.get(activity_id, [0.0, 0.0, 0.0, 0.0, 0.0, "Nie"])
        z1, z2, z3, z4, z5, pasek_hr = dane_hr

        name = activity.get('activityName', 'Brak nazwy')
        act_type = activity.get('activityType', {}).get('typeKey', 'unknown')

        dist = activity.get('distance', 0)
        dur = activity.get('duration', 0)
        distance_km = round(dist / 1000, 2) if dist else ""
        duration_min = round(dur / 60, 2) if dur else ""

        tempo_str = "-"
        if dist and dur and dist > 0:
            sekundy_na_km = dur / (dist / 1000)
            minuty = int(sekundy_na_km // 60)
            sekundy = int(sekundy_na_km % 60)
            tempo_str = f"{minuty}:{sekundy:02d}"

        avg_hr = activity.get('averageHR', '')
        max_hr = activity.get('maxHR', '')

        aero_te = activity_extra_metrics.get(activity_id, {}).get("aerobic_te", "0.0")
        anaero_te = activity_extra_metrics.get(activity_id, {}).get("anaerobic_te", "0.0")

# ... (kod pobierający dane bez zmian) ...

        hrv = staty_dnia.get("hrv", "Brak")
        rhr = staty_dnia.get("rhr", "Brak")
        g_load = staty_dnia.get("garmin_load", "0")
        t_kcal = staty_dnia.get("total_kcal", "Brak")
        a_kcal = staty_dnia.get("active_kcal", "Brak")
        b_kcal = staty_dnia.get("bmr_kcal", "Brak")
        # NOWOŚĆ: Wyciąganie nowych danych
        body_bat = staty_dnia.get("body_battery_min", "Brak")
        stress = staty_dnia.get("avg_stress", "Brak")

        if activity_id.startswith("REST_"):
            pasek_hr = "-"
            z1 = z2 = z3 = z4 = z5 = ""
            tempo_str = "-"

        plan_treningu1 = plany_dzienne1.get(data_aktywnosci, "")
        plan_treningu2 = plany_dzienne2.get(data_aktywnosci, "")

        trimp = z1 * 1 + z2 * 2 + z3 * 3 + z4 * 4 + z5 * 5

        # NOWOŚĆ: Zaktualizowany układ kolumn (K=BodyBat, L=Stress)
        row = [
            activity_id, data_aktywnosci, plan_treningu1, plan_treningu2,
            hrv, rhr, g_load, t_kcal, a_kcal, b_kcal, body_bat, stress, act_type,
            distance_km, duration_min, tempo_str, avg_hr, max_hr,
            pasek_hr, aero_te, anaero_te, z1, z2, z3, z4, z5, exact_date, trimp
        ]

        docelowy_wiersz = None

        if is_existing_workout:
            docelowy_wiersz = existing_ids.index(activity_id) + 1
        else:
            for i, r_data in enumerate(wszystkie_dane):
                row_idx = i + 1
                if row_idx <= 1:
                    continue
                r_id = r_data[0] if len(r_data) > 0 else ""
                r_date = r_data[1] if len(r_data) > 1 else ""

                if r_date == data_aktywnosci and (r_id == "" or r_id.startswith("REST_")):
                    docelowy_wiersz = row_idx
                    break

        if docelowy_wiersz:
            if is_existing_workout:
                logger.info(f"Odświeżanie statystyk dla istniejącego treningu {activity_id} (wiersz {docelowy_wiersz}).")
            else:
                logger.info(f"Nadpisywanie wiersza {docelowy_wiersz} aktywnością {activity_id}.")
            # NOWOŚĆ: Zakres rozszerzony do kolumny X
            sheet.batch_update([{'range': f"A{docelowy_wiersz}:AB{docelowy_wiersz}", 'values': [row]}])
            wszystkie_dane[docelowy_wiersz - 1] = row
            existing_ids[docelowy_wiersz - 1] = activity_id
        else:
            # Szukamy pozycji jako "tuż za ostatnim wierszem o dacie <= nowej" (a nie
            # "tuż przed pierwszym wierszem o dacie >"), żeby pojedynczy nieuporządkowany
            # wiersz gdzieś wcześniej w arkuszu nie przerywał skanu i nie wpychał wpisu na góre.
            insert_idx = 2

            for i in range(1, len(wszystkie_dane)):
                row_idx = i + 1
                r_data = wszystkie_dane[i]
                r_date = r_data[1] if len(r_data) > 1 else ""

                # Ukryta pełna data (exact_date) jest pod indeksem 26 (Kolumna AA)
                r_exact = r_data[26] if len(r_data) > 26 else ""

                if not r_date:
                    continue

                row_time = r_exact if r_exact else f"{r_date} 00:00:00"

                if row_time <= exact_date:
                    insert_idx = row_idx + 1

            if insert_idx > len(wszystkie_dane):
                logger.info(f"Dopisywanie nowego wiersza {activity_id} na samym dole arkusza...")
                sheet.append_row(row)
                wszystkie_dane.append(row)
                existing_ids.append(activity_id)
            else:
                logger.info(f"Wstawianie wiersza {activity_id} chronologicznie w pozycji {insert_idx}...")
                sheet.insert_rows([row], row=insert_idx)
                wszystkie_dane.insert(insert_idx - 1, row)
                existing_ids.insert(insert_idx - 1, activity_id)

    logger.info("Aktualizacja scalonych komórek i krawędzi (z białym tłem siatki!)...")
    wszystkie_ids_po_wstawieniu = sheet.col_values(1)

    def scal_i_formatuj_dla_daty(daty_list, sheet, activities, wszystkie_ids_po_wstawieniu):
        all_requests = []
        czarny = {"red": 0.0, "green": 0.0, "blue": 0.0}

        for data_str in daty_list:
            ids_dla_daty = [str(act.get('activityId')) for act in activities if act.get('startTimeLocal', '').startswith(data_str)]
            ids_dla_daty.append(f"REST_{data_str}")

            wiersze_w_arkuszu = [i + 1 for i, id_arkusz in enumerate(wszystkie_ids_po_wstawieniu) if id_arkusz in ids_dla_daty]

            if not wiersze_w_arkuszu:
                continue

            start_w, end_w = min(wiersze_w_arkuszu), max(wiersze_w_arkuszu)

            if len(wiersze_w_arkuszu) > 1:
                # 1. Najpierw rozwalamy stare scalenia (zapobiega błędowi 400 o istniejącym scaleniu)
                for col in ["B", "C", "D", "E", "F", "G", "H", "I", "J", "K", "L"]:
                    col_idx = ord(col) - 65
                    all_requests.append({
                        "unmergeCells": {
                            "range": {"sheetId": sheet.id, "startRowIndex": start_w-1, "endRowIndex": end_w, "startColumnIndex": col_idx, "endColumnIndex": col_idx+1}
                        }
                    })
                    # 2. Następnie aplikujemy nowe scalanie
                    all_requests.append({
                        "mergeCells": {
                            "range": {"sheetId": sheet.id, "startRowIndex": start_w-1, "endRowIndex": end_w, "startColumnIndex": col_idx, "endColumnIndex": col_idx+1},
                            "mergeType": "MERGE_ALL"
                        }
                    })

            # Formatowanie tekstu i dół — dla każdego dnia, niezależnie od liczby wierszy
            zakres = {"sheetId": sheet.id, "startRowIndex": start_w-1, "endRowIndex": end_w, "startColumnIndex": 0, "endColumnIndex": 28}
            all_requests.append({"repeatCell": {"range": zakres, "cell": {"userEnteredFormat": {"horizontalAlignment": "CENTER", "verticalAlignment": "MIDDLE"}}, "fields": "userEnteredFormat(horizontalAlignment,verticalAlignment)"}})
            all_requests.append({"updateBorders": {"range": zakres, "bottom": {"style": "SOLID_MEDIUM", "color": czarny}}})

            # Linie pionowe
            for col_idx in [3, 11, 20, 25]:
                all_requests.append({"updateBorders": {"range": {"sheetId": sheet.id, "startRowIndex": start_w-1, "endRowIndex": end_w, "startColumnIndex": col_idx, "endColumnIndex": col_idx+1}, "right": {"style": "SOLID_MEDIUM", "color": czarny}}})

        if all_requests:
            try:
                sheet.spreadsheet.batch_update({"requests": all_requests})
                logger.info("Pomyślnie rozszyto, scalono i sformatowano arkusz w jednym zapytaniu.")
            except Exception as e:
                logger.error(f"Błąd zbiorczego formatowania: {e}")

    try:
        scal_i_formatuj_dla_daty(daty_do_pobrania, sheet, activities, wszystkie_ids_po_wstawieniu)
    except Exception as e:
        logger.error(f"Nie udało się połączyć/sformatować komórek: {e}")

    logger.info("Pomyślnie zsynchronizowano dane w arkuszu!")

    # Po zsynchronizowaniu danych z Garmina, planujemy kolejne 7 dni na podstawie
    # zaktualizowanych wskaźników fizjologicznych (ten sam sheet/wszystkie_dane, bez ponownego odczytu).
    plan_upcoming_days(sheet, wszystkie_dane, dzis)

if __name__ == "__main__":
    main()
