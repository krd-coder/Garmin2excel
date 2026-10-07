# Garmin to Google Sheets AI Training Planner

Tool connecting your Garmin account to Google Sheets, allowing you to monitor and analyze your data precisely. It also proposes trainings for today and future 6 days based on given strategies for each mezocycle using the latest Gemini-Flash model via Google Cloud API.

## 1. Google Cloud Setup (`credentials.json`)
To allow the script to read and modify your spreadsheet, you need to create a Service Account in Google Cloud.

1. Go to the [Google Cloud Console](https://console.cloud.google.com/).
2. Create a new project (or select an existing one).
3. Go to **APIs & Services > Library**.
4. Search for **Google Sheets API** and click **Enable**.
5. Search for **Google Drive API** and click **Enable** (this is strictly required for the Python `gspread` library to locate the file).
6. Go to **APIs & Services > Credentials**.
7. Click **Create Credentials** -> **Service Account**.
8. Provide a name (e.g., `garmin-planner`), click **Create and Continue**, then **Done**.
9. In the Credentials list, click on the newly created Service Account email to open its details.
10. Go to the **Keys** tab -> **Add Key** -> **Create new key** -> Choose **JSON** -> **Create**.
11. The file will download automatically. Rename it to `credentials.json` and place it in the main directory of this project.
12. **CRITICAL STEP:** Open the downloaded `credentials.json` file, copy the `client_email` address, open your target Google Sheet in the browser, click **Share**, and grant this email **Editor** permissions.

## 2. Gemini API Setup
1. Go to [Google AI Studio](https://aistudio.google.com/app/apikey) (this is the direct portal for Gemini API keys).
2. Sign in with your Google account.
3. Click **Create API key**.
4. Select the Google Cloud project you created in Step 1.
5. Copy the generated API key.

## 3. Environmental Variables
In the main directory, create a file named `.env` and put the following variables:

```env
GARMIN_EMAIL=<your garmin account email>
GARMIN_PASSWORD=<your garmin account password>
SHEET_ID=<ID of the google sheet you want to use as your training plan - can be found at the end of the sheet link>
GEMINI_API_KEY=<API key to your Gemini API from Google Cloud>
```

## 4. Sheet Configuration
* Rows 1-3 will be empty - those are for your header. 
* Column A is for the activity ID, you can hide it as it's not necessary. 
* In Column B you need to insert dates starting from today and at least 7 days ahead for the tool to plan your trainings. Dates should be in the format `yyyy-mm-dd`.

## 5. Strategies
In the main directory create a folder called `strategies/`. There you can create strategies for any time period by naming files `dd.mm.yyyy-dd.mm.yyyy.txt` and inserting a prompt for that period there. You can also insert a main strategy that will be inputed before the detailed period strategy into the prompt to the file `strategies/strategy_main.txt`. 

It is advised to specify in the main strategy how the model should interprete the data - it is fed last 50 days from your sheet alongside a lot of your health statistics from garmin so you should specify which parameters it should base the training plan on.