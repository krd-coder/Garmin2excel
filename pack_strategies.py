import shutil
import base64

# 1. Packs the 'strategies' folder into the 'strategies.zip' file
shutil.make_archive('strategies', 'zip', 'strategies')

# 2. Reads the ZIP file, encodes it to Base64, and saves it as a text file
with open('strategies.zip', 'rb') as f_in, open('strategies_b64.txt', 'w', encoding='utf-8') as f_out:
    encoded = base64.b64encode(f_in.read()).decode('utf-8')
    f_out.write(encoded)

print("Done! Open the strategies_b64.txt file and copy its contents to GitHub Secrets.")