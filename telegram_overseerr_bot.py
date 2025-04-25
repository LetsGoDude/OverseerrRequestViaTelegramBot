import logging
import base64 # For secure storage of login data
import requests
import urllib.parse
import json
import os
from enum import Enum
from typing import Optional
from datetime import datetime

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    CallbackQuery,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

###############################################################################
#                              BOT VERSION & BUILD
###############################################################################
VERSION = "3.0.0"
BUILD = "2025.04.20.245"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)
logger.info(f"Bot Version: {VERSION} BUILD: {BUILD}")

###############################################################################
#                    LOAD CONFIG OR ENVIRONMENT VARIABLES
###############################################################################
try:
    logger.info("Importing environment/config variables...")
    OVERSEERR_API_URL = (
        os.environ.get("OVERSEERR_API_URL")
        or getattr(__import__("config"), "OVERSEERR_API_URL", None)
    )
    OVERSEERR_API_KEY = (
        os.environ.get("OVERSEERR_API_KEY")
        or getattr(__import__("config"), "OVERSEERR_API_KEY", None)
    )
    TELEGRAM_TOKEN = (
        os.environ.get("TELEGRAM_TOKEN")
        or getattr(__import__("config"), "TELEGRAM_TOKEN", None)
    )
    # password initialization
    try:
        PASSWORD = os.environ.get("PASSWORD")
        if PASSWORD is None:
            # Try loading from config.py
            config_module = __import__("config")
            PASSWORD = getattr(config_module, "PASSWORD", None)
    except ImportError:
        # config.py not found, use None as fallback
        PASSWORD = None
    logger.info("Variables loaded successfully.")
except Exception as e:
    logger.error(f"Failed to load config: {e}")

###############################################################################
#                          OVERSEERR CONSTANTS
###############################################################################
STATUS_UNKNOWN = 1
STATUS_PENDING = 2
STATUS_PROCESSING = 3
STATUS_PARTIALLY_AVAILABLE = 4
STATUS_AVAILABLE = 5

ISSUE_TYPES = {
    1: "Video",
    2: "Audio",
    3: "Subtitle",
    4: "Other"
}

# Operating modes as enum
class BotMode(Enum):
    NORMAL = "normal"
    API = "api"
    SHARED = "shared"

# Define CURRENT_MODE globally
CURRENT_MODE = BotMode.NORMAL  # Default mode

# Contains the authorisation bit for 4K
PERMISSION_4K_MOVIE = 2048
PERMISSION_4K_TV = 4096

DEFAULT_POSTER_URL = "https://raw.githubusercontent.com/sct/overseerr/refs/heads/develop/public/images/overseerr_poster_not_found.png"

os.makedirs("data", exist_ok=True)  # Ensure 'data/' folder exists

###############################################################################
#                              FILE PATHS
###############################################################################
CONFIG_FILE = "data/bot_config.json"
USER_SELECTION_FILE = "data/api_mode_selections.json"  # For API mode
USER_SESSIONS_FILE = "data/normal_mode_sessions.json"  # For Normal mode
SHARED_SESSION_FILE = "data/shared_mode_session.json"  # For Shared mode

def load_config():
    """
    Loads the configuration from data/bot_config.json.
    Returns a dict with default values if the file is missing or invalid.
    """
    ensure_data_directory()
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            config = json.load(f)
            config.setdefault("group_mode", False)
            config.setdefault("primary_chat_id", {"chat_id": None, "message_thread_id": None})
            config["primary_chat_id"].setdefault("chat_id", None)
            config["primary_chat_id"].setdefault("message_thread_id", None)
            config.setdefault("mode", "normal")
            config.setdefault("users", {})
            logger.debug("Loaded configuration successfully")
            return config
    except (FileNotFoundError, json.JSONDecodeError, PermissionError) as e:
        logger.warning(f"Failed to load {CONFIG_FILE}: {e}. Using defaults.")
        default_config = {
            "group_mode": False,
            "primary_chat_id": {"chat_id": None, "message_thread_id": None},
            "mode": "normal",
            "users": {}
        }
        save_config(default_config)
        return default_config

async def send_message(context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str, reply_markup=None, allow_sending=True, message_thread_id: Optional[int]=None):
    """
    Sends a message to the specified chat_id, or to primary_chat_id (with thread) if group_mode is enabled.
    """
    if not allow_sending:
        logger.debug(f"Skipped sending message to chat {chat_id}: sending not allowed")
        return

    config = load_config()
    if config["group_mode"] and config["primary_chat_id"]["chat_id"] is not None:
        chat_id = config["primary_chat_id"]["chat_id"]
        message_thread_id = config["primary_chat_id"]["message_thread_id"]
        logger.info(f"Group mode enabled, redirecting message to primary_chat_id: {chat_id}, thread: {message_thread_id}")
    try:
        kwargs = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "Markdown",
            "reply_markup": reply_markup
        }
        if message_thread_id is not None:
            kwargs["message_thread_id"] = message_thread_id
        await context.bot.send_message(**kwargs)
    except Exception as e:
        logger.error(f"Failed to send message to chat {chat_id}, thread {message_thread_id}: {e}")

def is_command_allowed(chat_id: int, message_thread_id: Optional[int], config: dict, telegram_user_id: int) -> bool:
    """
    Checks if a command is allowed based on Group Mode, chat/thread, and user status.
    Admins can always use commands in private chats, even in Group Mode.
    """
    user_id_str = str(telegram_user_id)
    user = config["users"].get(user_id_str, {})
    is_admin = user.get("is_admin", False)
    is_blocked = user.get("is_blocked", False)

    if is_blocked:
        logger.debug(f"User {telegram_user_id} is blocked, denying command")
        return False

    if is_admin and chat_id > 0:  # Positive chat_id indicates a private chat
        logger.debug(f"Admin {telegram_user_id} in private chat {chat_id}, allowing command")
        return True

    if not config["group_mode"]:
        return True

    primary = config["primary_chat_id"]
    if primary["chat_id"] is None:
        logger.debug(f"Group Mode on, primary_chat_id unset, allowing command in chat {chat_id}")
        return True
    if chat_id != primary["chat_id"]:
        logger.info(f"Ignoring command in chat {chat_id}: Group Mode restricts to primary chat {primary['chat_id']}")
        return False
    if primary["message_thread_id"] is not None and message_thread_id != primary["message_thread_id"]:
        logger.info(f"Ignoring command in thread {message_thread_id}: Group Mode restricts to thread {primary['message_thread_id']}")
        return False
    return True

def user_is_authorized(telegram_user_id: int) -> bool:
    """
    Checks if a Telegram user is authorized based on the config.
    """
    config = load_config()
    user_id_str = str(telegram_user_id)
    user = config["users"].get(user_id_str, {})
    return user.get("is_authorized", False) and not user.get("is_blocked", False)

def ensure_data_directory():
    """
    Ensures the directory for bot_config.json exists.
    Creates the directory if it doesn't exist.
    """
    directory = os.path.dirname(CONFIG_FILE)
    if directory:
        try:
            os.makedirs(directory, exist_ok=True)
            logger.debug(f"Ensured directory exists: {directory}")
        except Exception as e:
            logger.error(f"Failed to create directory {directory}: {e}")

def save_config(config):
    """
    Saves the configuration to data/bot_config.json.
    Ensures the directory exists before writing.
    """
    ensure_data_directory()
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=4)
        logger.info(f"Configuration saved to {CONFIG_FILE}")
    except (IOError, PermissionError) as e:
        logger.error(f"Failed to save {CONFIG_FILE}: {e}")

def load_user_sessions():
    try:
        with open(USER_SESSIONS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def save_user_session(telegram_telegram_user_id: int, session_data: dict):
    """Save a user's session data to a JSON file for Normal mode."""
    ensure_data_directory()
    try:
        # Load existing sessions if file exists
        try:
            with open(USER_SESSIONS_FILE, "r", encoding="utf-8") as f:
                all_sessions = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError) as e:
            logger.info(f"Creating new sessions file: {e}")
            all_sessions = {}
        
        # Update with new session data
        all_sessions[str(telegram_telegram_user_id)] = session_data
        
        # Write to file
        with open(USER_SESSIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(all_sessions, f, indent=2)
        logger.info(f"Saved session for Telegram user {telegram_telegram_user_id} to {USER_SESSIONS_FILE}")
    except Exception as e:
        logger.error(f"Failed to save session for Telegram user {telegram_telegram_user_id}: {e}")
        raise  # Re-raise to catch in caller if needed

def load_user_session(telegram_user_id: int) -> dict | None:
    """Load a user's session data from the JSON file."""
    ensure_data_directory()
    try:
        with open(USER_SESSIONS_FILE, "r", encoding="utf-8") as f:
            all_sessions = json.load(f)
            return all_sessions.get(str(telegram_user_id))
    except (FileNotFoundError, json.JSONDecodeError):
        return None

def save_user_sessions(sessions):
    ensure_data_directory()
    with open(USER_SESSIONS_FILE, "w", encoding="utf-8") as f:
        json.dump(sessions, f, indent=2)
    logger.info("Saved user sessions")

def load_shared_session():
    try:
        with open(SHARED_SESSION_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None

def save_shared_session(session_data):
    ensure_data_directory()
    with open(SHARED_SESSION_FILE, "w", encoding="utf-8") as f:
        json.dump(session_data, f, indent=2)
    logger.info("Saved shared session")

def clear_shared_session():
    """Clear the shared session data."""
    ensure_data_directory()
    if os.path.exists(SHARED_SESSION_FILE):
        os.remove(SHARED_SESSION_FILE)
        logger.info("Cleared shared session")

###############################################################################
#                PERSISTENT USER SELECTION LOGIC (Overseerr user)
###############################################################################
def load_user_selections() -> dict:
    """
    Load a dict from user_selection.json:
    {
      "<telegram_telegram_user_id>": {
        "userId": 10,
        "userName": "Some Name"
      },
      ...
    }
    """
    if not os.path.exists(USER_SELECTION_FILE):
        logger.info("No user_selection.json found. Returning empty dictionary.")
        return {}
    try:
        with open(USER_SELECTION_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            logger.info(f"Loaded user selections from {USER_SELECTION_FILE}: {data}")
            return data
    except (FileNotFoundError, json.JSONDecodeError):
        logger.warning("user_selection.json not found or invalid. Returning empty dictionary.")
        return {}

def save_user_selection(telegram_telegram_user_id: int, telegram_user_id: int, user_name: str):
    """
    Store the user's Overseerr selection in user_selection.json:
    {
      "<telegram_telegram_user_id>": {
        "userId": 10,
        "userName": "DisplayName"
      }
    }
    """
    data = load_user_selections()
    data[str(telegram_telegram_user_id)] = {
        "userId": telegram_user_id,
        "userName": user_name
    }
    try:
        with open(USER_SELECTION_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4)
        logger.info(f"Saved user selection for Telegram user {telegram_telegram_user_id}: (Overseerr user {telegram_user_id})")
    except Exception as e:
        logger.error(f"Failed to save user selection: {e}")

def get_saved_user_for_telegram_id(telegram_telegram_user_id: int):
    """
    Return (userId, userName) or (None, None) if not found.
    """
    data = load_user_selections()
    entry = data.get(str(telegram_telegram_user_id))
    if entry:
        logger.info(f"Found saved user for Telegram user {telegram_telegram_user_id}: {entry}")
        return entry["userId"], entry["userName"]
    logger.info(f"No saved user found for Telegram user {telegram_telegram_user_id}.")
    return None, None

###############################################################################
#                      OVERSEERR API: FETCH USERS
###############################################################################
def get_overseerr_users():
    """
    Fetch all Overseerr users via /api/v1/user.
    Returns a list of users or an empty list on error.
    """
    try:
        url = f"{OVERSEERR_API_URL}/user?take=256"
        logger.info(f"Fetching Overseerr users from: {url}")
        response = requests.get(
            url,
            headers={"X-Api-Key": OVERSEERR_API_KEY},
            timeout=10
        )
        response.raise_for_status()
        data = response.json()
        results = data.get("results", [])
        logger.info(f"Fetched {len(results)} Overseerr users.")
        return results
    except requests.RequestException as e:
        logger.error(f"Error fetching Overseerr users: {e}")
        return []

###############################################################################
#                     OVERSEERR API: SEARCH
###############################################################################
def search_media(media_name: str):
    """
    Search for media by title in Overseerr.
    Returns the JSON result or None on error.
    """
    try:
        logger.info(f"Searching for media: {media_name}")
        query_params = {'query': media_name}
        encoded_query = urllib.parse.urlencode(query_params, quote_via=urllib.parse.quote)
        url = f"{OVERSEERR_API_URL}/search?{encoded_query}"
        response = requests.get(
            url,
            headers={"X-Api-Key": OVERSEERR_API_KEY},
            timeout=10,
        )
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
        logger.error(f"Error during media search: {e}")
        return None

def process_search_results(results: list):
    """
    Process Overseerr search results into a simplified list of dicts.
    Each dict contains relevant fields (title, year, mediaType, etc.).
    """
    processed_results = []
    for result in results:
        media_title = (
            result.get("name")
            or result.get("originalName")
            or result.get("title")
            or "Unknown Title"
        )

        date_key = "firstAirDate" if result["mediaType"] == "tv" else "releaseDate"
        full_date_str = result.get(date_key, "")  # e.g. "2024-05-12"

        # Extract just the year from the date (if it exists)
        media_year = full_date_str.split("-")[0] if "-" in full_date_str else "Unknown Year"

        media_info = result.get("mediaInfo", {})
        overseerr_media_id = media_info.get("id")
        hd_status = media_info.get("status", 1)
        uhd_status = media_info.get("status4k", 1)

        processed_results.append({
            "title": media_title,
            "year": media_year,
            "id": result["id"],  # usually the TMDb ID
            "mediaType": result["mediaType"],
            "poster": result.get("posterPath"),
            "description": result.get("overview", "No description available"),
            "overseerr_id": overseerr_media_id,
            "release_date_full": full_date_str,
            "status_hd": hd_status,
            "status_4k": uhd_status
        })

    logger.info(f"Processed {len(results)} search results.")
    return processed_results

def overseerr_login(email: str, password: str) -> str | None:
    """Führt einen Login über die Overseerr-API aus und gibt den Session-Cookie zurück."""
    url = f"{OVERSEERR_API_URL}/auth/local"
    payload = {"email": email, "password": password}
    try:
        response = requests.post(
            url,
            headers={"Content-Type": "application/json"},
            json=payload,
            timeout=10
        )
        response.raise_for_status()
        cookie = response.cookies.get("connect.sid")
        logger.info(f"Login erfolgreich für {email}")
        return cookie
    except requests.RequestException as e:
        logger.error(f"Login fehlgeschlagen für {email}: {e}")
        return None

def overseerr_logout(session_cookie: str) -> bool:
    """Führt einen Logout über die Overseerr-API aus."""
    url = f"{OVERSEERR_API_URL}/auth/logout"
    try:
        response = requests.post(
            url,
            headers={"Cookie": f"connect.sid={session_cookie}"},
            timeout=10
        )
        response.raise_for_status()
        logger.info("Logout erfolgreich")
        return True
    except requests.RequestException as e:
        logger.error(f"Logout fehlgeschlagen: {e}")
        return False

def check_session_validity(session_cookie: str) -> bool:
    """Prüft, ob der Session-Cookie gültig ist, indem eine einfache API-Anfrage gestellt wird."""
    url = f"{OVERSEERR_API_URL}/auth/me"
    try:
        response = requests.get(
            url,
            headers={"Cookie": f"connect.sid={session_cookie}"},
            timeout=5
        )
        response.raise_for_status()
        return True
    except requests.RequestException:
        return False

###############################################################################
#              OVERSEERR API: REQUEST & ISSUE CREATION
###############################################################################
def request_media(media_id: int, media_type: str, requested_by: int = None, is4k: bool = False, session_cookie: str = None) -> tuple[bool, str]:
    payload = {"mediaType": media_type, "mediaId": media_id, "is4k": is4k}
    if requested_by is not None:  # Only in API Mode
        payload["userId"] = requested_by
    
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if session_cookie:
        headers["Cookie"] = f"connect.sid={session_cookie}"
    elif CURRENT_MODE == BotMode.API:
        headers["X-Api-Key"] = OVERSEERR_API_KEY
    else:
        return False, "No authentication provided."

    try:
        response = requests.post(f"{OVERSEERR_API_URL}/request", json=payload, headers=headers, timeout=10)
        logger.info(f"Request response: Status {response.status_code}, Body: {response.text}")
        if response.status_code == 201:
            return True, "Request successful"
        return False, f"Failed: {response.status_code} - {response.text}"
    except requests.RequestException as e:
        logger.error(f"Request failed: {e}")
        return False, f"Error: {str(e)}"

def create_issue(media_id: int, media_type: str, issue_description: str, issue_type: int, telegram_user_id: int = None, session_cookie: str = None) -> bool:
    """
    Create an issue on Overseerr via the API.
    Uses session cookies in NORMAL or SHARED mode, or the API key in ADMIN mode.
    
    Args:
        media_id (int): The Overseerr media ID.
        media_type (str): 'movie' or 'tv'.
        issue_description (str): Description of the issue.
        issue_type (int): Type of issue (1=Video, 2=Audio, 3=Subtitle, 4=Other).
        telegram_user_id (int, optional): Overseerr user ID reporting the issue.
        session_cookie (str, optional): Session cookie for authentication in NORMAL/SHARED modes.
    
    Returns:
        bool: True if the issue was created successfully, False otherwise.
    """
    # Prepare the payload for the API request
    payload = {
        "mediaId": media_id,
        "mediaType": media_type,
        "issueType": issue_type,
        "message": issue_description,
    }
    if telegram_user_id is not None:
        payload["userId"] = telegram_user_id

    # Log the payload being sent
    logger.info(f"Sending issue payload to Overseerr: {payload}")

    # Set up headers based on the current mode
    headers = {"Content-Type": "application/json"}
    if session_cookie and CURRENT_MODE != BotMode.ADMIN:
        headers["Cookie"] = f"connect.sid={session_cookie}"
    else:
        headers["X-Api-Key"] = OVERSEERR_API_KEY

    # Send the POST request to create the issue
    try:
        response = requests.post(
            f"{OVERSEERR_API_URL}/issue",
            headers=headers,
            json=payload,
            timeout=10,
        )
        response.raise_for_status()
        logger.info(f"Issue creation successful for mediaId {media_id}.")
        return True
    except requests.RequestException as e:
        logger.error(f"Error during issue creation: {e}")
        if e.response is not None:
            logger.error(f"Response content: {e.response.text}")
        return False


###############################################################################
#          get_latest_version_from_github: CHECK FOR UPDATES (OPTIONAL)
###############################################################################
def get_latest_version_from_github():
    """
    Check GitHub releases to find the latest version name (if any).
    Returns a string like 'v2.4.0' or an empty string on error.
    """
    try:
        response = requests.get(
            "https://api.github.com/repos/LetsGoDude/OverseerrRequestViaTelegramBot/releases/latest",
            timeout=10
        )
        response.raise_for_status()
        data = response.json()
        latest_version = data.get("tag_name", "")
        return latest_version
    except requests.RequestException as e:
        logger.warning(f"Failed to check latest version on GitHub: {e}")
        return ""

###############################################################################
#            user_data_loader: RUNS BEFORE OTHER HANDLERS (group=-999)
###############################################################################
async def user_data_loader(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Load user data, including session data and user selections, at the start of each update.
    Ensures overseerr_telegram_user_id is available across restarts.
    """
    telegram_telegram_user_id = update.effective_user.id if update.effective_user else update.callback_query.from_user.id
    logger.info(f"Loading user data for Telegram user {telegram_telegram_user_id} in mode {CURRENT_MODE.value}")

    # Normal mode: Load session data
    if CURRENT_MODE == BotMode.NORMAL:
        session_data = load_user_session(telegram_telegram_user_id)
        if session_data and "cookie" in session_data:
            context.user_data["session_data"] = session_data
            context.user_data["overseerr_telegram_user_id"] = session_data["overseerr_telegram_user_id"]
            context.user_data["overseerr_user_name"] = session_data.get("overseerr_user_name", "Unknown")
            logger.info(f"Loaded Normal mode session for user {telegram_telegram_user_id}: {session_data['overseerr_telegram_user_id']}")

    # API mode: Load user selection
    elif CURRENT_MODE == BotMode.API:
        overseerr_telegram_user_id, overseerr_user_name = get_saved_user_for_telegram_id(telegram_telegram_user_id)
        if overseerr_telegram_user_id:
            context.user_data["overseerr_telegram_user_id"] = overseerr_telegram_user_id
            context.user_data["overseerr_user_name"] = overseerr_user_name
            logger.info(f"Loaded API mode user selection for {telegram_telegram_user_id}: {overseerr_telegram_user_id} ({overseerr_user_name})")

    # Shared mode: Load shared session (global)
    elif CURRENT_MODE == BotMode.SHARED:
        shared_session = load_shared_session()
        if shared_session and "cookie" in shared_session:
            context.application.bot_data["shared_session"] = shared_session
            context.user_data["overseerr_telegram_user_id"] = shared_session["overseerr_telegram_user_id"]
            context.user_data["overseerr_user_name"] = shared_session.get("overseerr_user_name", "Shared User")
            logger.info(f"Loaded Shared mode session for user {telegram_telegram_user_id}: {shared_session['overseerr_telegram_user_id']}")

def get_global_telegram_notifications():
    """
    Retrieves the current global Telegram notification settings from Overseerr.
    Returns a dictionary with the settings or None on error.
    """
    try:
        url = f"{OVERSEERR_API_URL}/settings/notifications/telegram"
        headers = {
            "X-Api-Key": OVERSEERR_API_KEY
        }
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        settings = response.json()
        logger.info(f"Current Global Telegram notification settings: {settings}")
        return settings
    except requests.RequestException as e:
        logger.error(f"Error when retrieving Telegram notification settings: {e}")
        return None

async def set_global_telegram_notifications(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Activates the global Telegram notifications in Overseerr.
    Returns True if successful, otherwise False.
    """

    bot_info = await context.bot.get_me()
    chat_id = str(update.effective_chat.id)

    payload = {
        "enabled": True,
        "types": 1,  # Disable all notification types (except silent)
        "options": {
            "botUsername": bot_info.username,  # Botname
            "botAPI": TELEGRAM_TOKEN,          # Telegram Token
            "chatId": chat_id,                 # Chat-ID - i guess the admin will use the bot first?
            "sendSilently": True
        }
    }
    try:
        url = f"{OVERSEERR_API_URL}/settings/notifications/telegram"
        headers = {
            "Content-Type": "application/json",
            "X-Api-Key": OVERSEERR_API_KEY
        }
        response = requests.post(url, headers=headers, json=payload, timeout=10)
        response.raise_for_status()
        logger.info("Global Telegram notifications have been successfully activated.")
        return True
    except requests.RequestException as e:
        logger.error(f"Error when activating global Telegram notifications: {e}")
        if e.response is not None:
            logger.error(f"Response content: {e.response.text}")
        return False

GLOBAL_TELEGRAM_NOTIFICATION_STATUS = get_global_telegram_notifications()

async def enable_global_telegram_notifications(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Activates global Telegram notifications
    """
    if GLOBAL_TELEGRAM_NOTIFICATION_STATUS:
        enabled = GLOBAL_TELEGRAM_NOTIFICATION_STATUS.get("enabled", False)
        if enabled:

            logger.info("Global Telegram notifications are activated.")
        else:
            logger.info("Activate global Telegram notifications...")
            await set_global_telegram_notifications(update, context)
    else:
        logger.error("Could not retrieve Global Telegram notification settings.")


async def start_login(update_or_query: Update | CallbackQuery, context: ContextTypes.DEFAULT_TYPE):
    """Initiates login, deleting the settings menu and cleaning up prompts."""
    if isinstance(update_or_query, Update):
        telegram_telegram_user_id = update_or_query.effective_user.id
        message = update_or_query.message
    else:  # CallbackQuery
        telegram_telegram_user_id = update_or_query.from_user.id
        message = update_or_query.message

    logger.info(f"User {telegram_telegram_user_id} started login process.")

    # Check mode restrictions
    if CURRENT_MODE == BotMode.API:
        await message.reply_text("In API Mode, no login is required.")
        return
    if CURRENT_MODE == BotMode.SHARED and telegram_telegram_user_id != load_config()["admin_telegram_id"]:
        await message.reply_text("In Shared Mode, only the admin can log in.")
        return

    # Delete the settings menu if this is a callback query
    if isinstance(update_or_query, CallbackQuery):
        try:
            await message.delete()
            logger.info(f"Deleted settings menu message for user {telegram_telegram_user_id} during login.")
        except Exception as e:
            logger.warning(f"Failed to delete settings menu message: {e}")

    # Set login state and prompt for email
    context.user_data["login_step"] = "email"
    msg = await context.bot.send_message(
        chat_id=message.chat_id,
        text="Please enter your Overseerr email address:"
    )
    context.user_data["login_message_id"] = msg.message_id

async def handle_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handles text input from users, including password authentication and issue reporting.
    """
    telegram_user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    message_thread_id = getattr(update.message, "message_thread_id", None)
    text = update.message.text
    logger.info(f"Text input from {telegram_user_id}: {text}, awaiting_password: {context.user_data.get('awaiting_password')}, chat {chat_id}, thread {message_thread_id}")

    config = load_config()
    user_id_str = str(telegram_user_id)
    user = config["users"].get(user_id_str, {})

    # Update username if necessary
    if not user or user.get("username") != (update.effective_user.username or update.effective_user.full_name):
        config["users"][user_id_str] = {
            "username": update.effective_user.username or update.effective_user.full_name,
            "is_authorized": user.get("is_authorized", False),
            "is_blocked": user.get("is_blocked", False),
            "is_admin": user.get("is_admin", False),
            "created_at": user.get("created_at", datetime.utcnow().isoformat() + "Z")
        }
        save_config(config)

    # Handle issue reporting
    if 'reporting_issue' in context.user_data:
        issue_description = text
        reporting_issue = context.user_data['reporting_issue']
        issue_type_id = reporting_issue['issue_type']
        issue_type_name = reporting_issue['issue_type_name']

        selected_result = context.user_data.get('selected_result')
        if not selected_result:
            logger.error("No selected_result found while reporting an issue.")
            await update.message.reply_text(
                "An error occurred. Please try reporting the issue again.",
                parse_mode="Markdown",
            )
            return

        media_id = selected_result.get('overseerr_id')
        media_title = selected_result['title']
        media_type = selected_result['mediaType']

        telegram_user_id_for_issue = context.user_data.get("overseerr_telegram_user_id")
        user_display_name = context.user_data.get("overseerr_user_name", "Unknown User")
        logger.info(
            f"User {telegram_user_id} is reporting an issue on mediaId {media_id} "
            f"as Overseerr user {telegram_user_id_for_issue}."
        )

        final_issue_description = f"(Reported by {user_display_name})\n\n{issue_description}"

        success = create_issue(
            media_id=media_id,
            media_type=media_type,
            issue_description=final_issue_description,
            issue_type=issue_type_id,
            telegram_user_id=telegram_user_id_for_issue
        )

        if success:
            await update.message.reply_text(
                f"✅ Thank you! Your issue with *{media_title}* has been successfully reported.",
                parse_mode="Markdown",
            )
        else:
            await update.message.reply_text(
                f"❌ Failed to report the issue with *{media_title}*. Please try again later.",
                parse_mode="Markdown",
            )

        # Cleanup
        context.user_data.pop('reporting_issue', None)
        media_message_id = context.user_data.get('media_message_id')
        if media_message_id:
            try:
                await context.bot.delete_message(
                    chat_id=update.message.chat_id,
                    message_id=media_message_id
                )
                logger.info(f"Deleted media message {media_message_id} after issue reporting.")
            except Exception as e:
                logger.warning(f"Failed to delete media message {media_message_id}: {e}")
            context.user_data.pop('media_message_id', None)

        context.user_data.pop('selected_result', None)
        return

    # Handle password authentication
    if context.user_data.get("awaiting_password"):
        logger.info(f"Comparing input '{text}' with PASSWORD '{PASSWORD}'")
        if text == PASSWORD:
            is_admin = user.get("is_admin", False)
            if not user.get("is_authorized", False):
                config["users"][user_id_str] = {
                    "username": update.effective_user.username or update.effective_user.full_name,
                    "is_authorized": True,
                    "is_blocked": False,
                    "is_admin": is_admin,
                    "created_at": datetime.utcnow().isoformat() + "Z"
                }
                save_config(config)
                logger.info(f"User {telegram_user_id} added to users with authorized status")
            context.user_data.pop("awaiting_password")
            await send_message(context, chat_id, "✅ *Access granted!* Let’s get started...", message_thread_id=message_thread_id)
            await context.bot.delete_message(chat_id=chat_id, message_id=update.message.message_id)
            await start_command(update, context)
            if not is_admin and CURRENT_MODE == BotMode.API:
                await handle_change_user(update, context, is_initial=True)
        else:
            await send_message(context, chat_id, "❌ *Oops!* That’s not the right password. Try again:", message_thread_id=message_thread_id)
            await context.bot.delete_message(chat_id=chat_id, message_id=update.message.message_id)
        return

    # Ignore non-command text input if Group Mode restricts this chat/thread
    if not is_command_allowed(chat_id, message_thread_id, config, telegram_user_id):
        logger.info(f"Ignoring text input in chat {chat_id}, thread {message_thread_id}: Group Mode restricts to primary")
        return

    # Handle Overseerr login
    if "login_step" in context.user_data:
        # Delete previous prompt and user input
        if "login_message_id" in context.user_data:
            try:
                await context.bot.delete_message(chat_id, context.user_data["login_message_id"])
            except Exception as e:
                logger.warning(f"Failed to delete login prompt message: {e}")
        try:
            await context.bot.delete_message(chat_id, update.message.message_id)
        except Exception as e:
            logger.warning(f"Failed to delete user input message: {e}")

        is_admin = user.get("is_admin", False)

        if context.user_data["login_step"] == "email":
            context.user_data["login_email"] = text
            context.user_data["login_step"] = "password"
            msg = await context.bot.send_message(chat_id, "Please enter your Overseerr password:")
            context.user_data["login_message_id"] = msg.message_id
        elif context.user_data["login_step"] == "password":
            email = context.user_data["login_email"]
            password = text
            session_cookie = overseerr_login(email, password)
            if session_cookie:
                credentials = base64.b64encode(f"{email}:{password}".encode()).decode()
                response = requests.get(
                    f"{OVERSEERR_API_URL}/auth/me",
                    headers={"Cookie": f"connect.sid={session_cookie}"}
                )
                user_info = response.json()
                overseerr_id = user_info.get("id")
                if not overseerr_id:
                    await context.bot.send_message(chat_id, "❌ Login failed: Invalid user data.")
                    await show_settings_menu(update, context, is_admin=is_admin)
                    return
                
                session_data = {
                    "cookie": session_cookie,
                    "credentials": credentials,
                    "overseerr_telegram_user_id": overseerr_id,
                    "overseerr_user_name": user_info.get("displayName", "Unknown")
                }
                context.user_data["session_data"] = session_data
                
                if CURRENT_MODE == BotMode.NORMAL:
                    sessions = load_user_sessions()
                    sessions[str(telegram_user_id)] = session_data
                    save_user_sessions(sessions)
                elif CURRENT_MODE == BotMode.SHARED and is_admin:
                    save_shared_session(session_data)
                    context.application.bot_data["shared_session"] = session_data
                
                await context.bot.send_message(
                    chat_id,
                    f"✅ Logged in as {user_info.get('displayName', 'Unknown')}!"
                )
            else:
                await context.bot.send_message(chat_id, "❌ Login failed. Check your credentials.")
            
            context.user_data.pop("login_step", None)
            context.user_data.pop("login_email", None)
            context.user_data.pop("login_message_id", None)
            await show_settings_menu(update, context, is_admin=is_admin)
        return

    # Fallback für nicht erkannte Eingaben
    logger.info(f"User {telegram_user_id} typed something unrecognized: {text}")
    await update.message.reply_text(
        "I didn't understand that. Please use /start to see the available commands."
    )

async def show_user_management_menu(update_or_query, context: ContextTypes.DEFAULT_TYPE, offset=0):
    """
    Displays the user management menu with pagination within the settings menu, including an option to create a new user.
    """
    config = load_config()
    if isinstance(update_or_query, Update):
        telegram_user_id = update_or_query.effective_user.id
        chat_id = update_or_query.effective_chat.id
        message_thread_id = getattr(update_or_query.message, "message_thread_id", None)
    else:
        telegram_user_id = update_or_query.from_user.id
        chat_id = update_or_query.message.chat_id
        message_thread_id = getattr(update_or_query.message, "message_thread_id", None)

    user_id_str = str(telegram_user_id)
    if not config["users"].get(user_id_str, {}).get("is_admin", False):
        await send_message(context, chat_id, "❌ Only admins can manage users.", message_thread_id=message_thread_id)
        return

    users = [
        {
            "telegram_id": uid,
            "username": details.get("username", "Unknown"),
            "is_admin": details.get("is_admin", False),
            "is_blocked": details.get("is_blocked", False)
        }
        for uid, details in config["users"].items()
    ]

    if not users:
        text = "👥 *User Management*\n\nNo users found."
        keyboard = [
            [InlineKeyboardButton("➕ Create New User", callback_data="create_user")],
            [InlineKeyboardButton("⬅️ Back to Settings", callback_data="back_to_settings")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        if isinstance(update_or_query, Update):
            await send_message(context, chat_id, text, reply_markup=reply_markup, message_thread_id=message_thread_id)
        else:
            await update_or_query.edit_message_text(text, parse_mode="Markdown", reply_markup=reply_markup)
        return

    page_size = 5
    total_users = len(users)
    current_users = users[offset:offset + page_size]

    text = "👥 *User Management*\n\nSelect a user to manage:\n"
    keyboard = []
    for user in current_users:
        status = "🚫 Blocked" if user["is_blocked"] else "👑 Admin" if user["is_admin"] else "✅ User"
        button_text = f"{user['username']} (ID: {user['telegram_id']}) - {status}"
        keyboard.append([InlineKeyboardButton(button_text, callback_data=f"manage_user_{user['telegram_id']}")])

    keyboard.append([InlineKeyboardButton("➕ Create New User", callback_data="create_user")])

    navigation_buttons = []
    if offset > 0:
        navigation_buttons.append(InlineKeyboardButton("⬅️ Back", callback_data=f"users_page_{offset - page_size}"))
    if offset + page_size < total_users:
        navigation_buttons.append(InlineKeyboardButton("➡️ More", callback_data=f"users_page_{offset + page_size}"))
    navigation_buttons.append(InlineKeyboardButton("⬅️ Back to Settings", callback_data="back_to_settings"))
    keyboard.append(navigation_buttons)

    reply_markup = InlineKeyboardMarkup(keyboard)

    if isinstance(update_or_query, Update):
        await send_message(context, chat_id, text, reply_markup=reply_markup, message_thread_id=message_thread_id)
    else:
        await update_or_query.edit_message_text(text, parse_mode="Markdown", reply_markup=reply_markup)

async def manage_specific_user(query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE, telegram_id: str):
    """
    Manages a specific user (block, unblock, promote, demote).
    """
    config = load_config()
    telegram_user_id = query.from_user.id
    chat_id = query.message.chat_id
    message_thread_id = getattr(query.message, "message_thread_id", None)

    user_id_str = str(telegram_user_id)
    if not config["users"].get(user_id_str, {}).get("is_admin", False):
        await query.edit_message_text("❌ Only admins can manage users.")
        return

    user = config["users"].get(telegram_id, {})
    username = user.get("username", "Unknown")
    is_admin = user.get("is_admin", False)
    is_blocked = user.get("is_blocked", False)

    text = (
        f"👤 *Manage User: {username}*\n"
        f"🆔 ID: {telegram_id}\n"
        f"🔖 Status: {'Blocked' if is_blocked else 'Admin' if is_admin else 'User'}\n\n"
        "Choose an action:"
    )

    keyboard = []
    if is_blocked:
        keyboard.append([InlineKeyboardButton("✅ Unblock User", callback_data=f"unblock_user_{telegram_id}")])
    else:
        keyboard.append([InlineKeyboardButton("🚫 Block User", callback_data=f"block_user_{telegram_id}")])
    if is_admin and telegram_id != user_id_str:
        keyboard.append([InlineKeyboardButton("👇 Demote to User", callback_data=f"demote_user_{telegram_id}")])
    elif not is_admin and not is_blocked:
        keyboard.append([InlineKeyboardButton("👑 Promote to Admin", callback_data=f"promote_user_{telegram_id}")])
    keyboard.append([InlineKeyboardButton("⬅️ Back to User List", callback_data="manage_users")])

    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(text, parse_mode="Markdown", reply_markup=reply_markup)

###############################################################################
#                           BOT COMMAND HANDLERS
###############################################################################
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /start command. Shows a welcome message and sets the primary chat/thread if Group Mode is enabled.
    """
    telegram_user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    message_thread_id = getattr(update.message, "message_thread_id", None)
    logger.info(f"User {telegram_user_id} executed /start in chat {chat_id}, thread {message_thread_id}")

    config = load_config()

    # Check if command is allowed
    if not is_command_allowed(chat_id, message_thread_id, config, telegram_user_id):
        return

    # Handle unauthorized users
    if PASSWORD and not user_is_authorized(telegram_user_id):
        logger.info(f"User {telegram_user_id} is not authorized. Requesting password.")
        await send_message(
            context,
            chat_id,
            "👋 *Welcome!* Please enter the bot’s password to get started:",
            message_thread_id=message_thread_id
        )
        context.user_data["awaiting_password"] = True
        logger.info(f"Set awaiting_password=True for user {telegram_user_id}")
        return

    # Set primary_chat_id only if Group Mode is enabled
    if config["group_mode"]:
        config["primary_chat_id"] = {
            "chat_id": chat_id,
            "message_thread_id": message_thread_id
        }
        save_config(config)
        logger.info(f"Set primary_chat_id to chat {chat_id}, thread {message_thread_id} in {CONFIG_FILE}")

    # Set first user as admin if no admin exists
    user_id_str = str(telegram_user_id)
    if not any(user.get("is_admin", False) for user in config["users"].values()):
        config["users"][user_id_str] = {
            "username": update.effective_user.username or update.effective_user.full_name,
            "is_authorized": True,
            "is_blocked": False,
            "is_admin": True,
            "created_at": "2025-04-20T12:00:00Z"
        }
        save_config(config)
        logger.info(f"Set user {telegram_user_id} as admin")

    await enable_global_telegram_notifications(update, context)

    # Set first user as admin if no admin exists
    user_id_str = str(telegram_user_id)
    if not any(user.get("is_admin", False) for user in config["users"].values()):
        config["users"][user_id_str] = {
            "username": update.effective_user.username or update.effective_user.full_name,
            "is_authorized": True,
            "is_blocked": False,
            "is_admin": True,
            "created_at": datetime.utcnow().isoformat() + "Z"
        }
        save_config(config)
        logger.info(f"Set user {telegram_user_id} as admin")

    # Version check
    latest_version = get_latest_version_from_github()
    newer_version_text = ""
    if latest_version:
        latest_stripped = latest_version.strip().lstrip("v")
        if latest_stripped > VERSION:
            newer_version_text = f"\n🔔 A new version ({latest_version}) is available!"
        else:
            logger.info(f"Current version {VERSION} is up to date or newer than {latest_version}")

    # Base welcome message
    start_message = (
        f"👋 *Welcome to the Overseerr Telegram Bot!* v{VERSION}"
        f"{newer_version_text}"
        "\n\n🎬 *What I can do:*\n"
        " - 🔍 Search movies & TV shows\n"
        " - 📊 Check availability\n"
        " - 🎫 Request new titles\n"
        " - 🛠 Report issues\n\n"
        "💡 *How to start:* Type `/check <title>`\n"
        "_Example: `/check Venom`_\n\n"
        "You can also configure your preferences with [/settings]."
    )

    # Add login prompt only for non-admins in Normal mode
    reply_markup = None
    user = config["users"].get(user_id_str, {})
    is_admin = user.get("is_admin", False)
    if CURRENT_MODE == BotMode.NORMAL and not is_admin and "session_data" not in context.user_data:
        start_message += (
            "\n\n🔑 *Login Required*\n"
            "Please log in with your Overseerr credentials to start requesting media."
        )
        keyboard = [[InlineKeyboardButton("🔑 Login", callback_data="login")]]
        reply_markup = InlineKeyboardMarkup(keyboard)

    await send_message(context, chat_id, start_message, reply_markup=reply_markup, message_thread_id=message_thread_id)

########################################################################
#                 UNIFIED SETTINGS MENU FUNCTION
########################################################################
async def show_settings_menu(update_or_query, context: ContextTypes.DEFAULT_TYPE, is_admin=False):
    """
    Displays the settings menu tailored for users or admins with conditional buttons.
    In Shared mode, only the admin can access settings. Manage Notifications button is only shown if an Overseerr user is selected.
    """
    if isinstance(update_or_query, Update):
        telegram_user_id = update_or_query.effective_user.id
        chat_id = update_or_query.effective_chat.id
        message_thread_id = getattr(update_or_query.message, "message_thread_id", None)
    elif isinstance(update_or_query, CallbackQuery):
        telegram_user_id = update_or_query.from_user.id
        chat_id = update_or_query.message.chat_id
        message_thread_id = getattr(update_or_query.message, "message_thread_id", None)
    else:
        logger.error("Invalid argument type passed to show_settings_menu")
        return

    config = load_config()

    # Check if command/query is allowed
    if not is_command_allowed(chat_id, message_thread_id, config, telegram_user_id):
        return

    user_id_str = str(telegram_user_id)
    user = config["users"].get(user_id_str, {})
    is_admin = user.get("is_admin", False)

    # In Shared mode, only the admin can access settings
    if CURRENT_MODE == BotMode.SHARED and not is_admin:
        logger.info(f"Non-admin {telegram_user_id} attempted to access settings in Shared mode; ignoring.")
        return

    # Restrict access if unauthorized
    if PASSWORD and not user_is_authorized(telegram_user_id):
        await send_message(
            context,
            chat_id,
            "🔒 *Access Denied*\nPlease enter the bot’s password via /start to access settings.",
            message_thread_id=message_thread_id
        )
        return

    # Refresh user data based on CURRENT_MODE
    context.user_data.pop("overseerr_telegram_user_id", None)
    context.user_data.pop("overseerr_user_name", None)
    context.user_data.pop("session_data", None)

    if CURRENT_MODE == BotMode.NORMAL:
        session_data = load_user_session(telegram_user_id)
        if session_data and "cookie" in session_data:
            context.user_data["session_data"] = session_data
            context.user_data["overseerr_telegram_user_id"] = session_data["overseerr_telegram_user_id"]
            context.user_data["overseerr_user_name"] = session_data.get("overseerr_user_name", "Unknown")
            logger.info(f"Loaded Normal mode session for user {telegram_user_id}: {session_data['overseerr_telegram_user_id']}")
    elif CURRENT_MODE == BotMode.API:
        overseerr_telegram_user_id, overseerr_user_name = get_saved_user_for_telegram_id(telegram_user_id)
        if overseerr_telegram_user_id:
            context.user_data["overseerr_telegram_user_id"] = overseerr_telegram_user_id
            context.user_data["overseerr_user_name"] = overseerr_user_name
            logger.info(f"Loaded API mode user selection for {telegram_user_id}: {overseerr_telegram_user_id} ({overseerr_user_name})")
    elif CURRENT_MODE == BotMode.SHARED:
        shared_session = load_shared_session()
        if shared_session and "cookie" in shared_session:
            context.application.bot_data["shared_session"] = shared_session
            context.user_data["overseerr_telegram_user_id"] = shared_session["overseerr_telegram_user_id"]
            context.user_data["overseerr_user_name"] = shared_session.get("overseerr_user_name", "Shared User")
            logger.info(f"Loaded Shared mode session for user {telegram_user_id}: {shared_session['overseerr_telegram_user_id']}")

    # Get current Overseerr user info (if any)
    overseerr_user_name = context.user_data.get("overseerr_user_name", "None selected")
    overseerr_telegram_user_id = context.user_data.get("overseerr_telegram_user_id", "N/A")
    user_info = f"{overseerr_user_name} ({overseerr_telegram_user_id}) ✅" if overseerr_telegram_user_id != "N/A" else "Not set ❌"

    group_mode_status = "🟢 On" if config["group_mode"] else "🔴 Off"

    if is_admin:
        mode_symbols = {
            BotMode.NORMAL: "🌟",
            BotMode.API: "🔑",
            BotMode.SHARED: "👥"
        }
        mode_symbol = mode_symbols.get(CURRENT_MODE, "❓")
        text = (
            "⚙️ *Admin Settings*\n\n"
            f"🤖 *Bot Mode:* {mode_symbol} *{CURRENT_MODE.value.capitalize()}*\n"
            f"👤 *Current User:* {user_info}\n"
            f"👥 *Group Mode:* {group_mode_status}\n\n"
            "Select an option below to manage your settings:\n"
        )
    else:
        text = (
            "⚙️ *Settings - Current User:*\n\n"
            f"👤 {user_info}\n\n"
            "Select an option below to manage your settings:\n"
        )

    keyboard = []
    account_buttons = []
    if CURRENT_MODE == BotMode.API:
        account_buttons.append(InlineKeyboardButton("🔄 Change User", callback_data="change_user"))
    elif CURRENT_MODE == BotMode.NORMAL:
        if context.user_data.get("session_data"):
            account_buttons.append(InlineKeyboardButton("🔓 Logout", callback_data="logout"))
        else:
            account_buttons.append(InlineKeyboardButton("🔑 Login", callback_data="login"))
    elif CURRENT_MODE == BotMode.SHARED and is_admin:
        if context.application.bot_data.get("shared_session"):
            account_buttons.append(InlineKeyboardButton("🔓 Logout", callback_data="logout"))
        else:
            account_buttons.append(InlineKeyboardButton("🔑 Login", callback_data="login"))
    if account_buttons:
        keyboard.append(account_buttons)

    if is_admin:
        keyboard.extend([
            [InlineKeyboardButton("🔧 Change Mode", callback_data="mode_select")],
            [InlineKeyboardButton(f"👥 Group Mode: {group_mode_status}", callback_data="toggle_group_mode")],
            [InlineKeyboardButton("👤 Manage Users", callback_data="manage_users")]
        ])
    
    # Show Manage Notifications only if an Overseerr user is selected
    if overseerr_telegram_user_id != "N/A":
        keyboard.append([InlineKeyboardButton("🔔 Manage Notifications", callback_data="manage_notifications")])

    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel_settings")])
    reply_markup = InlineKeyboardMarkup(keyboard)

    if isinstance(update_or_query, Update):
        await send_message(context, chat_id, text, reply_markup=reply_markup, message_thread_id=message_thread_id)
    else:
        await update_or_query.edit_message_text(text, parse_mode="Markdown", reply_markup=reply_markup)
    

async def show_manage_notifications_menu(update_or_query, context: ContextTypes.DEFAULT_TYPE):
    """
    Displays a menu letting the user toggle:
      - Telegram notifications on/off (interpreted from notificationTypes.telegram)
      - Silent mode on/off (from telegramSendSilently)
    for their selected Overseerr user, using partial updates.
    """
    # Check if it's a callback or normal command
    if isinstance(update_or_query, Update) and update_or_query.message:
        query = None
        telegram_user_id = update_or_query.effective_user.id
        chat_id = update_or_query.effective_chat.id
    elif isinstance(update_or_query, CallbackQuery):
        query = update_or_query
        telegram_user_id = query.from_user.id
        chat_id = query.message.chat_id
    else:
        return

    # Which Overseerr user is selected?
    overseerr_telegram_user_id = context.user_data.get("overseerr_telegram_user_id")
    overseerr_user_name = context.user_data.get("overseerr_user_name", "Unknown User")

    if not overseerr_telegram_user_id:
        msg = "No Overseerr user selected. Use /settings to pick a user first."
        if query:
            await query.edit_message_text(msg)
        else:
            await update_or_query.message.reply_text(msg)
        return

    # Fetch from Overseerr to show real-time status
    current_settings = get_user_notification_settings(overseerr_telegram_user_id)
    if not current_settings:
        error_text = f"Failed to retrieve notification settings for Overseerr user {overseerr_telegram_user_id}."
        if query:
            await query.edit_message_text(error_text)
        else:
            await update_or_query.message.reply_text(error_text)
        return

    # Extract relevant fields
    # If notificationTypes or telegram is missing, default to 0
    notification_types = current_settings.get("notificationTypes", {})
    telegram_bitmask = notification_types.get("telegram", 0)
    telegram_silent = current_settings.get("telegramSendSilently", False)

    # We interpret telegram_bitmask > 0 to mean "enabled"
    telegram_is_enabled = (telegram_bitmask != 0)

    # Build the display text
    heading_text = (
        "🔔 *Notification Settings*\n"
        "Manage how Overseerr sends you updates via Telegram.\n\n"
        f"👤 *User Information:*\n"
        f"   - Name: *{overseerr_user_name}* (ID: `{overseerr_telegram_user_id}`)\n\n"
        "⚙️ *Current Telegram Settings:*\n"
        f"   - Notifications: {'*Enabled* ✅' if telegram_is_enabled else '*Disabled* ❌'}\n"
        f"   - Silent Mode: {'*On* 🤫' if telegram_silent else '*Off* 🔊'}\n\n"
        "🔄 *Actions:*\n"
        "Use the buttons below to toggle notifications or silent mode. "
        "Your preferences will be updated immediately."
    )

    # Build inline keyboard
    # "Disable notifications" if currently enabled, or "Enable notifications" if it's disabled
    toggle_telegram_label = "Disable notifications" if telegram_is_enabled else "Enable notifications"
    toggle_silent_label = "Turn Silent Off" if telegram_silent else "Turn Silent On"

    keyboard = [
        [
            InlineKeyboardButton(toggle_telegram_label, callback_data="toggle_user_notifications")
        ],
        [
            InlineKeyboardButton(toggle_silent_label, callback_data="toggle_user_silent")
        ],
        [
            InlineKeyboardButton("⬅️ Back to Settings", callback_data="back_to_settings")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    # Edit or send message
    if query:
        await query.edit_message_text(
            text=heading_text,
            parse_mode="Markdown",
            reply_markup=reply_markup
        )
    else:
        await update_or_query.message.reply_text(
            text=heading_text,
            parse_mode="Markdown",
            reply_markup=reply_markup
        )

def get_user_notification_settings(overseerr_telegram_user_id: int) -> dict:
    """
    (Optional) Fetch the user's notification settings from Overseerr:
    GET /api/v1/user/<OverseerrUserID>/settings/notifications
    Returns a dict or an empty dict on error.
    """
    try:
        url = f"{OVERSEERR_API_URL}/user/{overseerr_telegram_user_id}/settings/notifications"
        headers = {
            "X-Api-Key": OVERSEERR_API_KEY,
            "Content-Type": "application/json"
        }
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        logger.info(f"Fetched notification settings for Overseerr user {overseerr_telegram_user_id}: {data}")
        return data
    except requests.RequestException as e:
        logger.error(f"Failed to fetch settings for user {overseerr_telegram_user_id}: {e}")
        return {}

def update_telegram_settings_for_user(
    overseerr_telegram_user_id: int,
    telegram_bitmask: int,       # either 3657 or 0
    chat_id: str,
    send_silently: bool
) -> bool:
    """
    Sends a partial update to /user/<OverseerrUserID>/settings/notifications.
    - telegram_bitmask=3657: enable all telegram notifications
    - telegram_bitmask=0: disable all telegram notifications
    - We set telegramEnabled=true because Overseerr apparently keeps it that way.
    - telegramChatId is necessary so Overseerr knows which chat to use.
    """
    payload = {
        "notificationTypes": {
            "telegram": telegram_bitmask
        },
        "telegramEnabled": True,         # Always true in Overseerr DB
        "telegramChatId": chat_id,       # Must provide the chat ID
        "telegramSendSilently": send_silently
    }

    url = f"{OVERSEERR_API_URL}/user/{overseerr_telegram_user_id}/settings/notifications"
    headers = {
        "X-Api-Key": OVERSEERR_API_KEY,
        "Content-Type": "application/json"
    }
    logger.info(f"Updating user {overseerr_telegram_user_id} with payload: {payload}")

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=10)
        resp.raise_for_status()
        logger.info(f"Successfully updated telegram bitmask for user {overseerr_telegram_user_id}.")
        return True
    except requests.RequestException as e:
        logger.error(f"Failed to update telegram bitmask for user {overseerr_telegram_user_id}: {e}")
        if e.response is not None:
            logger.error(f"Response content: {e.response.text}")
        return False

async def toggle_user_notifications(query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE):
    """
    If the user currently has 0 => switch to 3657.
    If the user currently has 3657 => switch to 0.
    """
    overseerr_telegram_user_id = context.user_data.get("overseerr_telegram_user_id")
    if not overseerr_telegram_user_id:
        await query.edit_message_text("No Overseerr user selected.")
        return

    # GET the current settings to see if it's 0 or not
    settings = get_user_notification_settings(overseerr_telegram_user_id)
    if not settings:
        await query.edit_message_text(f"Failed to get settings for user {overseerr_telegram_user_id}.")
        return
    
    # If "notificationTypes" or "notificationTypes.telegram" is missing, default to 0
    notif_types = settings.get("notificationTypes", {})
    current_value = notif_types.get("telegram", 0)

    # Flip between 0 and 3657
    new_value = 3657 if current_value == 0 else 0

    telegram_silent = settings.get("telegramSendSilently", False)
    chat_id = str(query.message.chat_id)

    success = update_telegram_settings_for_user(
        overseerr_telegram_user_id=overseerr_telegram_user_id,
        telegram_bitmask=new_value,
        chat_id=chat_id,
        send_silently=telegram_silent
    )

    if not success:
        await query.edit_message_text("❌ Failed to update Telegram bitmask in Overseerr.")
        return

    # Optionally re-fetch or trust your new data
    await show_manage_notifications_menu(query, context)

async def toggle_user_silent(query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE):
    """
    Toggles the 'telegramSendSilently' flag for the currently selected Overseerr user.
    This only changes whether notifications (if enabled) are sent silently or not.
    If notifications are disabled (bitmask=0), silent mode won't matter in practice,
    but we still update the 'telegramSendSilently' field in Overseerr.
    """
    telegram_user_id = query.from_user.id

    overseerr_telegram_user_id = context.user_data.get("overseerr_telegram_user_id")
    if not overseerr_telegram_user_id:
        await query.edit_message_text("No Overseerr user selected.")
        return

    current_settings = get_user_notification_settings(overseerr_telegram_user_id)
    if not current_settings:
        await query.edit_message_text(
            f"Failed to fetch notification settings for user {overseerr_telegram_user_id}."
        )
        return

    # Instead of relying on telegramEnabled (which Overseerr keeps 'true'),
    # we read the actual bitmask from notificationTypes['telegram'].
    notification_types = current_settings.get("notificationTypes", {})
    current_bitmask = notification_types.get("telegram", 0)

    # Flip the silent mode
    current_silent = current_settings.get("telegramSendSilently", False)
    new_silent = not current_silent

    # If current_bitmask == 0, the user effectively has Telegram disabled.
    # Toggling silent won't enable them, but we can still store the preference.
    chat_id = str(query.message.chat_id)

    success = update_telegram_settings_for_user(
        overseerr_telegram_user_id=overseerr_telegram_user_id,
        telegram_bitmask=current_bitmask,  # keep the same bitmask (0 = off, 3657 = on, etc.)
        chat_id=chat_id,
        send_silently=new_silent
    )

    if not success:
        await query.edit_message_text("❌ Failed to update silent mode in Overseerr.")
        return

    # Refresh the menu to display the new silent mode
    await show_manage_notifications_menu(query, context)

########################################################################
#                    /check COMMAND
########################################################################
async def check_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handles /check command to search for media.
    """
    telegram_user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    message_thread_id = getattr(update.message, "message_thread_id", None)
    logger.info(f"User {telegram_user_id} executed /check with args: {context.args} in chat {chat_id}, thread {message_thread_id}")

    config = load_config()

    # Check if command is allowed
    if not is_command_allowed(chat_id, message_thread_id, config, telegram_user_id):
        return

    if PASSWORD and not user_is_authorized(telegram_user_id):
        logger.info(f"User {telegram_user_id} is not authorized. Requesting password.")
        await send_message(context, chat_id, "👋 *Hey there!* Please enter the bot’s password to proceed:", message_thread_id=message_thread_id)
        context.user_data["awaiting_password"] = True
        return

    if "overseerr_telegram_user_id" not in context.user_data:
        logger.info(f"User {telegram_user_id} has no Overseerr user set.")
        mode_specific_msg = {
            BotMode.NORMAL: "Please log in with your Overseerr credentials in /settings.",
            BotMode.API: "Please select an Overseerr user in /settings.",
            BotMode.SHARED: "The admin needs to log in first. Use /settings if you’re the admin."
        }
        await send_message(
            context,
            chat_id,
            f"👤 *No user configured yet.*\n{mode_specific_msg.get(CURRENT_MODE, 'Please configure in /settings.')}",
            message_thread_id=message_thread_id
        )
        await show_settings_menu(update, context)
        return

    if not context.args:
        await send_message(
            context,
            chat_id,
            "🔍 *Search Media*\nPlease provide a title. Example: `/check Venom`",
            message_thread_id=message_thread_id
        )
        return

    media_name = " ".join(context.args)
    search_data = search_media(media_name)
    if not search_data:
        await send_message(
            context,
            chat_id,
            "❌ *Search Error*\nSomething went wrong. Please try again later.",
            message_thread_id=message_thread_id
        )
        return

    results = search_data.get("results", [])
    if not results:
        await send_message(
            context,
            chat_id,
            f"🔍 *No Results*\nNo media found for '{media_name}'. Try a different title!",
            message_thread_id=message_thread_id
        )
        return

    processed_results = process_search_results(results)
    context.user_data["search_results"] = processed_results

    sent_message = await display_results_with_buttons(update, context, processed_results, offset=0)
    context.user_data["results_message_id"] = sent_message.message_id

###############################################################################
#              DISPLAY RESULTS WITH BUTTONS (SEARCH PAGINATION)
###############################################################################
async def display_results_with_buttons(
    update_or_query,
    context: ContextTypes.DEFAULT_TYPE,
    results: list,
    offset: int,
    new_message: bool = False,
):
    """
    Creates inline buttons for up to 5 titles per page. Allows
    navigation with Back/More. Returns the message object (if any).
    """
    keyboard = []
    for idx, result in enumerate(results[offset : offset + 5]):
        year = result.get("year", "Unknown Year")
        button_text = f"{result['title']} ({year})"
        callback_data = f"select_{offset + idx}"
        keyboard.append([InlineKeyboardButton(button_text, callback_data=callback_data)])

    total_results = len(results)
    is_first_page = (offset == 0)
    is_last_page = (offset + 5 >= total_results)

    navigation_buttons = []
    if is_first_page:
        cancel_button = InlineKeyboardButton("❌ Cancel", callback_data="cancel_search")
        if total_results > 5:
            more_button = InlineKeyboardButton("➡️ More", callback_data=f"page_{offset + 5}")
            navigation_buttons = [cancel_button, more_button]
        else:
            navigation_buttons = [cancel_button]
    elif is_last_page:
        back_button = InlineKeyboardButton("⬅️ Back", callback_data=f"page_{offset - 5}")
        cancel_button = InlineKeyboardButton("❌ Cancel", callback_data="cancel_search")
        navigation_buttons = [back_button, cancel_button]
    else:
        back_button = InlineKeyboardButton("⬅️ Back", callback_data=f"page_{offset - 5}")
        x_button = InlineKeyboardButton("❌ Cancel", callback_data="cancel_search")
        more_button = InlineKeyboardButton("➡️ More", callback_data=f"page_{offset + 5}")
        navigation_buttons = [back_button, x_button, more_button]

    if navigation_buttons:
        keyboard.append(navigation_buttons)

    reply_markup = InlineKeyboardMarkup(keyboard)

    # Decide how to send/edit the message
    if new_message:
        sent_message = await context.bot.send_message(
            chat_id=update_or_query.message.chat_id,
            text="Please select a result:",
            reply_markup=reply_markup
        )
        return sent_message
    elif isinstance(update_or_query, Update) and update_or_query.message:
        sent_message = await update_or_query.message.reply_text(
            text="Please select a result:",
            reply_markup=reply_markup
        )
        return sent_message
    elif isinstance(update_or_query, CallbackQuery):
        await update_or_query.edit_message_text(
            text="Please select a result:",
            reply_markup=reply_markup
        )
        return
    else:
        sent_message = await context.bot.send_message(
            chat_id=update_or_query.message.chat_id,
            text="Please select a result:",
            reply_markup=reply_markup
        )
        return sent_message

###############################################################################
#        process_user_selection: SHOW MEDIA DETAILS + REQUEST/ISSUE BUTTON
###############################################################################
async def process_user_selection(
    update_or_query,
    context: ContextTypes.DEFAULT_TYPE,
    result: dict,
    edit_message: bool = False
):
    """
    Displays details about the selected media (poster, description, status).
    Shows buttons for 1080p, 4K, or both—depending on user permissions.
    """
    REQUESTED_STATUSES = [STATUS_PENDING, STATUS_PROCESSING, STATUS_PARTIALLY_AVAILABLE, STATUS_AVAILABLE]
    # Determine whether this was triggered by a CallbackQuery
    if isinstance(update_or_query, Update):
        query = update_or_query.callback_query
    else:
        query = update_or_query

    # Basic media info
    media_title = result.get("title", "Unknown Title")
    media_year = result.get("year", "????")
    poster = result.get("poster")
    description = result.get("description", "No description available")

    status_hd = result.get("status_hd", STATUS_UNKNOWN)
    status_4k = result.get("status_4k", STATUS_UNKNOWN)
    overseerr_media_id = result.get("overseerr_id")

    # Save the result for potential future actions (report issue, etc.)
    context.user_data["selected_result"] = result

    overseerr_telegram_user_id = context.user_data.get("overseerr_telegram_user_id")

    # Decide if the user can request 4K for this media_type
    user_has_4k_permission = False
    if overseerr_telegram_user_id:
        user_has_4k_permission = user_can_request_4k(overseerr_telegram_user_id, result.get("mediaType", ""))

    # Inline function to interpret the numeric status codes
    def interpret_status(code: int) -> str:
        if code == STATUS_AVAILABLE:
            return "Available ✅"
        elif code == STATUS_PROCESSING:
            return "Processing ⏳"
        elif code == STATUS_PARTIALLY_AVAILABLE:
            return "Partially available ⏳"
        elif code == STATUS_PENDING:
            return "Pending ⏳"
        else:
            # If not requested (STATUS_UNKNOWN), return an empty string.
            return ""

    # Helper to see if we can request a given resolution
    def can_request_resolution(code: int) -> bool:
        return code not in REQUESTED_STATUSES

    # Build the inline keyboard
    keyboard = []
    back_button = InlineKeyboardButton("⬅️ Back", callback_data="back_to_results")

    # Build request_buttons list
    request_buttons = []
    if can_request_resolution(status_hd):
        btn_1080p = InlineKeyboardButton("📥 1080p", callback_data=f"confirm_1080p_{result['id']}")
        request_buttons.append(btn_1080p)

    if user_has_4k_permission and can_request_resolution(status_4k):
        btn_4k = InlineKeyboardButton("📥 4K", callback_data=f"confirm_4k_{result['id']}")
        request_buttons.append(btn_4k)

    if user_has_4k_permission and can_request_resolution(status_hd) and can_request_resolution(status_4k):
        btn_both = InlineKeyboardButton("📥 Both", callback_data=f"confirm_both_{result['id']}")
        request_buttons.append(btn_both)

    # Adjust labels if exactly two buttons are present
    if len(request_buttons) == 1:
        new_buttons = []
        for btn in request_buttons:
            if "Request" not in btn.text:
                new_text = "📥 Request " + btn.text.lstrip("📥 ").strip()
            else:
                new_text = btn.text
            new_buttons.append(InlineKeyboardButton(new_text, callback_data=btn.callback_data))
        request_buttons = new_buttons

    if request_buttons:
        keyboard.append(request_buttons)


    # Show Report Issue if any resolution is pending/processing/partial/available
    def is_reportable(code: int) -> bool:
        return code in [STATUS_PENDING, STATUS_PROCESSING, STATUS_PARTIALLY_AVAILABLE, STATUS_AVAILABLE]

    if (is_reportable(status_hd) or is_reportable(status_4k)) and overseerr_media_id:
        report_button = InlineKeyboardButton("🛠 Report Issue", callback_data=f"report_{overseerr_media_id}")
        keyboard.append([report_button])

    keyboard.append([back_button])

    # Construct the main message text (with inline status interpretation)
    status_hd_str = interpret_status(status_hd)
    status_4k_str = interpret_status(status_4k)

    status_lines = []
    if status_hd_str:
        status_lines.append(f"• 1080p: {status_hd_str}")
    if status_4k_str:
        status_lines.append(f"• 4K: {status_4k_str}")

    if status_lines:
        status_block = "*Current status*:\n" + "\n".join(status_lines)
    else:
        status_block = ""

    media_heading = f"*{media_title} ({media_year})*"
    message_text = f"{media_heading}\n\n{description}\n\n{status_block}"

    # If we have an old results_message_id, delete it
    results_message_id = context.user_data.get("results_message_id")
    if results_message_id:
        try:
            await context.bot.delete_message(
                chat_id=query.message.chat_id,
                message_id=results_message_id
            )
            logger.info(f"Deleted previous results message {results_message_id}.")
        except Exception as e:
            logger.debug(f"Could not delete previous results message {results_message_id}: {e}")
        context.user_data.pop("results_message_id", None)

    reply_markup = InlineKeyboardMarkup(keyboard)

    # Finally send or edit the message (photo or text)
    # Use provided poster or default poster if none available
    media_preview_url = f"https://image.tmdb.org/t/p/w500{poster}" if poster else DEFAULT_POSTER_URL

    if edit_message:
        # If the original message is a photo, edit its caption, otherwise edit the text.
        if query.message.photo:
            await query.edit_message_caption(
                caption=message_text,
                parse_mode="Markdown",
                reply_markup=reply_markup
            )
            context.user_data["media_message_id"] = query.message.message_id
        else:
            await query.edit_message_text(
                text=message_text,
                parse_mode="Markdown",
                reply_markup=reply_markup
            )
            context.user_data["media_message_id"] = query.message.message_id
    else:
        sent_msg = await context.bot.send_photo(
            chat_id=query.message.chat_id,
            photo=media_preview_url,
            caption=message_text,
            parse_mode="Markdown",
            reply_markup=reply_markup
        )
        context.user_data["media_message_id"] = sent_msg.message_id

def user_can_request_4k(overseerr_telegram_user_id: int, media_type: str) -> bool:
    """
    Returns True if this user can request 4K for the specified media_type.
    """
    all_users = get_overseerr_users()
    user_info = next((u for u in all_users if u["id"] == overseerr_telegram_user_id), None)
    if not user_info:
        logger.warning(f"No user found with Overseerr ID {overseerr_telegram_user_id}")
        return False

    user_permissions = user_info.get("permissions", 0)

    # Grant all 4K permissions to admin users (permission value 2)
    if user_permissions == 2:
        return True

    # PERMISSION_4K_MOVIE: 2048 bit
    # PERMISSION_4K_TV = 4096 bit
    if media_type == "movie":
        return (user_permissions & PERMISSION_4K_MOVIE) == PERMISSION_4K_MOVIE
    elif media_type == "tv":
        return (user_permissions & PERMISSION_4K_TV) == PERMISSION_4K_TV
    else:
        return False

###############################################################################
#            cancel_search: CANCEL CURRENT SEARCH & CLEANUP
###############################################################################
async def cancel_search(query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE):
    """
    Cancels the current search and removes related messages or states.
    """
    logger.info(f"Search canceled by user {query.from_user.id}.")
    # Delete the current message
    await query.message.delete()

    # If we have a results_message_id, delete it as well
    results_message_id = context.user_data.get("results_message_id")
    if results_message_id:
        try:
            await context.bot.delete_message(
                chat_id=query.message.chat_id,
                message_id=results_message_id
            )
            logger.info(f"Deleted search results message {results_message_id}.")
        except Exception as e:
            # Not critical - sometimes the message is already deleted.
            logger.debug(f"Could not delete search results message {results_message_id}: {e}")
        context.user_data.pop("results_message_id", None)

    # Clear any saved search results from context
    context.user_data.pop("search_results", None)

    # Notify user
    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text=f"🔍 Search canceled. \n"
        f"💡 Type `/check <title>` for a new search\n",
        parse_mode="Markdown"
    )

ISSUE_TYPES = {
    1: "Video",
    2: "Audio",
    3: "Subtitle",
    4: "Other"
}

async def mode_select(query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE):
    """
    Displays a detailed mode selection menu with descriptions for each mode.
    """
    # Detailed mode descriptions with formatting and Emojis
    text = (
    "🔧 *Change Mode*\n\n"
    "🌟 *Normal Mode*\n"
    "Each user logs in with their own Overseerr credentials. Requests are made using individual session cookies. "
    "If a session expires, the bot tries to auto-login. If it fails, users must log in again.\n\n"

    "🔑 *API Mode*\n"
    "All requests are sent using the API key, so users **don't need to log in**. Instead, they can select a user from the list, and the bot will process requests as that user.\n"
    "Limitations:\n"
    "- All media requests are approved automatically.\n"
    "- Issue reports are sent under the admin’s account\n"
    "- No individual login credentials.\n\n"

    "👥 *Shared User Mode*\n"
    "A single Overseerr account is shared for all users. The admin logs in once, and all user requests are sent through this shared account. "
    "Normal users cannot change any settings.\n\n"

    "📖 See GitHub Wiki for details."
    )


    # Mode selection buttons (three-column layout)
    keyboard = [
        [
            InlineKeyboardButton("🌟 Normal", callback_data="activate_normal"),
            InlineKeyboardButton("🔑 API", callback_data="activate_api"),
            InlineKeyboardButton("👥 Shared", callback_data="activate_shared")
        ],
        [InlineKeyboardButton("🔙 Back", callback_data="back_to_settings")]
    ]

    await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))

###############################################################################
#   button_handler: PROCESSES ALL INLINE BUTTON CLICKS (search, confirm, etc.)
###############################################################################
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handles button callbacks from inline keyboards.
    """
    global CURRENT_MODE
    query = update.callback_query
    data = query.data
    telegram_user_id = query.from_user.id
    chat_id = query.message.chat_id
    message_thread_id = getattr(query.message, "message_thread_id", None)
    config = load_config()
    user_id_str = str(telegram_user_id)
    is_admin = config["users"].get(user_id_str, {}).get("is_admin", False)

    logger.info(f"User {telegram_user_id} pressed a button with callback data: {data} in chat {chat_id}, thread {message_thread_id}")

    # Check if button callback is allowed
    if not is_command_allowed(chat_id, message_thread_id, config, telegram_user_id):
        return

    if PASSWORD and not user_is_authorized(telegram_user_id):
        logger.info(f"User {telegram_user_id} is not authorized. Showing an error.")
        await query.edit_message_text(
            text="You need to be authorized. Please use /start and enter the password first."
        )
        return

    # Handle settings
    if data == "settings":
        await show_settings_menu(query, context, is_admin)
        return

    elif data == "cancel_settings":
        logger.info(f"User {telegram_user_id} canceled settings.")
        await query.edit_message_text(
            "⚙️ Settings closed. Use /start or /settings to return."
        )
        return

    elif data == "change_user":
        logger.info(f"User {telegram_user_id} wants to change Overseerr user.")
        await handle_change_user(query, context)
        return

    elif data == "manage_users":
        await show_user_management_menu(query, context)
        return

    elif data.startswith("users_page_"):
        offset = int(data.split("_")[2])
        await show_user_management_menu(query, context, offset=offset)
        return

    elif data.startswith("manage_user_"):
        telegram_id = data.split("_")[2]
        await manage_specific_user(query, context, telegram_id)
        return

    elif data.startswith("block_user_"):
        telegram_id = data.split("_")[2]
        if config["users"].get(telegram_id, {}).get("is_admin", False) and telegram_id == user_id_str:
            await query.edit_message_text("❌ Cannot block the main admin.")
            return
        config["users"][telegram_id]["is_blocked"] = True
        config["users"][telegram_id]["is_authorized"] = False
        save_config(config)
        await manage_specific_user(query, context, telegram_id)
        return

    elif data.startswith("unblock_user_"):
        telegram_id = data.split("_")[2]
        config["users"][telegram_id]["is_blocked"] = False
        config["users"][telegram_id]["is_authorized"] = True
        save_config(config)
        await manage_specific_user(query, context, telegram_id)
        return

    elif data.startswith("promote_user_"):
        telegram_id = data.split("_")[2]
        config["users"][telegram_id]["is_admin"] = True
        config["users"][telegram_id]["is_authorized"] = True
        config["users"][telegram_id]["is_blocked"] = False
        save_config(config)
        await manage_specific_user(query, context, telegram_id)
        return

    elif data.startswith("demote_user_"):
        telegram_id = data.split("_")[2]
        if config["users"].get(telegram_id, {}).get("is_admin", False) and telegram_id == user_id_str:
            await query.edit_message_text("❌ Cannot demote the main admin.")
            return
        config["users"][telegram_id]["is_admin"] = False
        save_config(config)
        await manage_specific_user(query, context, telegram_id)
        return

    elif data == "manage_notifications":
        logger.info(f"User {telegram_user_id} wants to manage notifications.")
        await show_manage_notifications_menu(query, context)
        return

    elif data == "toggle_user_notifications":
        logger.info(f"User {telegram_user_id} toggling their Telegram notifications.")
        await toggle_user_notifications(query, context)
        return

    elif data == "toggle_user_silent":
        logger.info(f"User {telegram_user_id} toggling silent mode.")
        await toggle_user_silent(query, context)
        return

    elif data == "create_user":
        logger.info(f"User {telegram_user_id} clicked 'Create New Overseerr User'.")
        context.user_data["creating_new_user"] = True
        context.user_data["new_user_data"] = {}
        context.user_data["create_user_message_id"] = query.message.message_id

        keyboard = [[InlineKeyboardButton("🔙 Cancel Creation", callback_data="cancel_user_creation")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            text=(
                "➕ *Create New User*\n\n"
                "Step 1: Please enter the user's email address."
            ),
            parse_mode="Markdown",
            reply_markup=reply_markup
        )
        return

    elif data == "cancel_user_creation":
        logger.info(f"User {telegram_user_id} canceled user creation.")
        context.user_data.pop("creating_new_user", None)
        context.user_data.pop("new_user_data", None)
        await show_user_management_menu(query, context)
        return

    elif data == "back_to_settings":
        logger.info(f"User {telegram_user_id} going back to settings.")
        await show_settings_menu(query, context, is_admin)
        return

    elif data == "toggle_group_mode":
        if not is_admin:
            await query.edit_message_text("Only admins can toggle Group Mode.")
            return
        config["group_mode"] = not config["group_mode"]
        if not config["group_mode"]:
            config["primary_chat_id"] = {"chat_id": None, "message_thread_id": None}
            logger.info("Group Mode disabled, reset primary_chat_id to null")
        save_config(config)
        logger.info(f"Group Mode set to {config['group_mode']} by user {telegram_user_id}")
        await show_settings_menu(query, context, is_admin=is_admin)
        return

    elif data == "login":
        logger.info(f"User {telegram_user_id} initiated login.")
        if CURRENT_MODE == BotMode.API:
            await query.edit_message_text("In API Mode, no login is required.")
            return
        if CURRENT_MODE == BotMode.SHARED and not is_admin:
            await query.edit_message_text("In Shared Mode, only the admin can log in.")
            return
        await start_login(query, context)
        return

    elif data == "logout":
        logger.info(f"User {telegram_user_id} initiated logout.")
        if CURRENT_MODE == BotMode.SHARED and not is_admin:
            await query.edit_message_text("In Shared Mode, only the admin can log out.")
            return
        context.user_data.pop("session_data", None)
        context.user_data.pop("overseerr_telegram_user_id", None)
        context.user_data.pop("overseerr_user_name", None)
        context.user_data.pop("all_users", None)
        if CURRENT_MODE == BotMode.NORMAL:
            sessions = load_user_sessions()
            sessions.pop(str(telegram_user_id), None)
            save_user_sessions(sessions)
        elif CURRENT_MODE == BotMode.SHARED and is_admin:
            context.application.bot_data.pop("shared_session", None)
            if os.path.exists(SHARED_SESSION_FILE):
                os.remove(SHARED_SESSION_FILE)
                logger.info("Cleared shared session file.")
        await query.edit_message_text("✅ Logged out!")
        await show_settings_menu(query, context, is_admin)
        return

    elif data == "mode_select" and is_admin:
        logger.info(f"Admin {telegram_user_id} accessing mode selection.")
        await mode_select(query, context)
        return

    elif data.startswith("activate_") and is_admin:
        mode = data.split("_")[1]
        config["mode"] = mode
        CURRENT_MODE = BotMode[mode.upper()]
        save_config(config)
        await show_settings_menu(query, context, is_admin)
        return

    # ---------------------------------------------------------
    # D) Search Pagination / Selection
    # ---------------------------------------------------------
    results = context.user_data.get("search_results", [])

    if data.startswith("page_"):
        offset = int(data.split("_")[1])
        logger.info(f"User {telegram_user_id} requested page offset {offset}.")
        await display_results_with_buttons(query, context, results, offset)
        return
    
    elif data == "cancel_user_selection":
        logger.info(f"User {telegram_user_id} canceled user selection.")
        await show_settings_menu(query, context, is_admin)
        return

    elif data.startswith("user_page_"):
        offset = int(data.split("_")[2])
        logger.info(f"User {telegram_user_id} requested user page offset {offset}.")
        await handle_change_user(query, context, offset=offset)
        return

    elif data.startswith("select_user_"):
        selected_telegram_user_id_str = data.replace("select_user_", "")
        all_users = get_overseerr_users()
        selected_user = next((u for u in all_users if str(u["id"]) == selected_telegram_user_id_str), None)
        if not selected_user:
            logger.info(f"User ID {selected_telegram_user_id_str} not found in Overseerr user list.")
            await query.edit_message_text("User not found. Please try again.")
            return

        display_name = (
            selected_user.get("displayName")
            or selected_user.get("username")
            or f"User {selected_telegram_user_id_str}"
        )

        context.user_data["overseerr_telegram_user_id"] = int(selected_telegram_user_id_str)
        context.user_data["overseerr_user_name"] = display_name

        # Fetch notification settings for the selected user
        current_settings = get_user_notification_settings(int(selected_telegram_user_id_str))
        
        # Check if Telegram notifications are enabled
        notification_types = current_settings.get("notificationTypes", {})
        telegram_bitmask = notification_types.get("telegram", 0)
        if telegram_bitmask == 0:  # Notifications are disabled
            chat_id = str(query.message.chat_id)
            success = update_telegram_settings_for_user(
                overseerr_telegram_user_id=int(selected_telegram_user_id_str),
                chat_id=chat_id,
                send_silently=current_settings.get("telegramSendSilently", False),
                telegram_bitmask=3657  # Enable all notifications
            )

        # Persist in JSON so it survives bot restarts
        save_user_selection(telegram_user_id, int(selected_telegram_user_id_str), display_name)

        await show_settings_menu(query, context, is_admin)
        return

    elif data.startswith("select_"):
        result_index = int(data.split("_")[1])
        if 0 <= result_index < len(results):
            selected_result = results[result_index]
            logger.info(f"User {telegram_user_id} selected index {result_index}: {selected_result['title']}")
            await process_user_selection(query, context, selected_result)
        else:
            logger.warning(f"Invalid search result index: {result_index}")
            await query.edit_message_text("Invalid selection. Please try again.")
        return

    elif data == "back_to_results":
        logger.info(f"User {telegram_user_id} going back to search results.")
        await query.message.delete()
        sent_message = await display_results_with_buttons(
            query, context, results, offset=0, new_message=True
        )
        context.user_data["results_message_id"] = sent_message.message_id
        return

    elif data == "cancel_search":
        logger.info(f"User {telegram_user_id} canceled the search.")
        await cancel_search(query, context)
        return

    # ---------------------------------------------------------
    # E) Handling Requests for 1080p, 4K, or Both
    # ---------------------------------------------------------
    elif data.startswith("confirm_"):
        media_id = int(data.split("_")[2])
        selected_result = next((r for r in results if r["id"] == media_id), None)
        if not selected_result:
            logger.warning(f"Media ID {media_id} not found in search results.")
            await query.edit_message_text("Unable to find this media. Please try again.")
            return

        session_cookie = None
        requested_by = None  # Default to None (excluded in Normal/Shared)

        if CURRENT_MODE == BotMode.NORMAL:
            if "session_data" not in context.user_data:
                await query.edit_message_text("Please log in first (/settings).")
                return
            session_cookie = context.user_data["session_data"]["cookie"]
            if not check_session_validity(session_cookie):
                await query.edit_message_text("⏳ Session expired, attempting to re-login...")
                email, password = base64.b64decode(context.user_data["session_data"]["credentials"]).decode().split(":")
                new_cookie = overseerr_login(email, password)
                if new_cookie:
                    context.user_data["session_data"]["cookie"] = new_cookie
                    sessions = load_user_sessions()
                    sessions[str(telegram_user_id)]["cookie"] = new_cookie
                    save_user_sessions(sessions)
                    await query.edit_message_text("✅ Successfully re-logged in!")
                else:
                    context.user_data.pop("session_data", None)
                    await query.edit_message_text("❌ Re-login failed. Please log in again.")
                    return
        elif CURRENT_MODE == BotMode.SHARED:
            shared_session = context.application.bot_data.get("shared_session")
            if not shared_session or not check_session_validity(shared_session["cookie"]):
                await query.edit_message_text("Shared session expired. Admin must re-login.")
                return
            session_cookie = shared_session["cookie"]
        elif CURRENT_MODE == BotMode.API:
            requested_by = context.user_data.get("overseerr_telegram_user_id", 1)  # Use selected user ID in API mode

        if data.startswith("confirm_1080p_"):
            success_1080p, message_1080p = request_media(
                media_id=media_id,
                media_type=selected_result["mediaType"],
                requested_by=requested_by,
                is4k=False,
                session_cookie=session_cookie
            )
            await send_request_status(query, selected_result['title'], success_1080p=success_1080p, message_1080p=message_1080p)
        elif data.startswith("confirm_4k_"):
            success_4k, message_4k = request_media(
                media_id=media_id,
                media_type=selected_result["mediaType"],
                requested_by=requested_by,
                is4k=True,
                session_cookie=session_cookie
            )
            await send_request_status(query, selected_result['title'], success_4k=success_4k, message_4k=message_4k)
        elif data.startswith("confirm_both_"):
            success_1080p, message_1080p = request_media(
                media_id=media_id,
                media_type=selected_result["mediaType"],
                requested_by=requested_by,
                is4k=False,
                session_cookie=session_cookie
            )
            success_4k, message_4k = request_media(
                media_id=media_id,
                media_type=selected_result["mediaType"],
                requested_by=requested_by,
                is4k=True,
                session_cookie=session_cookie
            )
            await send_request_status(query, selected_result['title'], success_1080p, message_1080p, success_4k, message_4k)
        return

    # ---------------------------------------------------------
    # F) Report Issue
    # ---------------------------------------------------------
    elif data.startswith("report_"):
        overseerr_media_id = int(data.split("_")[1])
        selected_result = next((r for r in results if r.get("overseerr_id") == overseerr_media_id), None)
        if selected_result:
            logger.info(
                f"User {telegram_user_id} wants to report an issue for {selected_result['title']} "
                f"(Overseerr ID {overseerr_media_id})."
            )
            context.user_data['selected_result'] = selected_result

            issue_buttons = [
                [InlineKeyboardButton(text=ISSUE_TYPES[1], callback_data=f"issue_type_{1}")],
                [InlineKeyboardButton(text=ISSUE_TYPES[2], callback_data=f"issue_type_{2}")],
                [InlineKeyboardButton(text=ISSUE_TYPES[3], callback_data=f"issue_type_{3}")],
                [InlineKeyboardButton(text=ISSUE_TYPES[4], callback_data=f"issue_type_{4}")],
                [InlineKeyboardButton(text="❌ Cancel", callback_data="cancel_issue")]
            ]
            reply_markup = InlineKeyboardMarkup(issue_buttons)

            await query.edit_message_caption(
                caption=f"🛠 *Report an Issue*\n\nSelect the issue type for *{selected_result['title']}*:",
                parse_mode="Markdown",
                reply_markup=reply_markup,
            )
        else:
            logger.warning(f"No matching search result found for Overseerr ID {overseerr_media_id}.")
            await query.edit_message_caption("Selected media not found. Please try again.")
        return

    elif data.startswith("issue_type_"):
        try:
            issue_type_id = int(data.split("_")[2])
        except (IndexError, ValueError):
            logger.warning(f"Invalid issue_type callback data: {data}")
            await query.edit_message_caption("Invalid issue type. Please start again.")
            return

        issue_type_name = ISSUE_TYPES.get(issue_type_id, "Other")
        context.user_data['reporting_issue'] = {
            'issue_type': issue_type_id,
            'issue_type_name': issue_type_name,
        }
        logger.info(f"User {telegram_user_id} selected issue type {issue_type_id} ({issue_type_name}).")

        cancel_button = InlineKeyboardButton("❌ Cancel", callback_data="cancel_issue")
        reply_markup = InlineKeyboardMarkup([[cancel_button]])

        selected_result = context.user_data.get('selected_result')
        if not selected_result:
            logger.warning("No selected_result found in context when choosing issue type.")
            await query.edit_message_caption("No media selected. Please try reporting again.")
            return

        issue_examples = {
            1: "- *The video freezes at 1h 10m, but audio continues.*\n"
               "- *The quality is very bad despite selecting HD/4K.*\n"
               "- *Episode 2, Season 5 is missing entirely.*",
            2: "- *Episode 3, Season 2 has no sound from minute 10.*\n"
               "- *The audio is out of sync by 3 seconds.*\n"
               "- *No sound at all in the movie after 45 minutes.*",
            3: "- *No English subtitles available for the movie.*\n"
               "- *Subtitles are completely out of sync.*\n"
               "- *Wrong subtitles are shown (Spanish instead of German).*",
            4: "- *Playback keeps buffering despite a stable connection.*\n"
               "- *The wrong version of the movie is playing.*\n"
               "- *Plex error when trying to watch.*"
        }

        example_text = issue_examples.get(issue_type_id, "- *Please describe the issue.*")
        prompt_message = (
            f"🛠 *Report an Issue*\n\n"
            f"You selected: *{issue_type_name}*\n\n"
            f"📋 *Describe the issue with {selected_result['title']}.*\n"
            "Example:\n"
            f"{example_text}\n\n"
            "Type your issue below:"
        )

        await query.edit_message_caption(
            caption=prompt_message,
            parse_mode="Markdown",
            reply_markup=reply_markup,
        )
        return

    elif data == "cancel_issue":
        logger.info(f"User {telegram_user_id} canceled the issue reporting process.")
        context.user_data.pop('reporting_issue', None)
        selected_result = context.user_data.get('selected_result')
        await process_user_selection(query, context, selected_result, edit_message=True)
        return

    # ---------------------------------------------------------
    # G) Fallback
    # ---------------------------------------------------------
    logger.warning(f"User {telegram_user_id} triggered unknown callback data: {data}")
    await query.edit_message_text(
        text="Invalid action or unknown callback data. Please try again.",
        parse_mode="Markdown"
    )

async def send_request_status(query, title, success_1080p=None, message_1080p=None, success_4k=None, message_4k=None):
    """
    Sends a formatted status message for a media request.
    Handles 1080p, 4K, or both in a unified way.
    """
    status_1080p = "✅ 1080p requested successfully" if success_1080p else f"❌ 1080p: {message_1080p}" if message_1080p else "❌ 1080p request failed"
    status_4k = "✅ 4K requested successfully" if success_4k else f"❌ 4K: {message_4k}" if message_4k else "❌ 4K request failed"

    msg = f"*Request Status for {title}:*\n"
    if success_1080p is not None:
        msg += f"• {status_1080p}\n"
    if success_4k is not None:
        msg += f"• {status_4k}\n"

    await query.edit_message_caption(msg.strip(), parse_mode="Markdown")


###############################################################################
#                HANDLE CHANGE USER FUNCTION
###############################################################################
async def handle_change_user(update_or_query, context: ContextTypes.DEFAULT_TYPE, is_initial=False, offset=0):
    """
    Handles the 'Change User' button click or direct call after password.
    Presents a paginated list of Overseerr users to select from, with efficient user list caching.
    """
    if isinstance(update_or_query, Update):
        telegram_user_id = update_or_query.effective_user.id
        chat_id = update_or_query.effective_chat.id
        message_thread_id = getattr(update_or_query.message, "message_thread_id", None)
    elif isinstance(update_or_query, CallbackQuery):
        telegram_user_id = update_or_query.from_user.id
        chat_id = update_or_query.message.chat_id
        message_thread_id = getattr(update_or_query.message, "message_thread_id", None)
    else:
        logger.error("Invalid argument type passed to handle_change_user")
        return

    config = load_config()

    # Check if action is allowed
    if not is_command_allowed(chat_id, message_thread_id, config, telegram_user_id):
        return

    logger.info(f"User {telegram_user_id} is attempting to change Overseerr user, is_initial={is_initial}, offset={offset}, chat {chat_id}, thread {message_thread_id}")

    if "all_users" not in context.user_data:
        user_list = get_overseerr_users()
        if not user_list:
            error_text = "❌ Could not fetch user list from Overseerr. Please try again later."
            await send_message(context, chat_id, error_text, message_thread_id=message_thread_id)
            return
        context.user_data["all_users"] = user_list.get("results", user_list) if isinstance(user_list, dict) else user_list

    users = context.user_data["all_users"]
    total_users = len(users)
    page_size = 9
    max_pages = (total_users + page_size - 1) // page_size

    offset = max(0, min(offset, (max_pages - 1) * page_size))
    current_users = users[offset:offset + page_size]

    keyboard = []
    for overseerr_user in current_users:
        uid = overseerr_user["id"]
        display_name = (
            overseerr_user.get("displayName")
            or overseerr_user.get("username")
            or f"User {uid}"
        )
        callback_data = f"select_user_{uid}"
        keyboard.append([InlineKeyboardButton(f"{display_name} (ID: {uid})", callback_data=callback_data)])

    navigation_buttons = []
    cancel_button = InlineKeyboardButton("❌ Cancel", callback_data="cancel_user_selection")
    navigation_buttons.append(cancel_button)

    if offset > 0:
        back_button = InlineKeyboardButton("⬅️ Back", callback_data=f"user_page_{offset - page_size}")
        navigation_buttons.append(back_button)

    if offset + page_size < total_users:
        more_button = InlineKeyboardButton("➡️ More", callback_data=f"user_page_{offset + page_size}")
        navigation_buttons.append(more_button)

    if navigation_buttons:
        keyboard.append(navigation_buttons)

    reply_markup = InlineKeyboardMarkup(keyboard)
    message_text = (
        "🔄 *Select an Overseerr User:*" if not is_initial
        else "🔄 *Let’s pick your Overseerr user:*"
    )

    if isinstance(update_or_query, Update):
        await send_message(context, chat_id, message_text, reply_markup=reply_markup, message_thread_id=message_thread_id)
    elif isinstance(update_or_query, CallbackQuery):
        await update_or_query.edit_message_text(message_text, parse_mode="Markdown", reply_markup=reply_markup)
        

###############################################################################
#                               MAIN ENTRY POINT
###############################################################################
def main():
    global CURRENT_MODE
    config = load_config()
    mode_from_config = config.get("mode", "normal")
    try:
        CURRENT_MODE = BotMode[mode_from_config.upper()]
    except KeyError:
        logger.warning(f"Invalid mode {mode_from_config} in config, defaulting to NORMAL")
        CURRENT_MODE = BotMode.NORMAL
    logger.info(f"Bot started in mode: {CURRENT_MODE.value}")

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    if CURRENT_MODE == BotMode.SHARED:
        shared_session = load_shared_session()
        if shared_session:
            app.bot_data["shared_session"] = shared_session
            logger.info("Loaded shared session for Shared mode")

    # Register handlers
    app.add_handler(MessageHandler(filters.ALL, user_data_loader), group=-999)
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("settings", show_settings_menu))
    app.add_handler(CommandHandler("check", check_media))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_input))

    logger.info("Starting bot polling...")
    app.run_polling()

if __name__ == "__main__":
    main()