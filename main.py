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

days_back_to_fetch = 5  # Number of days back to fetch data from Garmin

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN_DIR = "./.garmin_tokens"
WARSAW_TZ = ZoneInfo("Europe/Warsaw")

def _activity_covers_date(act, check_date):
    """Check if an activity (which may span multiple days) covers the given date."""
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

def fetch_garmin_data(client, date_list):
    activities = client.get_activities_by_date(min(date_list), max(date_list), sortorder="asc")
    activities.sort(key=lambda act: act.get('startTimeLocal', ''))

    daily_stats = {}
    for target_date in date_list:
        hrv_summary = "None"
        rhr = "None"
        total_kcal = "None"
        active_kcal = "None"
        bmr_kcal = "None"
        avg_stress = "None"
        body_battery_min = "None"

        try:
            hrv = client.get_hrv_data(target_date)
            if hrv:
                hrv_summary = (
                    hrv.get('hrvSummary', {}).get('lastNightAvg', "None")
                    if 'hrvSummary' in hrv
                    else hrv.get('lastNightAvg', "None")
                )
        except Exception as e:
            logger.warning(f"Failed to fetch HRV for {target_date}: {e}")

        try:
            summary = client.get_user_summary(target_date)
            rhr = summary.get('restingHeartRate', "None")
            total_kcal = summary.get('totalKilocalories', "None")
            active_kcal = summary.get('activeKilocalories', "None")
            bmr_kcal = summary.get('bmrKilocalories', "None")
            avg_stress = summary.get('averageStressLevel', "None")
            body_battery_min = summary.get('bodyBatteryLowestValue', "None")
        except Exception as e:
            logger.warning(f"Failed to fetch daily summary for {target_date}: {e}")

        daily_stats[target_date] = {
            "hrv": hrv_summary,
            "rhr": rhr,
            "garmin_load": 0.0,
            "found_load": False,
            "total_kcal": total_kcal,
            "active_kcal": active_kcal,
            "bmr_kcal": bmr_kcal,
            "avg_stress": avg_stress,
            "body_battery_min": body_battery_min
        }

    # Fetch activities from the entire date range
    recent_activities = [
        act for act in activities
        if any(act.get('startTimeLocal', '').startswith(d) for d in date_list)
    ]

    hr_zones_dict = {}
    activity_extra_metrics = {}

    for act in recent_activities:
        act_id = str(act['activityId'])
        exact_date = act.get('startTimeLocal', '')
        activity_date = exact_date[:10]

        load = act.get('activityTrainingLoad') or act.get('trainingLoad') or 0.0
        aerobic_te = 0.0
        anaerobic_te = 0.0
        z1 = z2 = z3 = z4 = z5 = 0.0
        hr_strap = "No"

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

            sensors = act_details.get("metadata", {}).get("sensors", [])
            for sensor in sensors:
                if "HEART" in str(sensor).upper() or "HRM" in str(sensor).upper():
                    hr_strap = "Yes"; break
        except: pass

        if load and float(load) > 0:
            if activity_date in daily_stats:
                daily_stats[activity_date]["garmin_load"] += float(load)
                daily_stats[activity_date]["found_load"] = True

        hr_zones_dict[act_id] = [z1, z2, z3, z4, z5, hr_strap]
        activity_extra_metrics[act_id] = {"aerobic_te": aerobic_te or "0.0", "anaerobic_te": anaerobic_te or "0.0"}

    for d in date_list:
        if daily_stats[d]["found_load"]:
            daily_stats[d]["garmin_load"] = str(round(daily_stats[d]["garmin_load"]))
        else:
            daily_stats[d]["garmin_load"] = "0"

    return activities, daily_stats, hr_zones_dict, activity_extra_metrics


def calculate_physiology(all_data, today):
    """
    Calculates CTL, ATL, TSB, ACWR, and HRV Trend based on historical spreadsheet data.
    Assumed indices (0-indexed, where A=0, B=1, C=2...):
    - Date: column B (index 1)
    - HRV: column E (index 4)
    - Load (TSS / Garmin Load / sRPE): column G (index 6) - ADJUST IF NECESSARY
    """
    history = {}

    # 1. Data collection and cleaning (header is filtered out by date format validation below)
    # Cells here might be regular strings (fresh read from sheet) or raw
    # int/float (locally mutated list from sync phase) — cast to str for safety.
    for row in all_data:
        if len(row) > 1 and str(row[1]).strip():
            date_str = str(row[1]).strip()
            try:
                # Date format validation
                datetime.strptime(date_str, "%Y-%m-%d")

                # Parse load (column G)
                load_val = 0.0
                if len(row) > 6 and str(row[6]).strip().replace('.', '', 1).isdigit():
                    load_val = float(str(row[6]).strip())

                # Parse HRV (column E)
                hrv_val = None
                if len(row) > 4 and str(row[4]).strip().replace('.', '', 1).isdigit():
                    hrv_val = float(str(row[4]).strip())

                history[date_str] = {"load": load_val, "hrv": hrv_val}
            except ValueError:
                continue

    # 2. Sort dates chronologically, taking everything up to yesterday into account
    yesterday_str = (today - timedelta(days=1)).strftime("%Y-%m-%d")
    sorted_dates = sorted([d for d in history.keys() if d <= yesterday_str])

    ctl = 0.0  # Chronic Training Load
    atl = 0.0  # Acute Training Load
    hrv_list = []

    # 3. Mathematical modeling day by day
    for date in sorted_dates:
        load = history[date]["load"]

        # Exponential Weighted Moving Average (EWMA) formulas used in cycling and running
        ctl = ctl + (load - ctl) / min(42.0, len(sorted_dates))
        atl = atl + (load - atl) / min(7.0, len(sorted_dates))  # Cap at 7 days for ATL

        hrv = history[date]["hrv"]
        if hrv:
            hrv_list.append(hrv)

    # 4. Final metrics calculation
    tsb = ctl - atl  # Training Stress Balance (Form)
    acwr = atl / ctl if ctl > 0 else 0.0  # Acute-to-Chronic Workload Ratio

    # HRV Trend
    hrv_trend_str = "No data"
    if len(hrv_list) >= 2:
        # Take the average of a maximum of the last 30 entries
        last_30 = hrv_list[-30:]
        avg_30d = sum(last_30) / len(last_30)
        latest_hrv = hrv_list[-1]

        delta_hrv = ((latest_hrv - avg_30d) / avg_30d) * 100
        hrv_trend_str = f"{delta_hrv:+.1f}%"

    # 5. Build the result table for the LLM
    table = "METRIC,VALUE\n"
    table += f"CTL (Base/Fitness),{ctl:.1f}\n"
    table += f"ATL (Fatigue),{atl:.1f}\n"
    table += f"TSB (Freshness/Form),{tsb:.1f}\n"
    table += f"ACWR (Overload indicator),{acwr:.2f}\n"
    table += f"HRV Trend (vs 30d),{hrv_trend_str}\n"

    logger.info(f"Metrics calculated: CTL={ctl:.1f}, ATL={atl:.1f}, TSB={tsb:.1f}")
    return table


def fetch_physiological_profile(client, today):
    """Fetches baseline physiological parameters, HR zones, and LTHR of the user."""
    logger.info("Fetching physiological profile (HRmax, LTHR, baseline HRV, Zones Z1-Z5)...")
    today_str = today.strftime("%Y-%m-%d")
    profile = {
        "HRmax": "No data",
        "LTHR": "No data",
        "HRV_baseline": "No data",
        "RHR_baseline": "No data",
        "Z1": "None", "Z2": "None", "Z3": "None", "Z4": "None", "Z5": "None"
    }

    try:
        summary = client.get_user_summary(today_str)
        if summary and 'lastSevenDaysAvgRestingHeartRate' in summary:
            profile["RHR_baseline"] = f"{summary['lastSevenDaysAvgRestingHeartRate']} bpm"
    except Exception as e:
        logger.warning(f"Failed to fetch baseline RHR: {e}")

    try:
        hrv = client.get_hrv_data(today_str)
        if hrv and 'hrvSummary' in hrv:
            baseline = hrv['hrvSummary'].get('baseline', {})
            low = baseline.get('balancedLow')
            upper = baseline.get('balancedUpper')
            if low and upper:
                profile["HRV_baseline"] = f"{low} - {upper} ms"
    except Exception as e:
        logger.warning(f"Failed to fetch baseline HRV: {e}")

    # Fetch HR zones, LTHR, and HRmax from internal Garmin API
    try:
        zones_data = client.garth.client.get("connectapi", "/userprofile-service/userprofile/personal-information/hrZones").json()
        
        running_zones = None
        for zone_profile in zones_data:
            if zone_profile.get("sport") == "RUNNING":
                running_zones = zone_profile
                break
        
        if not running_zones and zones_data:
            running_zones = zones_data[0]

        if running_zones:
            if "maxHeartRate" in running_zones:
                profile["HRmax"] = f"{running_zones['maxHeartRate']} bpm"
            if "lactateThresholdHeartRate" in running_zones:
                profile["LTHR"] = f"{running_zones['lactateThresholdHeartRate']} bpm"
            
            hr_zones = running_zones.get("hrZones", [])
            for z in hr_zones:
                number = z.get("zoneNumber")
                min_hr = z.get("minHeartRate")
                max_hr = z.get("maxHeartRate")
                if number and min_hr and max_hr:
                    profile[f"Z{number}"] = f"{min_hr}-{max_hr} bpm"

    except Exception as e:
        logger.warning(f"Failed to fetch heart rate zones (LTHR/HRmax): {e}")

    result = (
        f"HRmax: {profile['HRmax']}\n"
        f"LTHR (Lactate Threshold): {profile['LTHR']}\n"
        f"HRV (baseline value): {profile['HRV_baseline']}\n"
        f"RHR (7-day baseline Resting Heart Rate): {profile['RHR_baseline']}\n"
        f"Zone 1 (Z1 - Recovery): {profile['Z1']}\n"
        f"Zone 2 (Z2 - Aerobic Base): {profile['Z2']}\n"
        f"Zone 3 (Z3 - Aerobic): {profile['Z3']}\n"
        f"Zone 4 (Z4 - Threshold): {profile['Z4']}\n"
        f"Zone 5 (Z5 - Maximum/VO2max): {profile['Z5']}\n"
    )
    return result

def plan_upcoming_days(sheet, all_data, today, client):
    """Plans the next 7 training days (AI Coach) based on physiological metrics."""
    logger.info("Initializing AI Coach (planning upcoming days)...")

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        logger.error("Missing GEMINI_API_KEY in the .env file!")
        return

    # Call the new function
    physiological_profile = fetch_physiological_profile(client, today)
    logger.info(f"Fetched physiological profile:\n{physiological_profile}")

    # For analysis (physiology table, CSV for LLM), we take only the last 42 rows
    history_to_analyze = all_data[-42:]

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerows(history_to_analyze)
    csv_string = output.getvalue()

    # --- NEW LOGIC: Dynamically loading strategies from the ./strategies folder ---
    strategies_dir = "./strategies"
    strategy_period = None
    strategy_main = open(os.path.join(strategies_dir, "strategy_main.txt"), "r", encoding="utf-8").read()
    
    if not os.path.exists(strategies_dir):
        logger.error(f"Missing directory {strategies_dir}! Create it and add strategy files.")
        return

    # Search files for the matching date range
    for filename in os.listdir(strategies_dir):
        if not filename.endswith(".txt"):
            continue
            
        name_without_ext = filename[:-4]  # Removes ".txt"
        parts = name_without_ext.split('-')
        
        if len(parts) == 2:
            try:
                # Parse dates from the filename (format DD.MM.YYYY)
                start_date = datetime.strptime(parts[0], "%d.%m.%Y").date()
                end_date = datetime.strptime(parts[1], "%d.%m.%Y").date()
                
                # Check if "today" falls within the file's date range
                if start_date <= today <= end_date:
                    filepath = os.path.join(strategies_dir, filename)
                    with open(filepath, "r", encoding="utf-8") as f:
                        strategy_period = f.read()
                    logger.info(f"Loaded strategy from file: {filename}")
                    break  # Found matching file, break the loop
            except ValueError:
                logger.warning(f"Ignored file with invalid date format: {filename}. Expected format: DD.MM.YYYY-DD.MM.YYYY.txt")
                continue

    if not strategy_period:
        logger.error(f"No strategy file found for today's date ({today.strftime('%d.%m.%Y')}) in the {strategies_dir} folder!")
        return
    # ----------------------------------------------------------------------------

    next_7_days = [(today + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(0, 7)]

    # Call the physiology calculator
    physiology_table = calculate_physiology(history_to_analyze, today)

    input_language=os.getenv("INPUT_LANGUAGE", "English")
    output_language=os.getenv("OUTPUT_LANGUAGE", "English")

    prompt = f"""
    You are a professional coach. 
    
    Below are my absolute, baseline physiological parameters:
    [YOUR ATHLETE - PHYSIOLOGICAL PROFILE]
    {physiological_profile}

    
    Below is my general strategy:
    [YOUR ATHLETE - OVERALL TRAINING STRATEGY WRITTEN IN LANGUAGE: {input_language}]
    {strategy_main}

    Below is my detailed strategy for the current period:
    [YOUR ATHLETE - PERIOD TRAINING STRATEGY WRITTEN IN LANGUAGE: {input_language}]
    {strategy_period}

    These are my mathematically calculated physiological metrics as of today (long-term trends):
    {physiology_table}

    And here is the full dump of my training log from the last 30 days in CSV format:
    [YOUR ATHLETE - TRAINING LOG CSV - WRITTEN IN LANGUAGE: {output_language}]
    {csv_string}

    Your task is to plan my trainings for the next 7 days, from {next_7_days[0]} to {next_7_days[-1]}.

    [CRITICAL DATA ANALYSIS HIERARCHY]
    Before generating the plan, you must analyze the data in the following order:
    1. HEALTH AND RECOVERY (ABSOLUTE PRIORITY): Analyze ONLY the last 3 to 5 days in the CSV file. Look for anomalies in the HRV (sudden drop), RHR (sudden spike), and 'Body Battery' or 'Stress' columns. 
    2. If you notice signs of infection, severe nervous system stress, or lack of recovery from the last 48-72h, IGNORE the long-term TSB/CTL metrics from the table. Enter emergency mode (REST or extremely light Z1), regardless of what the calendar and strategy dictate.
    3. If daily parameters from the CSV are stable, proceed to the physiological metrics table (CTL, ATL, TSB, ACWR) to assess overall fitness and plan loads according to the strategy.

    CRITICAL REQUIREMENT: Return the result EXCLUSIVELY as a raw JSON written in the {output_language} language.
    The JSON structure must look EXACTLY like this:
    {{
        "{next_7_days[0]}": {{"type": "Short name (e.g. Intervals)", "description": "Detailed description (e.g. 5x1km pace 4:00)"}},
        ...
    }}
    This script runs daily, so analyze the current plan for the upcoming 7 days and modify it if necessary to keep it aligned with my strategy and appropriate for my fatigue level.

    If you modify the plan for a given training - add a comment regarding the reason for the change (e.g. "Pace changed from 4:30 to 4:15 due to improved form").
    If there is no need to change the plan for a given day, leave it COMPLETELY unchanged.

    CRITICAL REQUIREMENT: Return the result EXCLUSIVELY as a raw JSON written in the {output_language} language.
    The JSON structure must look EXACTLY like this:
    {{
        "{next_7_days[0]}": {{"type": "Short name (e.g. Intervals)", "description": "Detailed description (e.g. 5x1km pace 4:00)"}},
        ...
    }}
    """

    logger.info("Sending integrated data (with metrics table) to Google API...")

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

        response_data = response.json()
        ai_text = response_data['candidates'][0]['content']['parts'][0]['text']

        raw_json = ai_text.replace('```json', '').replace('```', '').strip()
        generated_plan = json.loads(raw_json)
        logger.info("Plan from AI received and decoded successfully!")

    except requests.exceptions.RequestException as e:
        logger.error(f"HTTP communication error: {e}")
        if response is not None:
            logger.error(f"Error details: {response.text}")
        return
    except (KeyError, IndexError, json.JSONDecodeError) as e:
        logger.error(f"Parsing error: {e}\nRaw text: {ai_text if 'ai_text' in locals() else 'None'}")
        return

    for plan_date in next_7_days:
        if plan_date not in generated_plan:
            continue

        plan_type = generated_plan[plan_date].get("type", "REST")
        description = generated_plan[plan_date].get("description", "-")

        target_row = None
        empty_row = None

        for i, r_data in enumerate(all_data):
            row_idx = i + 1
            if row_idx <= 1:
                continue

            r_id = r_data[0] if len(r_data) > 0 else ""
            r_date = r_data[1] if len(r_data) > 1 else ""

            if r_date == plan_date:
                target_row = row_idx
                break

            if r_date == "" and r_id == "" and empty_row is None:
                empty_row = row_idx

        if target_row:
            logger.info(f"Updating (overwriting) plan for {plan_date} (row {target_row})...")
            sheet.batch_update([{'range': f"C{target_row}:D{target_row}", 'values': [[plan_type, description]]}])

        elif empty_row:
            logger.info(f"Filling empty row (no. {empty_row}) with plan for {plan_date}...")
            sheet.batch_update([
                {'range': f"B{empty_row}:D{empty_row}", 'values': [[plan_date, plan_type, description]]},
                {'range': f"V{empty_row}", 'values': [[f"{plan_date} 00:00:00"]]}
            ])
            while len(all_data) < empty_row:
                all_data.append([""] * 22)
            all_data[empty_row - 1][1] = plan_date

        else:
            logger.info(f"No empty rows found. Appending new row for the plan on: {plan_date}...")
            new_row = ["", plan_date, plan_type, description] + ([""] * 17) + [f"{plan_date} 00:00:00"]
            sheet.append_row(new_row)
            all_data.append(new_row)

    logger.info("Plan updated with mathematical models is now in the Sheet!")


def main():
    logger.info(f"Synchronization: {days_back_to_fetch} days back.")
    today = datetime.now(WARSAW_TZ).date()
    dates_to_fetch = [(today - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days_back_to_fetch)] # [today, yesterday, day before...]

    try:
        client = get_garmin_client()
        activities, daily_stats, hr_zones_dict, activity_extra_metrics = fetch_garmin_data(client, dates_to_fetch)
    except GarminConnectAuthenticationError as e:
        logger.error(f"Garmin authentication error — check GARMIN_EMAIL/GARMIN_PASSWORD: {e}")
        raise
    except GarminConnectTooManyRequestsError as e:
        logger.error(f"Garmin blocked login (rate limit): {e}")
        raise
    except GarminConnectConnectionError as e:
        logger.error(f"Connection error with Garmin: {e}")
        raise

    # Create missing REST entries
    for check_date in dates_to_fetch:
        acts_for_date = [act for act in activities if act.get('startTimeLocal', '').startswith(check_date)]

        if not acts_for_date:
            is_covered = any(_activity_covers_date(act, check_date) for act in activities)
            if is_covered:
                logger.info(f"Date {check_date} covered by a multi-day training — continuation entry.")
            else:
                logger.info(f"No trainings for {check_date}. Creating empty entry with daily stats.")
            dummy_act = {
                'activityId': f"REST_{check_date}",
                'startTimeLocal': f"{check_date} 00:00:00",
                'activityName': 'Multi-day training continuation' if is_covered else 'Rest day',
                'activityType': {'typeKey': 'in progress' if is_covered else '-'},
                'distance': 0,
                'duration': 0,
                'averageHR': '-',
                'maxHR': '-'
            }
            activities.append(dummy_act)

    activities_to_add = [
        act for act in activities
        if any(act.get('startTimeLocal', '').startswith(d) for d in dates_to_fetch)
    ]

    activities_to_add.sort(key=lambda act: act.get('startTimeLocal', ''))

    logger.info(f"Processing {len(activities_to_add)} entries from the fetched period...")

    try:
        logger.info("Connecting to Google Sheets...")
        gc = gspread.service_account(filename='credentials.json')

        sheet_id = os.environ.get("SHEET_ID")
        sheet = gc.open_by_key(sheet_id).sheet1

        all_data = sheet.get_all_values()
        daily_plans1 = {}
        daily_plans2 = {}
        for r_data in all_data:
            if len(r_data) >= 3:
                r_date = r_data[1]
                r_plan1 = r_data[2]
                r_plan2 = r_data[3] if len(r_data) >= 4 else ""
                if r_date and r_plan1.strip() and r_date not in daily_plans1:
                    daily_plans1[r_date] = r_plan1
                if r_date and r_plan2.strip() and r_date not in daily_plans2:
                    daily_plans2[r_date] = r_plan2
    except Exception as e:
        logger.error(f"Error connecting to sheet: {e}")
        return

    # We maintain a local copy of sheet data and update it after each write,
    # instead of reading the entire sheet again for each activity (Google Sheets API
    # has a read limit per minute, and 40+ reads within a second exceeds it — error 429).
    existing_ids = [r[0] if len(r) > 0 else "" for r in all_data]

    for activity in activities_to_add:
        activity_id = str(activity.get('activityId'))

        is_existing_workout = False
        if activity_id in existing_ids and not activity_id.startswith("REST_"):
            is_existing_workout = True

        exact_date = activity.get('startTimeLocal', '')
        activity_date = exact_date[:10]
        stats_of_day = daily_stats.get(activity_date, {})

        hr_data = hr_zones_dict.get(activity_id, [0.0, 0.0, 0.0, 0.0, 0.0, "No"])
        z1, z2, z3, z4, z5, hr_strap = hr_data

        name = activity.get('activityName', 'No name')
        act_type = activity.get('activityType', {}).get('typeKey', 'unknown')

        dist = activity.get('distance', 0)
        dur = activity.get('duration', 0)
        distance_km = round(dist / 1000, 2) if dist else ""
        duration_min = round(dur / 60, 2) if dur else ""

        pace_str = "-"
        if dist and dur and dist > 0:
            seconds_per_km = dur / (dist / 1000)
            minutes = int(seconds_per_km // 60)
            seconds = int(seconds_per_km % 60)
            pace_str = f"{minutes}:{seconds:02d}"

        avg_hr = activity.get('averageHR', '')
        max_hr = activity.get('maxHR', '')

        aero_te = activity_extra_metrics.get(activity_id, {}).get("aerobic_te", "0.0")
        anaero_te = activity_extra_metrics.get(activity_id, {}).get("anaerobic_te", "0.0")

        hrv = stats_of_day.get("hrv", "None")
        rhr = stats_of_day.get("rhr", "None")
        g_load = stats_of_day.get("garmin_load", "0")
        t_kcal = stats_of_day.get("total_kcal", "None")
        a_kcal = stats_of_day.get("active_kcal", "None")
        b_kcal = stats_of_day.get("bmr_kcal", "None")
        
        # Extracting new data
        body_bat = stats_of_day.get("body_battery_min", "None")
        stress = stats_of_day.get("avg_stress", "None")

        if activity_id.startswith("REST_"):
            hr_strap = "-"
            z1 = z2 = z3 = z4 = z5 = ""
            pace_str = "-"

        training_plan1 = daily_plans1.get(activity_date, "")
        training_plan2 = daily_plans2.get(activity_date, "")

        trimp = z1 * 1 + z2 * 2 + z3 * 3 + z4 * 4 + z5 * 5

        # Updated column layout (K=BodyBat, L=Stress)
        row = [
            activity_id, activity_date, training_plan1, training_plan2,
            hrv, rhr, g_load, t_kcal, a_kcal, b_kcal, body_bat, stress, act_type,
            distance_km, duration_min, pace_str, avg_hr, max_hr,
            hr_strap, aero_te, anaero_te, z1, z2, z3, z4, z5, exact_date, trimp
        ]

        target_row = None

        if is_existing_workout:
            target_row = existing_ids.index(activity_id) + 1
        else:
            for i, r_data in enumerate(all_data):
                row_idx = i + 1
                if row_idx <= 1:
                    continue
                r_id = r_data[0] if len(r_data) > 0 else ""
                r_date = r_data[1] if len(r_data) > 1 else ""

                if r_date == activity_date and (r_id == "" or r_id.startswith("REST_")):
                    target_row = row_idx
                    break

        if target_row:
            if is_existing_workout:
                logger.info(f"Refreshing stats for existing training {activity_id} (row {target_row}).")
            else:
                logger.info(f"Overwriting row {target_row} with activity {activity_id}.")
            
            # Range extended to column AB
            sheet.batch_update([{'range': f"A{target_row}:AB{target_row}", 'values': [row]}])
            all_data[target_row - 1] = row
            existing_ids[target_row - 1] = activity_id
        else:
            # Look for position as "just after the last row with date <= new date" (instead of
            # "just before the first row with date >"), so a single out-of-order
            # row earlier in the sheet doesn't interrupt the scan and push the entry up.
            insert_idx = 2

            for i in range(1, len(all_data)):
                row_idx = i + 1
                r_data = all_data[i]
                r_date = r_data[1] if len(r_data) > 1 else ""

                # Hidden exact date is at index 26 (Column AA)
                r_exact = r_data[26] if len(r_data) > 26 else ""

                if not r_date:
                    continue

                row_time = r_exact if r_exact else f"{r_date} 00:00:00"

                if row_time <= exact_date:
                    insert_idx = row_idx + 1

            if insert_idx > len(all_data):
                logger.info(f"Appending new row {activity_id} at the very bottom of the sheet...")
                sheet.append_row(row)
                all_data.append(row)
                existing_ids.append(activity_id)
            else:
                logger.info(f"Inserting row {activity_id} chronologically at position {insert_idx}...")
                sheet.insert_rows([row], row=insert_idx)
                all_data.insert(insert_idx - 1, row)
                existing_ids.insert(insert_idx - 1, activity_id)

    logger.info("Updating merged cells and borders (with white grid background!)...")
    all_ids_after_insert = sheet.col_values(1)

    def merge_and_format_for_date(date_list, sheet, activities, all_ids_after_insert):
        all_requests = []
        black = {"red": 0.0, "green": 0.0, "blue": 0.0}

        for date_str in date_list:
            ids_for_date = [str(act.get('activityId')) for act in activities if act.get('startTimeLocal', '').startswith(date_str)]
            ids_for_date.append(f"REST_{date_str}")

            rows_in_sheet = [i + 1 for i, id_sheet in enumerate(all_ids_after_insert) if id_sheet in ids_for_date]

            if not rows_in_sheet:
                continue

            start_w, end_w = min(rows_in_sheet), max(rows_in_sheet)

            if len(rows_in_sheet) > 1:
                # 1. First destroy old merges (prevents error 400 about existing merge)
                for col in ["B", "C", "D", "E", "F", "G", "H", "I", "J", "K", "L"]:
                    col_idx = ord(col) - 65
                    all_requests.append({
                        "unmergeCells": {
                            "range": {"sheetId": sheet.id, "startRowIndex": start_w-1, "endRowIndex": end_w, "startColumnIndex": col_idx, "endColumnIndex": col_idx+1}
                        }
                    })
                    # 2. Then apply new merges
                    all_requests.append({
                        "mergeCells": {
                            "range": {"sheetId": sheet.id, "startRowIndex": start_w-1, "endRowIndex": end_w, "startColumnIndex": col_idx, "endColumnIndex": col_idx+1},
                            "mergeType": "MERGE_ALL"
                        }
                    })

            # Text formatting and bottom border — for every day, regardless of row count
            full_range = {"sheetId": sheet.id, "startRowIndex": start_w-1, "endRowIndex": end_w, "startColumnIndex": 0, "endColumnIndex": 28}
            all_requests.append({"repeatCell": {"range": full_range, "cell": {"userEnteredFormat": {"horizontalAlignment": "CENTER", "verticalAlignment": "MIDDLE"}}, "fields": "userEnteredFormat(horizontalAlignment,verticalAlignment)"}})
            all_requests.append({"updateBorders": {"range": full_range, "bottom": {"style": "SOLID_MEDIUM", "color": black}}})

            # Vertical lines
            for col_idx in [3, 11, 20, 25]:
                all_requests.append({"updateBorders": {"range": {"sheetId": sheet.id, "startRowIndex": start_w-1, "endRowIndex": end_w, "startColumnIndex": col_idx, "endColumnIndex": col_idx+1}, "right": {"style": "SOLID_MEDIUM", "color": black}}})

        if all_requests:
            try:
                sheet.spreadsheet.batch_update({"requests": all_requests})
                logger.info("Successfully unmerged, merged, and formatted the sheet in a single request.")
            except Exception as e:
                logger.error(f"Batch formatting error: {e}")

    try:
        merge_and_format_for_date(dates_to_fetch, sheet, activities, all_ids_after_insert)
    except Exception as e:
        logger.error(f"Failed to merge/format cells: {e}")

    logger.info("Data in the sheet synced successfully!")

    # After syncing Garmin data, we plan the next 7 days based on
    # the updated physiological metrics (same sheet/all_data, without reading again).
    plan_upcoming_days(sheet, all_data, today, client)

if __name__ == "__main__":
    main()