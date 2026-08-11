import os
import json
import logging
from datetime import datetime, timedelta
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
import gspread
import dotenv

dotenv.load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class GarminSessionExpiredError(RuntimeError):
    """Sesja z garmin_state.json wygasła lub jest nieprawidłowa."""


def fetch_garmin_data(wczoraj_str, dzis_str, limit=20):
    activities = []
    hr_zones_dict = {}
    
    # Słownik do przechowywania dziennych statystyk
    dzienne_statystyki = {
        wczoraj_str: {"hrv": "Brak", "rhr": "Brak", "total_kcal": "Brak", "active_kcal": "Brak", "bmr_kcal": "Brak"},
        dzis_str: {"hrv": "Brak", "rhr": "Brak", "total_kcal": "Brak", "active_kcal": "Brak", "bmr_kcal": "Brak"}
    }
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            context = browser.new_context(storage_state="garmin_state.json")
            page = context.new_page()
            
            logger.info("Ładowanie panelu aktywności...")

            # 1. Przechwytywanie treningów
            try:
                with page.expect_response(
                    lambda r: "search/activities" in r.url and r.status == 200,
                    timeout=30000
                ) as response_info:
                    page.goto("https://connect.garmin.com/modern/activities")
                activities = response_info.value.json()[:limit]
            except PlaywrightTimeoutError:
                if "signin" in page.url or "sso" in page.url:
                    raise GarminSessionExpiredError(
                        "Sesja Garmina wygasła (przekierowano na stronę logowania). "
                        "Uruchom login.py na własnym komputerze i zaktualizuj garmin_state.json."
                    )
                raise GarminSessionExpiredError(
                    "Garmin nie odpowiedział w ciągu 30s przy pobieraniu aktywności — "
                    "sesja w garmin_state.json może być nieprawidłowa."
                )
            
            recent_activities = [
                act for act in activities 
                if act.get('startTimeLocal', '').startswith(wczoraj_str) or act.get('startTimeLocal', '').startswith(dzis_str)
            ]
            
# --- ZŁOTY BILET 3.0: Sniffing Stref Tętna i Czujników ---
            for act in recent_activities:
                act_id = str(act['activityId'])
                logger.info(f"Otwieranie strony treningu {act_id}...")
                
                z1 = z2 = z3 = z4 = z5 = 0.0
                pasek_hr = "Nie" # Domyślnie zakładamy, że to czujnik w zegarku
                zones_found = False
                
                def zlap_dane_treningu(response):
                    nonlocal z1, z2, z3, z4, z5, zones_found, pasek_hr
                    if response.status == 200:
                        url = response.url
                        
                        # 1. Łapanie stref tętna (to co już było)
                        if "hrTimeInZones" in url:
                            try:
                                tekst = response.text()
                                if tekst.strip() and not tekst.startswith('<'):
                                    zones_data = response.json()
                                    if isinstance(zones_data, list):
                                        for zone in zones_data:
                                            zn = zone.get('zoneNumber')
                                            mins = round(zone.get('secsInZone', 0) / 60.0, 2)
                                            if zn == 1: z1 = mins
                                            elif zn == 2: z2 = mins
                                            elif zn == 3: z3 = mins
                                            elif zn == 4: z4 = mins
                                            elif zn == 5: z5 = mins
                                        zones_found = True
                            except: pass
                            
                        # 2. NOWOŚĆ: Łapanie metadanych treningu (szukamy paska HR)
                        # Główny plik detali treningu kończy się samym ID aktywności
                        elif url.endswith(f"/activity/{act_id}"):
                            try:
                                tekst = response.text()
                                if tekst.strip() and not tekst.startswith('<'):
                                    act_details = response.json()
                                    # Wyciągamy listę zewnętrznych czujników
                                    czujniki = act_details.get("metadata", {}).get("sensors", [])
                                    
                                    # Szukamy czegokolwiek, co przypomina zewnętrzny pulsometr
                                    for czujnik in czujniki:
                                        dane_czujnika = str(czujnik).upper()
                                        if "HEART" in dane_czujnika or "HRM" in dane_czujnika or "ANTPLUS" in dane_czujnika:
                                            pasek_hr = "Tak"
                                            break
                            except: pass
                
                # Używamy nowej nazwy funkcji
                page.on("response", zlap_dane_treningu)
                
                page.goto(f"https://connect.garmin.com/modern/activity/{act_id}")
                page.wait_for_timeout(3500)
                
                page.remove_listener("response", zlap_dane_treningu)
                
                # Zapisujemy do słownika nową wartość jako szósty element!
                hr_zones_dict[act_id] = [z1, z2, z3, z4, z5, pasek_hr]
            
            # --- ZŁOTY BILET 2.0: Inteligentny podsłuch dziennych statystyk (HRV, Kalorie, RHR) ---
            aktualnie_sprawdzana_data = None
            
            def zlap_odpowiedzi(response):
                nonlocal aktualnie_sprawdzana_data
                if response.status == 200:
                    url = response.url
                    if "hrv-service/hrv" in url:
                        try:
                            tekst = response.text()
                            if tekst.strip() and not tekst.startswith('<'):
                                data = response.json()
                                hrv = data.get('hrvSummary', {}).get('lastNightAvg', "Brak") if 'hrvSummary' in data else data.get('lastNightAvg', "Brak")
                                dzienne_statystyki[aktualnie_sprawdzana_data]["hrv"] = hrv
                        except: pass
                    elif "usersummary-service/usersummary/daily" in url:
                        try:
                            tekst = response.text()
                            if tekst.strip() and not tekst.startswith('<'):
                                data = response.json()
                                dzienne_statystyki[aktualnie_sprawdzana_data]["rhr"] = data.get('restingHeartRate', "Brak")
                                dzienne_statystyki[aktualnie_sprawdzana_data]["total_kcal"] = data.get('totalKilocalories', "Brak")
                                dzienne_statystyki[aktualnie_sprawdzana_data]["active_kcal"] = data.get('activeKilocalories', "Brak")
                                dzienne_statystyki[aktualnie_sprawdzana_data]["bmr_kcal"] = data.get('bmrKilocalories', "Brak")
                        except: pass

            page.on("response", zlap_odpowiedzi)
            
            for data_badania in [wczoraj_str, dzis_str]:
                aktualnie_sprawdzana_data = data_badania
                logger.info(f"Otwieranie podsumowania dnia ({data_badania}) i nasłuchiwanie...")
                page.goto(f"https://connect.garmin.com/modern/daily-summary/{data_badania}")
                page.wait_for_timeout(4000)
                
        except GarminSessionExpiredError:
            raise
        except Exception as e:
            logger.error(f"Wystąpił błąd przy przechwytywaniu danych: {e}")
        finally:
            browser.close()
            
    return activities, dzienne_statystyki, hr_zones_dict


def main():
    logger.info("Rozpoczęcie procesu synchronizacji (Kolejność: Chronologiczna, z góry na dół)...")
    
    # 1. Obliczamy daty
    dzis = datetime.now().date()
    dzis_str = dzis.strftime("%Y-%m-%d")
    wczoraj = dzis - timedelta(days=1)
    wczoraj_str = wczoraj.strftime("%Y-%m-%d")
    
    activities, dzienne_statystyki, hr_zones_dict = fetch_garmin_data(wczoraj_str, dzis_str, limit=20)
    
    # --- Tworzenie pustych wierszy dla dni bez treningu ---
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
    # ----------------------------------------------------------------

    aktywnosci_do_dodania = [
        act for act in activities 
        if act.get('startTimeLocal', '').startswith(wczoraj_str) or act.get('startTimeLocal', '').startswith(dzis_str)
    ]

    if not aktywnosci_do_dodania:
        logger.info("Brak jakichkolwiek danych do dodania.")
        return

    aktywnosci_do_dodania.sort(key=lambda act: act.get('startTimeLocal', ''))
    
    logger.info(f"Znaleziono {len(aktywnosci_do_dodania)} wpisów z wczoraj i dzisiaj.")

    try:
        logger.info("Łączenie z Google Sheets...")
        gc = gspread.service_account(filename='credentials.json')
        
        sheet_id = os.environ.get("SHEET_ID")
        sheet = gc.open_by_key(sheet_id).sheet1
        
        wszystkie_dane = sheet.get_all_values()
        plany_dzienne = {}
        for r_data in wszystkie_dane:
            if len(r_data) >= 3:
                r_date = r_data[1]
                r_plan = r_data[2]
                if r_date and r_plan.strip() and r_date not in plany_dzienne:
                    plany_dzienne[r_date] = r_plan
    except Exception as e:
        logger.error(f"Błąd łączenia z arkuszem: {e}")
        return

    for activity in aktywnosci_do_dodania:
        wszystkie_dane = sheet.get_all_values()
        existing_ids = [r[0] if len(r) > 0 else "" for r in wszystkie_dane]
        
        activity_id = str(activity.get('activityId'))
        if activity_id in existing_ids and not activity_id.startswith("REST_"):
            logger.info(f"Wpis {activity_id} już istnieje w arkuszu. Pomijam.")
            continue
            
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
        
        hrv = staty_dnia.get("hrv", "Brak")
        rhr = staty_dnia.get("rhr", "Brak")
        t_kcal = staty_dnia.get("total_kcal", "Brak")
        a_kcal = staty_dnia.get("active_kcal", "Brak")
        b_kcal = staty_dnia.get("bmr_kcal", "Brak")
        
        if activity_id.startswith("REST_"):
            pasek_hr = "-"
            z1 = z2 = z3 = z4 = z5 = ""
            tempo_str = "-"
            
        plan_treningu = plany_dzienne.get(data_aktywnosci, "")
        
        row = [
            activity_id, data_aktywnosci, plan_treningu, hrv, rhr, t_kcal, a_kcal, b_kcal, act_type, 
            distance_km, duration_min, tempo_str, avg_hr, max_hr, 
            pasek_hr, z1, z2, z3, z4, z5, exact_date
        ]
        
        docelowy_wiersz = None
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
            logger.info(f"Nadpisywanie wiersza {docelowy_wiersz} aktywnością {activity_id}.")
            sheet.batch_update([{'range': f"A{docelowy_wiersz}:U{docelowy_wiersz}", 'values': [row]}])
        else:
            insert_idx = len(wszystkie_dane) + 1 
            
            for i in range(3, len(wszystkie_dane)): 
                row_idx = i + 1
                r_data = wszystkie_dane[i]
                r_date = r_data[1] if len(r_data) > 1 else ""
                r_exact = r_data[20] if len(r_data) > 20 else "" 
                
                if not r_date:
                    continue
                
                row_time = r_exact if r_exact else f"{r_date} 00:00:00"
                
                if exact_date < row_time:
                    insert_idx = row_idx
                    break
                    
            # NOWOŚĆ: Wykrywamy, czy wstawiamy na sam koniec arkusza (poza jego aktualne ramy)
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
            sheet.merge_cells(f"B{start_w}:B{end_w}")  
            sheet.merge_cells(f"C{start_w}:C{end_w}")  
            sheet.merge_cells(f"D{start_w}:D{end_w}")  
            sheet.merge_cells(f"E{start_w}:E{end_w}")  
            sheet.merge_cells(f"F{start_w}:F{end_w}")  
            sheet.merge_cells(f"G{start_w}:G{end_w}")  
            sheet.merge_cells(f"H{start_w}:H{end_w}")  

        try:
            requests = []
            bialy = {"red": 1.0, "green": 1.0, "blue": 1.0}
            czarny = {"red": 0.0, "green": 0.0, "blue": 0.0}
            
            # Definiujemy zakres obejmujący cały dany dzień
            zakres_pelny = {
                "sheetId": sheet.id,
                "startRowIndex": start_w - 1, 
                "endRowIndex": end_w,         
                "startColumnIndex": 0,        
                "endColumnIndex": 21 # Od A do U
            }

            # =========================================================
            # NOWOŚĆ: 0. Wyśrodkowanie w pionie i poziomie całego bloku
            # =========================================================
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
            
            # 2. Gruba pozioma czarna kreska na samym dole bloku
            requests.append({
                "updateBorders": {
                    "range": zakres_pelny,
                    "bottom": {"style": "SOLID_MEDIUM", "color": czarny}
                }
            })
            
            # 3. Grube pionowe linie dzielące logiczne sekcje
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

            dodaj_pionowa(2)   # Oddziela Plan od Dziennych Statystyk (między C i D)
            dodaj_pionowa(7)   # Oddziela Dzienne Statystyki od Treningów (między H i I)
            dodaj_pionowa(14)  # Oddziela Ogólne Dane Treningu od Stref Tętna (między O i P)
            dodaj_pionowa(19)  # Oddziela Strefy Tętna od Ukrytej Daty (między T i U)

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