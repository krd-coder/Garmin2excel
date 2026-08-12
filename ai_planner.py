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

def oblicz_fizjologie(wszystkie_dane, dzis):
    """
    Wylicza CTL, ATL, TSB, ACWR oraz Trend HRV na podstawie danych historycznych z arkusza.
    Zakładamy indeksy (liczone od 0, gdzie A=0, B=1, C=2...):
    - Data: kolumna B (indeks 1)
    - HRV: kolumna E (indeks 4)
    - Obciążenie (TSS / Garmin Load / sRPE): kolumna G (indeks 6) - DOSTOSUJ W RAZIE POTRZEBY
    """
    historia = {}
    
    # 1. Zbieranie i czyszczenie danych
    for row in wszystkie_dane[3:]:  # Pomijamy nagłówek
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

def main():
    logger.info("Uruchamianie Trenera AI (Z modułem wyliczania obciążeń matematycznych)...")
    
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        logger.error("Brak klucza GEMINI_API_KEY w pliku .env!")
        return

    try:
        gc = gspread.service_account(filename='credentials.json')
        sheet = gc.open_by_key(os.getenv("SHEET_ID")).sheet1
        wszystkie_dane = sheet.get_all_values()
    except Exception as e:
        logger.error(f"Błąd łączenia z arkuszem: {e}")
        return
    
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerows(wszystkie_dane)
    csv_string = output.getvalue()
    
    try:
        with open("strategia.txt", "r", encoding="utf-8") as f:
            strategia = f.read()
    except FileNotFoundError:
        logger.error("Brak pliku strategia.txt!")
        return

    dzis = datetime.now(WARSAW_TZ).date()
    kolejne_7_dni = [(dzis + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(1, 8)]
    
    # MAGIA DZIEJE SIĘ TUTAJ - wywołujemy kalkulator fizjologii
    tabela_wskaznikow = oblicz_fizjologie(wszystkie_dane, dzis)
    
    prompt = f"""
    Jesteś profesjonalnym trenerem. Poniżej znajduje się moja ogólna strategia z konkretnymi ramami czasowymi:
    {strategia}

    To są moje matematycznie wyliczone wskaźniki fizjologiczne na stan dzisiejszy (użyj ich jako głównego radaru zmęczenia):
    {tabela_wskaznikow}

    A oto pełny zrzut mojego dotychczasowego dziennika treningowego w formacie CSV do wglądu w szczegóły:
    {csv_string}

    Twoim zadaniem jest zaplanować mi treningi na kolejne 7 dni, od {kolejne_7_dni[0]} do {kolejne_7_dni[-1]}.
    Przeanalizuj wskaźniki CTL, ATL, TSB i ACWR, by zobaczyć w jakiej jestem formie, i dopasuj plany do ram czasowych w strategii. 
    Pamiętaj: zdrowie > strategia. Jeśli TSB jest niebezpiecznie niskie lub trend HRV mocno ujemny, koryguj plan awaryjnie.
    
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

if __name__ == "__main__":
    main()