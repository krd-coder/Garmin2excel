# login.py
from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

def login_and_save_state():
    # Inicjalizacja z użyciem obiektu Stealth w trybie synchronicznym
    with Stealth().use_sync(sync_playwright()) as p:
        # Uruchamiamy widoczną przeglądarkę
        browser = p.chromium.launch(headless=False)
        
        # Tworzymy kontekst z odpowiednimi wymiarami i językiem
        context = browser.new_context(
            viewport={"width": 1920, "height": 1080},
            locale="pl-PL"
        )
        
        page = context.new_page()

        print("Otwieranie strony logowania Garmina...")
        page.goto("https://connect.garmin.com/signin/")
        
        print("Zaloguj się ręcznie w oknie przeglądarki.")
        print("Skrypt czeka na załadowanie panelu głównego (masz na to 2 minuty)...")
        
        # Czekamy na pomyślne logowanie
        page.wait_for_url("https://connect.garmin.com/app/**", timeout=120000)
        
        print("Zalogowano pomyślnie! Zapisywanie sesji...")
        context.storage_state(path="garmin_state.json")
        
        print("Sesja zapisana w pliku 'garmin_state.json'.")
        browser.close()

if __name__ == "__main__":
    login_and_save_state()