import os
import logging
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

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN_DIR = "./.garmin_tokens"
WARSAW_TZ = ZoneInfo("Europe/Warsaw")


def get_garmin_client():
    email = os.getenv("GARMIN_EMAIL")
    password = os.getenv("GARMIN_PASSWORD")
    client = Garmin(email=email, password=password)
    client.login(tokenstore=TOKEN_DIR)
    return client


def fetch_garmin_data(client, wczoraj_str, dzis_str, limit=20):
    import json
    activities = client.get_activities(0, limit)

    dzienne_statystyki = {}
    for data_badania in [wczoraj_str, dzis_str]:
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
            
            # NOWOŚĆ: Wyciągamy średni stres i Body Battery z podsumowania dnia
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

    recent_activities = [
        act for act in activities
        if act.get('startTimeLocal', '').startswith(wczoraj_str) or act.get('startTimeLocal', '').startswith(dzis_str)
    ]

    hr_zones_dict = {}
    
    # Słownik do podpięcia dodatkowych metryk treningowych (TE, Stres itp.)
    activity_extra_metrics = {}

    for act in recent_activities:
        act_id = str(act['activityId'])
        exact_date = act.get('startTimeLocal', '')
        data_aktywnosci = exact_date[:10]
        logger.info(f"Pobieranie szczegółów treningu {act_id}...")

        # Poprawione szukanie load w głównym obiekcie aktywności
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
        except Exception as e:
            logger.warning(f"Nie udało się pobrać stref tętna dla {act_id}: {e}")

        try:
            act_details = client.get_activity(act_id)
            
            # Prawidłowa ścieżka do activityTrainingLoad wewnątrz summaryDTO
            summary_dto = act_details.get('summaryDTO', {})
            if not load or load == 0.0:
                load = summary_dto.get('activityTrainingLoad') or summary_dto.get('trainingLoad') or 0.0
            
            # Pobieramy Training Effect Aerobowy i Beztlenowy
            aerobic_te = summary_dto.get('trainingEffect', 0.0)
            anaerobic_te = summary_dto.get('anaerobicTrainingEffect', 0.0)
                
            czujniki = act_details.get("metadata", {}).get("sensors", [])
            for czujnik in czujniki:
                dane_czujnika = str(czujnik).upper()
                if "HEART" in dane_czujnika or "HRM" in dane_czujnika or "ANTPLUS" in dane_czujnika:
                    pasek_hr = "Tak"
                    break
        except Exception as e:
            logger.warning(f"Nie udało się pobrać czujników/szczegółów dla {act_id}: {e}")

        if load and float(load) > 0:
            if data_aktywnosci in dzienne_statystyki:
                dzienne_statystyki[data_aktywnosci]["garmin_load"] += float(load)
                dzienne_statystyki[data_aktywnosci]["znaleziono_load"] = True

        hr_zones_dict[act_id] = [z1, z2, z3, z4, z5, pasek_hr]
        
        # Zapisujemy dodatkowe metryki treningowe, żeby wstawić je do arkusza
        activity_extra_metrics[act_id] = {
            "aerobic_te": aerobic_te if aerobic_te else "-",
            "anaerobic_te": anaerobic_te if anaerobic_te else "-"
        }

    for data_badania in [wczoraj_str, dzis_str]:
        if dzienne_statystyki[data_badania]["znaleziono_load"]:
            dzienne_statystyki[data_badania]["garmin_load"] = str(round(dzienne_statystyki[data_badania]["garmin_load"]))
        else:
            dzienne_statystyki[data_badania]["garmin_load"] = "0"

    return activities, dzienne_statystyki, hr_zones_dict, activity_extra_metrics


def main():
    logger.info("Rozpoczęcie procesu synchronizacji (Kolejność: Chronologiczna, z góry na dół)...")

    dzis = datetime.now(WARSAW_TZ).date()
    dzis_str = dzis.strftime("%Y-%m-%d")
    wczoraj = dzis - timedelta(days=1)
    wczoraj_str = wczoraj.strftime("%Y-%m-%d")

    logger.info("Logowanie do Garmin Connect...")
    try:
        client = get_garmin_client()
    except GarminConnectAuthenticationError as e:
        logger.error(f"Błąd autoryzacji Garmina — sprawdź GARMIN_EMAIL/GARMIN_PASSWORD: {e}")
        raise
    except GarminConnectTooManyRequestsError as e:
        logger.error(f"Garmin zablokował logowanie (rate limit): {e}")
        raise
    except GarminConnectConnectionError as e:
        logger.error(f"Błąd połączenia z Garminem: {e}")
        raise

    activities, dzienne_statystyki, hr_zones_dict, activity_extra_metrics = fetch_garmin_data(client, wczoraj_str, dzis_str, limit=20)

    for check_date in [wczoraj_str, dzis_str]:
        acts_for_date = [act for act in activities if act.get('startTimeLocal', '').startswith(check_date)]

        if not acts_for_date:
            logger.info(f"Brak treningów dla {check_date}. Tworzenie pustego wpisu z dziennymi statystykami.")
            dummy_act = {
                'activityId': f"REST_{check_date}",
                'startTimeLocal': f"{check_date} 00:00:00",
                'activityName': 'Dzień bez treningu',
                'activityType': {'typeKey': '-'},
                'distance': 0,
                'duration': 0,
                'averageHR': '-',
                'maxHR': '-'
            }
            activities.append(dummy_act)

    aktywnosci_do_dodania = [
        act for act in activities
        if act.get('startTimeLocal', '').startswith(wczoraj_str) or act.get('startTimeLocal', '').startswith(dzis_str)
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

    for activity in aktywnosci_do_dodania:
        wszystkie_dane = sheet.get_all_values()
        existing_ids = [r[0] if len(r) > 0 else "" for r in wszystkie_dane]

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

        # NOWOŚĆ: Zaktualizowany układ kolumn (K=BodyBat, L=Stress)
        row = [
            activity_id, data_aktywnosci, plan_treningu1, plan_treningu2, 
            hrv, rhr, g_load, t_kcal, a_kcal, b_kcal, body_bat, stress, act_type,
            distance_km, duration_min, tempo_str, avg_hr, max_hr,
            pasek_hr, aero_te, anaero_te, z1, z2, z3, z4, z5, exact_date
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
            sheet.batch_update([{'range': f"A{docelowy_wiersz}:AA{docelowy_wiersz}", 'values': [row]}])
        else:
            # ... (reszta kodu bez zmian) ...
            insert_idx = len(wszystkie_dane) + 1

            for i in range(3, len(wszystkie_dane)):
                row_idx = i + 1
                r_data = wszystkie_dane[i]
                r_date = r_data[1] if len(r_data) > 1 else ""
                
                # POPRAWKA: Ukryta data jest teraz pod indeksem 22 (Kolumna W)
                r_exact = r_data[22] if len(r_data) > 22 else ""

                if not r_date:
                    continue

                row_time = r_exact if r_exact else f"{r_date} 00:00:00"

                if exact_date < row_time:
                    insert_idx = row_idx
                    break

            if insert_idx > len(wszystkie_dane):
                logger.info(f"Dopisywanie nowego wiersza {activity_id} na samym dole arkusza...")
                sheet.append_row(row)
            else:
                logger.info(f"Wstawianie wiersza {activity_id} chronologicznie w pozycji {insert_idx}...")
                sheet.insert_rows([row], row=insert_idx)

    logger.info("Aktualizacja scalonych komórek i krawędzi (z białym tłem siatki!)...")
    wszystkie_ids_po_wstawieniu = sheet.col_values(1)

    def scal_i_formatuj_dla_daty(data_str):
        ids_dla_daty = [
            str(act.get('activityId')) for act in activities
            if act.get('startTimeLocal', '').startswith(data_str)
        ]
        ids_dla_daty.append(f"REST_{data_str}")

        wiersze_w_arkuszu = []
        for i, id_arkusz in enumerate(wszystkie_ids_po_wstawieniu):
            if id_arkusz in ids_dla_daty:
                wiersze_w_arkuszu.append(i + 1)

        if not wiersze_w_arkuszu:
            return

        start_w = min(wiersze_w_arkuszu)
        end_w = max(wiersze_w_arkuszu)

        if len(wiersze_w_arkuszu) > 1:
            logger.info(f"Scalanie dla {data_str}: wiersze od {start_w} do {end_w}.")
            # Scalanie do kolumny L (Index 11 -> Litera L)
            cols_to_merge = ["B", "C", "D", "E", "F", "G", "H", "I", "J", "K", "L"]
            for col in cols_to_merge:
                sheet.merge_cells(f"{col}{start_w}:{col}{end_w}")

        try:
            requests = []
            czarny = {"red": 0.0, "green": 0.0, "blue": 0.0}

            zakres_pelny = {
                "sheetId": sheet.id,
                "startRowIndex": start_w - 1,
                "endRowIndex": end_w,
                "startColumnIndex": 0,
                "endColumnIndex": 27  # A do AA (27 kolumn)
            }

            requests.append({
                "repeatCell": {
                    "range": zakres_pelny,
                    "cell": {
                        "userEnteredFormat": {
                            "horizontalAlignment": "CENTER",
                            "verticalAlignment": "MIDDLE"
                        }
                    },
                    "fields": "userEnteredFormat(horizontalAlignment,verticalAlignment)"
                }
            })

            requests.append({
                "updateBorders": {
                    "range": zakres_pelny,
                    "bottom": {"style": "SOLID_MEDIUM", "color": czarny}
                }
            })

            def dodaj_pionowa(kolumna_indeks):
                requests.append({
                    "updateBorders": {
                        "range": {
                            "sheetId": sheet.id,
                            "startRowIndex": start_w - 1,
                            "endRowIndex": end_w,
                            "startColumnIndex": kolumna_indeks,
                            "endColumnIndex": kolumna_indeks + 1
                        },
                        "right": {"style": "SOLID_MEDIUM", "color": czarny}
                    }
                })

            # NOWOŚĆ: Uporządkowane i przesunięte linie pionowe!
            # NOWE GRANICE (pionowe linie):
            dodaj_pionowa(3)   # Między Plan(D) a HRV(E)
            dodaj_pionowa(11)  # Między Stress(L) a ActType(M)
            dodaj_pionowa(20)  # Między AnaerobicTE(U) a Z1(V)
            dodaj_pionowa(25)  # Między Z5(Z) a UkrytaData(AA)

            body = {"requests": requests}
            sheet.spreadsheet.batch_update(body)

        except Exception as e:
            logger.error(f"Nie udało się sformatować krawędzi / wyśrodkowania: {e}")

    try:
        scal_i_formatuj_dla_daty(dzis_str)
        scal_i_formatuj_dla_daty(wczoraj_str)
    except Exception as e:
        logger.error(f"Nie udało się połączyć/sformatować komórek: {e}")

    logger.info("Pomyślnie zsynchronizowano dane w arkuszu!")

if __name__ == "__main__":
    main()