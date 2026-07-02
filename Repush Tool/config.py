"""
config.py
=========
Constants (default table names, SLA hours) and load/save of the JSON settings
file that sits next to the app. Kept separate so the UI code never has to know
*where* settings live or *how* they are serialised.
"""

import json
import os

# ---- defaults shown in the UI the first time it is opened ----
# Friendly profile name -> actual DB table for the "Main table" dropdown, so the
# operator picks LS/DP/BP/... and never types (or mis-types) a real table name.
PROFILE_TABLES = {
    "LS":     "public.meter_blockloadprofile",
    "DP":     "public.meter_dailyloadprofile",
    "BP":     "public.meter_billingprofile",
    "Events": "public.meter_events",
    "ESW":    "public.meter_eswprofiledata",
}
DEFAULT_PROFILE = "LS"                             # selected in the dropdown by default
DEFAULT_TABLE = PROFILE_TABLES[DEFAULT_PROFILE]    # main table the profile maps to
DEFAULT_BACKUP = "temp2"                           # backup table for breaching rows
DEFAULT_TEMP1 = "temp1"                            # full good dataset repushed in step 4
DEFAULT_REPUSH = "data_repush_settings_by_sequence"  # per-sequence repush table
DEFAULT_SETTINGS = "data_repush_settings"          # parent settings table (owns datarepushid)
DEFAULT_SLA_HOURS = "11"                            # SLA window in hours
DEFAULT_METER_SOURCE = "table"                      # "table" or "file"
DEFAULT_METER_TABLE = "june18"                      # example table name
DEFAULT_PROFILENAME = "EVENTS"                      # profilename written to settings

# Settings file lives right beside the app folder.
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "repush_config.json")


def load_config(path=CONFIG_PATH):
    """Return the saved settings as a dict, or {} if nothing is saved yet."""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_config(data, path=CONFIG_PATH):
    """Write the settings dict to disk. Raises OSError on failure."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
