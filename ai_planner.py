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

dotenv.load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
WARSAW_TZ = ZoneInfo("Europe/Warsaw")

def main():
    logger.info("Uruchamianie Trenera AI (wersja REST API - autoryzacja nagłówkowa X-goog-api-key)...")
    
    # 1. Klucz API
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        logger.error("Brak klucza GEMINI_API_KEY w pliku .env!")
        return

    # 2. Połączenie z arkuszem
    try:
        gc = gspread.service_account(filename='credentials.json')
        sheet = gc.open_by_key(os.getenv("SHEET_ID")).sheet1
        wszystkie_dane = sheet.get_all_values()
    except Exception as e:
        logger.error(f"Błąd łączenia z arkuszem: {e}")
        return
    
    # 3. Konwersja danych z arkusza do wirtualnego pliku CSV dla Gemini
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerows(wszystkie_dane)
    csv_string = output.getvalue()
    
    # 4. Wczytanie strategii
    try:
        with open("strategia.txt", "r", encoding="utf-8") as f:
            strategia = f.read()
    except FileNotFoundError:
        logger.error("Brak pliku strategia.txt!")
        return

    # 5. Wyznaczenie dat na kolejne 7 dni (od jutra)
    dzis = datetime.now(WARSAW_TZ).date()
    kolejne_7_dni = [(dzis + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(1, 8)]
    
    # 6. Przygotowanie polecenia (Promptu)
    prompt = f"""
Jesteś profesjonalnym trenerem. Poniżej znajduje się moja ogólna strategia:
{strategia}

A oto pełny zrzut mojego dotychczasowego dziennika treningowego w formacie CSV:
{csv_string}

Twoim zadaniem jest zaplanować mi treningi na kolejne 7 dni, od {kolejne_7_dni[0]} do {kolejne_7_dni[-1]}.
Przeanalizuj historię, by zobaczyć w jakiej jestem formie, i dopasuj plany do strategii.

Ten skrypt jest uruchamiany codziennie, więc przeanalizuj obecny plan na najbliższe 7 dni i w razie potrzeby go zmodyfikuj, aby był zgodny z moją strategią i adekwatny do zmęczenia.

WYMÓG KRYTYCZNY: Zwróć wynik WYŁĄCZNIE jako surowy JSON. 
Struktura JSON musi wyglądać DOKŁADNIE tak:
{{
    "{kolejne_7_dni[0]}": {{"typ": "Krótka nazwa (np. Interwały)", "opis": "Szczegółowy opis (np. 5x1km tempo 4:00)"}},
    ...
}}
"""

    logger.info("Wysyłanie danych bezpośrednio do API Google z nowym systemem autoryzacji...")
    
    # NOWOŚĆ 1: URL z modelem gemini-flash-latest, bez parametru ?key=
    url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-flash-latest:generateContent"
    
    payload = {
        "contents": [{
            "parts": [{"text": prompt}]
        }],
        "generationConfig": {
            "responseMimeType": "application/json"
        }
    }
    
    # NOWOŚĆ 2: Wstrzyknięcie klucza AQ... bezpośrednio w nagłówki
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
            logger.error(f"Szczegóły błędu od serwera Google: {response.text}")
        return
    except (KeyError, IndexError, json.JSONDecodeError) as e:
        logger.error(f"Błąd parsowania odpowiedzi JSON: {e}\nSurowy tekst z serwera: {tekst_ai if 'tekst_ai' in locals() else 'Brak'}")
        return

    # 7. Wpisywanie wygenerowanego planu do Arkusza (dynamiczne nadpisywanie!)
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
            
    logger.info("Twój dynamicznie zaktualizowany plan na 7 dni jest już w Arkuszu!")

if __name__ == "__main__":
    main()