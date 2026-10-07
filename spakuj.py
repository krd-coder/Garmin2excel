import shutil
import base64

# 1. Pakuje folder 'strategies' do pliku 'strategies.zip'
shutil.make_archive('strategies', 'zip', 'strategies')

# 2. Odczytuje ZIPa, koduje do Base64 i zapisuje jako plik tekstowy
with open('strategies.zip', 'rb') as f_in, open('strategies_b64.txt', 'w', encoding='utf-8') as f_out:
    encoded = base64.b64encode(f_in.read()).decode('utf-8')
    f_out.write(encoded)

print("Gotowe! Otwórz plik strategies_b64.txt i skopiuj jego zawartość do GitHub Secrets.")