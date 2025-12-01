#!/usr/bin/env python

import os
import argparse # get CMD arguments
import getpass  # get passwords securely
import json     # graphql processing
from datetime import datetime
from playwright.async_api import async_playwright
import aiohttp
import aiofiles
import asyncio
import tempfile
# from tqdm.asyncio import tqdm # if you don't want a colourful progress bar,
# uncomment this and comment out rainbow_tqdm,
# either should work as a drop-in replacement.
# Enable the rainbow effect for tqdm
# make progress bars colorful and somewhat more appealing to look at
from rainbow_tqdm import tqdm
import logging
import sys
import subprocess
import re

from typing import Any, Awaitable

# for decorating:
from functools import wraps

# Current script version (using Semantic Versioning).
# TODO: should this be here or part of UV's metadata ?
# Since I want to have autoupdate functionality, I must have it here too...?
# Take care to keep them in sync!
__version__ = "1.0.0"


#################### Constant definitions ####################
## NB: These are still passed as funcion arguments.
## TODO Figure out if I should just declare them as "global".
# I think, that passing them explicitly saves me, when debugging,
# from having implicit state all over the place.
## I avoid mutable global state. e.g. "I can change the config and the global
# state would change, which could break things in multiple places and make it
# hard to debug them". Although config validation should be fixing this, no?

# base path. Change it if you wish to use this script outside Docker.
_BASE_PATH = "/"
# e.g. for the home directory of the user:
# _BASE_PATH=os.environ['HOME']
_LOGS_PATH = os.path.join(_BASE_PATH, "logs")
_SAVE_DIR_PATH = os.path.join(os.getcwd(), "lecture_recordings")


# default config locations:
# .local/share would have been better potentially,
# but it's nice to have everything in one place:
_CONFIG_URLS_PATH = os.path.expanduser("~/.config/eth-videoz/urls")


#################### Reasonable Defaults ####################
# HD video
# en and de subtitles (if available)
# ogg (best compression) for audio-only recordings

# use "low" for testing, to speed things up
_VIDEO_QUALITY = "mid"  # low/mid/high
_SUBTITLES = [
    "en-US",
    "de-DE",
]  # None #["en-US"] #["en-US", "de-DE"]; to disable, set to None
_AUDIO_QUALITY = "ogg"  # m4a/mpeg/ogg


######################## UX ######################
global ux_clicking_message

ux_clicking_message = (
    "\n\n😮‍💨 Phew! So much clicking would make my fingers sweat, "
    "if only I had them 😅"
)
#################### functions ####################
def setup_arg_parser():
    """Parse command-line arguments"""
    parser = argparse.ArgumentParser()
    # optional positional argument
    parser.add_argument(
        "quick_add",
        nargs="?",  # makes it optional (0 or 1 values)
        default=None,  # or some meaningful default
        help=(
            'Optional "quick add" -- fetches videos from a series link and adds'
            " it to the history file in one go. Note: it makes no sense to "
            "download individual video links, as you can just do it yourself "
            "without a script"
        ),
    )
    # ./script                 # positional_string = None
    # ./script "some string"   # positional_string = "some string"
    # ./script "some string" --username 'user' --password-from-stdin
    # ./script --username 'user' --debug   # still works
    # NB: "some string" (positional argument) *must* appear before keyword args
    parser.add_argument(
        "--username",
        required=False,
        help=(
            'ETH username, e.g. "jdoe" (without quotes); otherwise the script '
            "asks for it interactively."
        ),
    )
    parser.add_argument(
        "--password-from-stdin",
        required=False,
        action="store_true",
        help=(
            "This flag does not take any arguments, it allows you to pipe ETH "
            "password from on the command line; otherwise the script would ask "
            "you for the password interactively. There are some caveats about "
            "how to pipe a password securely. Consult README if you are unsure!"
        ),
    )
    # which subtitles to get, e.g. "en-US", "all" or (default) None
    parser.add_argument(
        "--subtitles",
        required=False,
        default=_SUBTITLES,
        type=str,
        help=(
            "TODO: Subtitles language to download. "
            "Default: download all available subtitles. "
            "Possible values: 'en-US', 'de-DE', 'all' (default)"
        ),
    )
    # where to save files? current working directory by default
    # the directory in which you were when you started the script
    parser.add_argument(
        "--save-dir",
        required=False,
        default=_SAVE_DIR_PATH,
    )
    parser.add_argument(
        "--video-quality",
        required=False,
        default=_VIDEO_QUALITY,
    )
    parser.add_argument(
        "--audio-quality",
        required=False,
        default=_AUDIO_QUALITY,
    )
    parser.add_argument(
        "-d",
        "--debug",
        required=False,
        action="store_true",
        help=f"If enabled, prints debugging info and saves it to {_LOGS_PATH}",
    )
    return parser


# 0th global var: args?
# global args
# better use a closure for my purposes (esp. providing default values for the
# keyword arguments)
def use_args(func):
    """A decorator for functions which need access to command line arguments"""
    """Basically this is like LISP pre-advice"""
    # parser = setup_arg_parser()
    # cli_args = parser.parse_args()

    @wraps(func)
    async def wrapper(*args, **kwargs):
        try:
            parser = setup_arg_parser()
            kwargs["cli_args"] = parser.parse_args()
            result = await func(*args, **kwargs)
            return result
        except Exception as e:
            log.error(f"Scheiße! {e}")  # the catch-it-all, ideally never fires
            raise e

    return wrapper


# purely for UX:
# counting clicks in the asyncronous environment
# note how it uses the lock to ensure we never get a race condition,
# where counter goes:
# 100 -> 25 -> 4 -> 97 etc.
class SharedCounter:
    def __init__(self):
        self.value = 0
        self.lock = asyncio.Lock()

    async def increment(self, n=1):
        async with self.lock:
            self.value += n
            await self.display()

    @use_args
    async def display(self, cli_args=None):
        # hide UX stuff for debugging - it messes up logs
        if not cli_args.debug:
            sys.stdout.write(
                f"\rCounting clicks, so you don't have to 🫸{self.value}🫷"
            )
            sys.stdout.flush()


# 1st global var
counter = SharedCounter()

global user_agent
user_agent = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
)


#################### logging ####################
# 2nd global var
log = logging.getLogger(__name__)


def setup_logging(args):
    log.setLevel(logging.INFO)
    # print("DEBUG is set to ", args.debug)
    if args.debug:
        log.setLevel(logging.DEBUG)

    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.DEBUG)
    formatter = logging.Formatter(
        "%(asctime)s - %(levelname)s - %(funcName)s - %(message)s"
    )
    console_handler.setFormatter(formatter)
    log.addHandler(console_handler)

    # Save log to file if debug is set
    # File handler
    if args.debug:
        # Generate filename with ISO timestamp
        timestamp = datetime.now().isoformat(timespec="seconds")
        timestamp = timestamp.replace(":", "-")
        log_filename = os.path.join(_LOGS_PATH, f"{timestamp}.log")
        file_handler = logging.FileHandler(log_filename, mode="w")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(formatter)
        log.addHandler(file_handler)

    # Prevent logger from sending messages to the root logger
    # (which may have other handlers):
    # i.e. keep debug messages only for log.debug() in this file
    log.propagate = False

    # to see which loggers are active:
    # https://stackoverflow.com/a/36208664
    # print('::::::::::logging::::::::::')
    # for key in logging.Logger.manager.loggerDict:
    #     print(key)
    # exit()

    # https://stackoverflow.com/questions/11029717/how-do-i-disable-log-messages-from-the-requests-library
    logging.getLogger("asyncio").setLevel(logging.WARNING)


def get_repo_root():
    """
    Returns the root of the git repo, since the executable can be a symlink
    """
    # __file__ can be a symlink, so resolve it to the real path
    script_path = os.path.realpath(__file__)
    repo_dir = os.path.dirname(script_path)
    # Optionally climb up to the repo root if script is in a subdirectory
    # For most cases, running 'git rev-parse --show-toplevel' is safest:
    completed = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=repo_dir,
        text=True,
        capture_output=True,
    )
    log.debug(f"{script_path=}")
    log.debug(f"{repo_dir=}")
    log.debug(f"{completed=}")
    if completed.returncode == 0:
        return completed.stdout.strip()
    else:
        return repo_dir


async def updater():
    """
    Checks if a new version is available.
    If yes, prints release notes and prompts the user (y/n) to update.

    If yes, runs git pull.
    NOTE: this might need manual adjusting if used outside of Docker.
    TODO test this.
    """
    owner = "isakovai"
    repo = "eth-videoz"
    url = f"https://api.github.com/repos/{owner}/{repo}/releases/latest"
    issues_page = f"https://github.com/{owner}/{repo}/issues"

    try:
        # Use aiohttp for asynchronous HTTP request
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=10) as response:
                response.raise_for_status()
                data = await response.json()
                latest_version = data.get("tag_name")

        log.debug(f"Current version: {__version__}")
        log.debug(f"Latest version: {latest_version}")

        if latest_version and latest_version != __version__:
            print(
                f"New version ({latest_version}) is available. "
                f"Current version is {__version__}."
            )
            user_input = input(
                "📑 Would you like to see release notes for it? (Y/n): "
            ).lower()
            if user_input == "y" or user_input == "\n":
                pass  # get release notes from github

            user_input = input(
                "🤔 Would you like to update to the latest version? (Y/n): "
            ).lower()
            if user_input == "y" or user_input == "\n":
                print("🔽 Updating...")
                log.debug("🔽 Updating...")
                repo_root = get_repo_root()
                subprocess.run(["git", "pull"], cwd=repo_root)
                print("👍 Update complete.")
                log.debug("👍 Update complete.")
            else:
                log.debug(
                    "❌ Update canceled. Continuing with the old version... "
                    "Beware that things might break!"
                )
        else:
            log.debug(
                "👌 You are already using the latest version = "
                f"{latest_version}"
            )
    except Exception as e:
        log.error(
            f"😵 Error checking for updates: {e}\n"
            f"Please, report this issue on {issues_page}"
        )


async def extract_video_entries(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Extracts video entry metadata from JSON. Returns a list with entries."""
    entries = []
    try:
        # for now let's assume those keys always exist
        # (the structure might change in the future) on the serverside!
        blocks = data["realm"]["blocks"]
        # blocks = data.get("data", {}).get("realm", {}).get("blocks", [])
        # for optional / missing keys (2nd arg is the default value to be
        # returned in case the key is missing)
        for block in blocks:
            # We want series entries and playlist entries containing videos
            if block["__typename"] == "SeriesBlock" and block.get("series"):
                entries.extend(block["series"].get("entries", []))
            elif block["__typename"] == "PlaylistBlock" and block.get(
                "playlist"
            ):
                entries.extend(block["playlist"].get("entries", []))
            elif block["__typename"] == "VideoBlock" and block.get("event"):
                entries.append(block["event"])

        log.debug(f"Found {len(entries)} video entries")
        return entries

    except Exception as e:
        log.error(f"⚠️ Error parsing blocks: {e}")
        return None


async def get_session_cookies(context) -> dict[str, Any]:
    raw_cookies = await context.cookies()
    log.debug(f"🍪 {raw_cookies=}")
    return {cookie["name"]: cookie["value"] for cookie in raw_cookies}


async def fetch_graphql_data(
    graphql_url, graphql_query, headers, session_cookies
):
    """Send the request with the session cookie"""
    headers = headers.copy()
    headers["Cookie"] = "; ".join(
        [f"{k}={v}" for k, v in session_cookies.items()]
    )
    async with aiohttp.ClientSession() as session:
        # bypass cookie domain check (so it works just like
        # synchronous requests library)
        async with session.post(
            graphql_url, json=graphql_query, headers=headers
        ) as response:
            # async with session.post(graphql_url, json=graphql_query,
            # headers=headers, cookies=session_cookies) as response:
            # or await response.text() if you're expecting a txt format
            return await response.json()


async def intercept_graphql(page):
    # Intercept graphql response (json)
    # Set up a waiter before going to the video.ethz.ch page
    # Else can't capture graphql, because it gets loaded quicker
    graphql_waiter = asyncio.create_task(
        page.wait_for_event(
            "request",
            predicate=lambda request: "graphql" in request.url
            and request.method == "POST",
            timeout=30 * 1000,
        )
    )

    await page.goto("https://video.ethz.ch")

    graphql_request = await graphql_waiter
    log.debug(f"✅ Intercepted GraphQL request: {graphql_request.url}")
    log.debug(f"✅ Intercepted GraphQL request: {graphql_request}")

    # log.debug(graphql_request.post_data)
    log.debug(graphql_request.headers)

    # Save to file only if debugging
    if log.getEffectiveLevel() == logging.DEBUG:
        timestamp = datetime.now().isoformat(timespec="seconds")
        timestamp = timestamp.replace(":", "-")
        graphql_query_log_filename = os.path.join(
            _LOGS_PATH, f"{timestamp}-graphql_query.json"
        )

        with open(graphql_query_log_filename, "w") as json_file:
            json.dump(graphql_request.post_data, json_file, indent=4)

    return graphql_request


async def graphql_append_json_metadata(
    series_url, graphql_request, session_cookies
):
    # Get the JSON response list of video page urls for the specific course
    # by sending the graphql request with the session cookie

    # Returns dict {course_path: json_metadata}
    # note that course_path[0] == '/'
    course_path = series_url["url"].split("https://video.ethz.ch")[1]
    log.debug(f"{course_path=}")
    graphql_url = "https://video.ethz.ch/graphql"
    # Reuse response, but change the path to the course of interest
    graphql_query = json.loads(graphql_request.post_data)
    graphql_query["variables"]["path"] = course_path
    # log.debug(graphql_query)
    # page.pause()
    headers = {
        "User-Agent": user_agent,
        "Content-Type": "application/json",
    }

    data = await fetch_graphql_data(
        graphql_url, graphql_query, headers, session_cookies
    )
    log.debug(json.dumps(data, indent=2))
    series_url["graphql"] = data["data"]
    # make the title of a series easier to access:
    series_url["title"] = series_url["graphql"]["realm"]["blocks"][0]["series"][
        "title"
    ]
    return series_url


def make_safe_filename(title: str) -> str:
    """Create a filename safe accross OSs (Windows 👀 I'm looking at you)"""
    underscored = title.replace(" ", "_")
    safe = re.sub(r'[<>:"/\\|?*]', "", underscored)
    return safe


async def get_series_type(context, entry, series) -> str:
    """Determines the type of the series by looking at its first video page
    Input: context (with login session active), entry id and series dict
    Output: updated series dict, with added 'login_type' -> open/protected/eth
    """
    # base case: if there are no video ids, the video needs eth login:
    if entry["__typename"] == "NotAllowed":
        return "eth"

    # NB: if metadata exposes video ids i.e. 'ev.....', then these can either be
    # "protected" or openly accessible, and I need to differentiate between
    # these. The session cookies are kept in the context = shared by new pages

    # Open new page in the same context, where 'Agree' button has been clicked
    page = await context.new_page()
    await counter.increment()
    try:
        entry_id = entry["id"].split("ev", 1)[1]
        video_page_url = f"{series['url']}/v/{entry_id}"
        log.debug(f"➡️ Visiting video page: {video_page_url=}")
        await page.goto(video_page_url)
        await counter.increment()

        # if openly accessible, there must be a 'Download' button:
        download_button = page.locator("button:has-text('Download')")
        # if protected, there must be a 'Verify' button:
        verify_button = page.locator("button:has-text('Verify')")

        # vibe:
        async def wait_for_either_button():
            tasks = [
                asyncio.create_task(download_button.wait_for(state="visible")),
                asyncio.create_task(verify_button.wait_for(state="visible")),
            ]
            done, pending = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_COMPLETED
            )

            # Cancel the task(s) that didn't complete
            for task in pending:
                task.cancel()

        await wait_for_either_button()

        if await download_button.is_visible():
            return "open"
        elif await verify_button.is_visible():
            return "protected"
        else:
            return "none"

    finally:
        await page.close()
        await counter.increment()


# TODO: this needs to be refactored
# It works, but.. yeah..
async def process_entry(context,
                        entry,
                        series,
                        protected_without_eth_login=False):
    """Fetches media metadata from the respective video page
    Input: context (with login session active), entry id and course page url
    Output: media (media includes video / audio / subtitles) metadata (dict)
    """
    page = await context.new_page()  # open new page in the same context
    await counter.increment()
    try:
        # lstrip is unreliable (if real entry_id starts with ev, it would strip
        # it as well...) try 'evevevevabc'.lstrip('ev')
        entry_id = entry["id"].split("ev", 1)[1]
        video_title = entry["title"].strip("\"' ")
        log.debug(f'process_entry video title = "{video_title}"')
        series_title = entry["series"]["title"].strip("\"' ")
        video_page_url = f"{series['url']}/v/{entry_id}"
        log.debug(f"➡️ Visiting video page: {video_page_url=}")
        await page.goto(video_page_url)
        await counter.increment()
        # special handling, because we need to relogin for every new opened page
        if protected_without_eth_login:
            # await asyncio.sleep(2)

            log.debug(
                "👉 protected_without_eth_login: Trying to log in for "
                f"this video: {video_page_url}\n"
            )
            # this gets too messy:
            # print(
            #     "👉 protected_without_eth_login: Trying to log in for "
            #     f"this video: {video_page_url}\n"
            # )
            await login_protected(context, series, page)

        # element = await page.wait_for_selector("time", timeout=100000)
        # timestamp = await element.get_attribute("datetime")
        # Why scrape from the page, when already available in the metadata:
        timestamp = entry["created"]

        dt_timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        log.debug(f"📅 {dt_timestamp=}")

        await page.locator("button:has-text('Download')").click()

        download_list = page.locator(
            "div[role='dialog'] "
            "ul:has(a:text-matches('Video|Audio|Caption', 'i'))"
        )
        #     Video|Audio|Caption matches any of the three
        #     'i' makes it case-insensitive (optional)

        # download_list = page.locator("div[role='dialog']
        # ul:has(a:has-text('Video'))")
        element_handle = await download_list.element_handle()
        links = await element_handle.query_selector_all("a")

        # Scrape video source download links
        video_sources = {}
        # Scrape caption links
        subtitle_sources = {}
        # some content is available as audio-only:
        audio_sources = {}

        for link in links:
            href = await link.get_attribute("href")
            label = await link.text_content()
            label = label.strip()
            log.debug(label)
            if "Video" in label and href.endswith(".mp4"):
                if "640" in label:
                    quality = "low"
                elif "1280" in label:
                    quality = "mid"
                elif "1920" in label:
                    quality = "high"
                else:
                    quality = "unknown"
                video_sources[quality] = href
            elif "Caption" in label and href.endswith(".vtt"):
                subtitle_language = label.lstrip("Caption ").strip("()")
                subtitle_sources[subtitle_language] = href
            elif "Audio" in label:
                if href.endswith(".m4a"):
                    quality = "m4a"
                elif href.endswith(".mpeg"):
                    quality = "mpeg"
                elif href.endswith(".ogg"):  # is the best compression
                    quality = "ogg"
                else:
                    quality = "unknown"
                audio_sources[quality] = href
            # A little hack to download audio-only recordings
            # I've never seen Audio-only recordings before, this must have been
            # some kind of error - camera issue probably, or lecturer turning on
            # Audio-only. For example:
            # https://video.ethz.ch/lectures/d-infk/2024/autumn/263-0006-00L/v/E6lbjGGrzmi

        return {
            "id": entry_id,  # Only short ID (without 'ev')
            # UX: for better readability in filename
            # replace "T" with "__" (double underscore)
            "datetime": dt_timestamp.strftime("%Y-%m-%d__%H_%M"),
            # _%S'),
            # dt_timestamp.date().isoformat(),
            # date is not enough to keep order, if there are multiple recordings
            # during the same day, so added time too.
            "url": video_page_url,
            "title": video_title,
            "series_title": series_title,
            "video_sources": video_sources,
            "subtitle_sources": subtitle_sources,
            "audio_sources": audio_sources,
        }
    finally:
        await page.close()
        await counter.increment()


# TODO: might consider a decorator/closure @args for functions using args
async def login_eth(args, context):
    # DONE: I wonder if this would now save eth session in the "global" context,
    # or is this only local, so I'd need to return the new "updated" context?
    # ANSWER: This changes global context (browser maintains it)
    page = await context.new_page()
    await page.goto("https://video.ethz.ch")

    await page.locator("a[href*='~session']").click()
    await counter.increment()
    log.debug("✅ Clicked login link")

    await page.locator("#userIdPSelection_iddtext").click()
    await page.locator("#userIdPSelection_iddtext").fill("ETH Zurich")
    log.debug("✅ Filled 'ETH Zurich' in search box")

    eth_entry = page.locator("//div[@title='Universities: ETH Zurich']")
    if await eth_entry.is_visible():
        await eth_entry.click()
        await counter.increment()
    log.debug("✅ Selected 'ETH Zurich' from dropdown")

    # Wait for ETH login page and fill in credentials
    await page.wait_for_selector("input[name='j_username']", timeout=10000)

    # Note: Username and password are needed only once - to login
    # afterwards they are del-ed and the data should be eventually gc-ed.
    # Ask interactively, if not supplied with args.
    if args.username:
        USERNAME = args.username
    else:  # interactive
        print(
            "\n👉 Please, enter your ETH login credentials "
            "(stdin supported, see options).\n"
        )
        USERNAME = input("Username: ")
    if args.password_from_stdin:
        PASSWORD = sys.stdin.readline().strip()  # strips newline
    else:  # interactive
        PASSWORD = getpass.getpass()

    await page.fill("input[name='j_username']", USERNAME)
    await page.fill("input[name='j_password']", PASSWORD)
    log.debug(f"Here's current SSO page url: {page.url}")
    await page.press("input[name='j_password']", "Enter")
    await counter.increment()
    # Free username and password immediately after use, now that we have a
    # "browser session" running; the browser should deal with secure storage now
    del PASSWORD
    del USERNAME
    log.debug("👍 Submitted ETH login form")
    await page.wait_for_selector("button[title='User settings']")
    # log.debug("===================Awaited button successfully")
    # await page.pause()
    # await page.wait_for_url("https://video.ethz.ch/*")
    # await page.pause()
    # await asyncio.sleep(2)

    # Check final URL to confirm redirect back to video.ethz.ch
    if page.url.startswith("https://video.ethz.ch"):
        log.debug("👍 Redirect to https://video.ethz.ch was successful")
        return True
    else:
        log.debug(f"👎 Unexpected final URL: {page.url}")
        log.error(
            "👎 ETH login unsuccessful. Continuing... "
            "Videos requiring ETH login would NOT be downloaded."
        )
        return False


async def login_protected(context, series, page):
    """Performs a "protected video" login.
    Takes a page and series this page is related to and tries to login with
    series credentials which were parsed from the urls file, otherwise asks
    interactively.
    Rreturns a series object for which the login has worked.
    """
    log.debug(
        "Outside if. Trying to login using protected series credentials: "
        f"{series['username']=} {series['password']=}"
    )

    log.debug(
        f"👉 Trying to log in for this protected series: {series['title']}\n"
    )
    print(
        f"\n👉 Trying to log in for this protected series: {series['title']}\n"
    )
    try:
        if series["username"]:
            USERNAME = series["username"]
        else:  # interactive + cache or the relogin case
            USERNAME = input("Username: ")
            series["username"] = USERNAME
        if series["password"]:
            PASSWORD = series["password"]
        else:  # interactive + cache or the relogin case
            PASSWORD = getpass.getpass()
            series["password"] = PASSWORD

        # A very bad idea to print your password to the logs!
        # NB: DEBUG is meant *only* for development!
        log.debug(
            "Trying to login using protected series credentials: "
            f"{USERNAME=} {len(PASSWORD)=}"
        )

        await page.get_by_label("Identifier").fill(USERNAME)
        await page.get_by_label("Password").fill(PASSWORD)

        verify_btn = page.locator("button:has-text('Verify')")
        await verify_btn.click()
        await counter.increment()
        log.debug("👍 Login successful!\n")
        print("\n👍 Login successful!\n")

        del PASSWORD
        del USERNAME

        # logged in successfuly.The API needs to be refined
        # series["login"] = True
        # TODO: changing the attribute 'login' vs returning the series object
        # for which logged in successfully...
        # TODO figure out the design here.
        # DONE: I prefer returning an object, it's more explicit
        return series
    except Exception as e:
        log.error(
            "⚠️ Failed to log in to the following protected series: "
            f"{series['url']}. It would NOT be downloaded."
            f"Please, check your credentials and try again. Original eror: {e}"
        )


async def get_remote_file_size(url: str) -> int:
    """Returns the total file size in bytes for a given URL using HTTP HEAD
    request.
    Returns 0 if size cannot be determined.
    """
    log.debug(f"get remote filesize for url = {url}")
    headers = {
        "User-Agent": user_agent,
        "Range": "bytes=0-0",
    }

    async with aiohttp.ClientSession() as session:
        # async with session.head(url) as response:
        # the server doesn't support head requests, so need to send a GET
        async with session.get(url, headers=headers) as response:
            if response.status not in (200, 206):  # 206 = Partial Content
                response.raise_for_status()

            content_range = response.headers.get("Content-Range")
            log.debug(
                f"get_remote_file_size(): Content-Range: {content_range} "
                f"of url {url}"
            )

            if content_range:
                # Format: bytes 0-0/118086638
                total_size = content_range.split("/")[-1]
                return int(total_size)

            # Fallback: Try Content-Length (may not be reliable here)
            content_length = response.headers.get("Content-Length")
            if content_length:
                return int(content_length)

            return 0  # Unknown size


def prettyprint_convert_bytes_size(byte_size: int) -> str:
    """Convert bytes into the nearest appropriate unit (KB, MB, GB, etc.).
    To be used in tqdm for display purposes.
    """
    if byte_size < 1024:
        return f"{byte_size} B"
    elif byte_size < 1024**2:
        return f"{byte_size / 1024:.2f} KB"
    elif byte_size < 1024**3:
        return f"{byte_size / 1024**2:.2f} MB"
    elif byte_size < 1024**4:
        return f"{byte_size / 1024**3:.2f} GB"
    else:
        return f"{byte_size / 1024**4:.2f} TB"


async def download_file(url: str, abspath: str):
    """url      - what to download
       abspath  - where to put it (abspath = download_path/filename.extension)

    Supports resuming interrupted downloads
    NB: the server itself must support HTTP Range headers
    """
    log.debug(f"⬇️  Downloading: {url} to {abspath}")

    # Check if a file already exists and get its size
    start_byte = os.path.getsize(abspath) if os.path.exists(abspath) else 0

    headers = {}
    # TODO There is a certain type of bug which can arise here.
    # I think, it has already, but I couldn't reproduce it when testing,
    # maybe I'm wrong, need to retest:
    #
    # if the download process is interrupted for a couple of bits in the byte
    # (bit granularity) the video would just stop playing from the point where
    # this happened onward (until its end):
    # TODO needs further investigation, mid priority -- if you don't interrupt
    # the download, should work fine.
    # Possible fix: send a request for start_byte-1, compare the last byte of
    # the partial file to the start_byte-1, if same, continue, if not,
    # "complete" the chopped-off byte and proceed with downloading
    if start_byte > 0:
        headers["Range"] = f"bytes={start_byte}-"
        log.debug(f"Resuming from byte {start_byte}")

    log.debug("⬇️  opening new aiohttp session")
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as response:
            log.debug(
                f"✅ got a response and headers={response.headers} and "
                f"status ={response.status}"
            )
            # total_size = int(response.headers.get('Content-Length', 0))
            # + start_byte
            total_size = int(response.headers.get("Content-Length", 0))

            if start_byte >= total_size:
                # print(f"✅ File already downloaded: {abspath.split('/')[-1]}")
                log.debug(f"✅ File already downloaded: {abspath}")
                return

            if response.status == 416:
                log.warning(
                    "🟡 File already fully downloaded or invalid range: "
                    f"{abspath}"
                )
                return  # Already fully downloaded or bad range

            if response.status not in (200, 206):
                response.raise_for_status()  # Raise error for other statuses

            if "Content-Range" in response.headers:
                log.debug("https://video.ethz.ch supports partial downloads 🎉")
                # This is a resumed download — adjust total size
                content_range = response.headers["Content-Range"]
                total_size = int(content_range.split("/")[-1])
                log.debug(f"{total_size=}")

            chunk_size = 1024

            response.raise_for_status()

            # tqdm progress bar
            desc_width = 40  # prefix (filename) width
            progress = tqdm(
                total=total_size,  # total file size (remote)
                initial=start_byte,  # how many bytes already downloaded
                unit="iB",
                unit_scale=True,
                # desc=abspath.split("/")[-1],
                # abspath, # is too long, shorten it to be only the filename
                # shorten even more -- set same width for all for visual appeal
                # desc = (abspath.split("/")[-1][:desc_width].ljust(desc_width))
                # Show file extension first
                # helps to see if it is a video or a sub file being downloaded
                desc=(
                    "".join(
                        [
                            abspath.split("/")[-1].split(".")[-1],
                            " ",
                            abspath.split("/")[-1],
                        ]
                    )[:desc_width].ljust(desc_width)
                ),
                unit_divisor=1024,
                dynamic_ncols=True,  # Let tqdm resize dynamically
                # leave=False        # Optional: don't leave bar after done
            )

            # Use 'ab' (append bytes) if resuming, otherwise 'wb'
            mode = "ab" if start_byte > 0 else "wb"
            async with aiofiles.open(abspath, mode) as f:
                async for chunk in response.content.iter_chunked(chunk_size):
                    await f.write(chunk)
                    progress.update(len(chunk))

            progress.close()
            # doesn't look as nice. TODO Improve UX?
            # print(f"DONE: {abspath.split("/")[-1]}")


@use_args
async def download_video_subtitles_and_maybe_audio(
    media_metadata: dict[
        str, Any
    ],  # from typing import Any value can be Any TODO what is the exact type?
    #
    # This won't work, because default parameter values are evaluated at
    # *definition* time and not at the *run* time
    #
    # download_path: list[str] = cli_args.save_dir,
    # video_quality: list[str] = cli_args.video_quality,
    # subtitles: list[str] = cli_args.subtitles,
    # audio_quality: list[str] = cli_args.audio_quality,
    # get_size: bool = False,  # needs refactoring TODO
    #
    # using None pattern:
    cli_args=None,  # gets initialized by @use_args
    download_path: str | None = None,
    video_quality: str | None = None,
    subtitles: list[str] | None = None,
    audio_quality: str | None = None,
    get_size: bool = False,  # needs refactoring TODO
):
    """Download videos in original format and the subtitles.
    If video is not available, tries to download audio before giving up
    (TODO: packing mp4 video + vtt subtitle into mkv if subtitles are enabled"""

    if download_path is None:
        download_path = cli_args.save_dir
    if video_quality is None:
        video_quality = cli_args.video_quality
    if subtitles is None:
        subtitles = cli_args.subtitles
    if audio_quality is None:
        audio_quality = cli_args.audio_quality

    media_url = media_metadata.get("video_sources", {}).get(video_quality)
    if not media_metadata.get("video_sources"):  # if empty, try getting audio
        try:
            media_url = media_metadata["audio_sources"][audio_quality]
        except Exception as e:
            log.error(
                "⚠️ Error fetching audio. No appropriate sources?"
                f"(continuing with the download): {e}"
            )
    media_extension = media_url.rsplit(".", 1)[1]
    log.debug(f"{media_metadata['title']=}")
    # media_filename = make_safe_filename(f'{media_metadata["datetime"]}--
    # {media_metadata["title"]}--{media_metadata["id"]}.{media_extension}')
    # UX: prefer underscores (they provide more space in non-monospace fonts -
    # making it easier for eyes to read the filename)
    media_filename = make_safe_filename(
        f"{media_metadata['datetime']}__{media_metadata['title']}__"
        f"{media_metadata['id']}.{media_extension}"
    )
    log.debug(f"{media_filename=}")
    # parent_dir = <year>_<course_name>
    media_parent_dir = "".join(
        [
            str(
                datetime.strptime(
                    media_metadata["datetime"], "%Y-%m-%d__%H_%M"
                ).year
            ),
            "_",
            make_safe_filename(media_metadata["series_title"]),
        ]
    )  # = series name
    log.debug(f"{media_parent_dir=}")
    download_path_with_parent = os.path.join(download_path, media_parent_dir)
    media_abspath = os.path.join(download_path_with_parent, media_filename)

    filesize = 0
    if get_size:  # yes this is a bit ugly. Looking forward to refactoring
        filesize = await get_remote_file_size(media_url)
        log.debug(f"👌 {filesize=}")
    else:
        # Create parent directories if they don't exist
        # path = os.path.dirname(abspath)
        # log.debug(f"{path =}")
        # THIS IS AWFUL -> DON'T EVER DO IS AGAIN:
        # await aiofiles.os.makedirs(os.path.join(download_path,
        # media_parent_dir), exist_ok=True)
        # it wasn't creating the dir, and was just hanging...
        # aparently due to its 'asyncronous nature'..
        log.debug(f"mkdir {os.path.join(download_path, media_parent_dir)}")
        # os.makedirs() runs *synchronously* — it completes before continuing:
        os.makedirs(download_path_with_parent, exist_ok=True)
        log.debug(f"made dir {download_path_with_parent}")

        # log.debug(f"{os.path.join(download_path, media_parent_dir)=}")
        await download_file(media_url, media_abspath)

    # DON'T UNCOMMENT -> log.debug(f"👌 {subtitles=}")
    # this is the stupidest bug -> log.debug(None) would go crazy
    # if subtitles: # if not None (because None is false)
    if subtitles:
        for subtitle in subtitles:  # subtitles is a list
            log.debug(f"👌 for subtitle in subtitles: {subtitle=}")
            try:
                subtitle_url = media_metadata["subtitle_sources"][subtitle]
            except KeyError:
                log.debug(
                    f"No subtitle language {subtitle} found. Proceeding..."
                )
                continue

            log.debug("👌 after try - except")

            subtitle_extension = subtitle_url.rsplit(".", 1)[1]
            # name subtitle same as video (good for videoplayer detection),
            # with a different extension:
            subtitle_filename = media_filename.replace(
                media_extension, subtitle_extension
            )
            subtitle_abspath = os.path.join(
                download_path_with_parent, subtitle_filename
            )

            # TODO it is a bit ugly to have a variable which changes the
            # behaviour of the download function to only output filesize..
            # TODO refactor -- a second function? --> code duplication... IDK..
            if get_size:
                filesize += await get_remote_file_size(media_url)
            else:
                await download_file(subtitle_url, subtitle_abspath)
    if get_size:
        log.debug(f"👌 total filesize {filesize=}\n returning")
        return filesize


async def download_protected_videos(context, protected_series_logged_in,
                                    protected_without_eth_login=False):
    # print(f"🦔 Trying to get {len(protected_series)} protected series.")
    for series in protected_series_logged_in:
        entries = await extract_video_entries(series["graphql"])
        log.debug(f"This many videos would be fetched: {len(entries)}")
        tasks = [
            process_entry(context, entry, series, protected_without_eth_login) for entry in entries
        ]
        series["videos_data"] = await gather_with_concurrency(
            10, *tasks
        )
        log.debug(f"{series['videos_data']}")
        # UX:
        print(ux_clicking_message)

        filesizes = await gather_with_concurrency(
            10,
            *(
                download_video_subtitles_and_maybe_audio(
                    video_metadata,
                    get_size=True,
                )
                for video_metadata in series["videos_data"]
            ),
        )
        log.debug(f"{filesizes=}")

        pretty_sum_filesize = prettyprint_convert_bytes_size(
            sum(filesizes)
        )
        if len(entries) == 1:
            print(
                "👉 Now I have the link. Let me try to download this "
                f"video ({pretty_sum_filesize}) to your computer.\n"
            )
        else:
            print(
                "👉 I have all the links now. Let me try to download "
                f"{len(entries)} videos "
                f"({pretty_sum_filesize}) to your computer.\n"
            )
        # await page.pause()
        await gather_with_concurrency(
            5,
            *(
                download_video_subtitles_and_maybe_audio(video_metadata)
                for video_metadata in series["videos_data"]
            ),
        )
    if protected_series_logged_in: # if there was anything to loop through at all
        log.debug("Done downloading protected videos.")
        print("\n\n👍 Done downloading protected videos.\n\n")


################### Utility functions ###################
async def gather_with_concurrency(n: int, *coros: Awaitable[Any]) -> list[Any]:
    """
    Gather with Semaphore: avoid having too many simultaneous open connections

    Args:
        n (int): Maximum number of coroutines to run concurrently.
        *coros: Any number of awaitable coroutine objects.

    Returns:
        list[Any]: A list of results or exceptions from the gathered coroutines.
    """
    semaphore = asyncio.Semaphore(n)

    async def sem_coro(coro: Awaitable[Any]) -> Any:
        async with semaphore:
            return await coro

    return await asyncio.gather(
        *(sem_coro(c) for c in coros), return_exceptions=True
    )


async def get_urls(
    args: argparse.Namespace, urls_path: str
) -> list[dict[str, str | None]]:
    """Get urls from the `urls` file.
    The path to it is either provided via --urls cli option or is searched for
    in the default locations:
    first in the current directory `.`
    then in `~/.config/eth-videoz/`
    """

    if hasattr(args, "urls") and os.path.isfile(args.urls):
        filepath = args.urls

    elif os.path.isfile("urls"):  # Check default locations: current directory
        filepath = "urls"
    elif os.path.isfile(urls_path):  # Then check ~/.config/
        filepath = urls_path
    else:
        current_dir = os.getcwd()
        raise FileNotFoundError(
            f"URL file 'urls' was neither supplied as a command line argument, "
            f"nor found in the current directory = {current_dir}, nor in "
            "~/.config/eth-videoz/\n"
            f"Make sure the file exists and try again.\n"
        )

    urls = []
    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                # Skip empty or comment lines
                continue

            parts = line.split()
            if len(parts) == 1:
                url = parts[0]
                username = password = None
            elif len(parts) == 3:
                url, username, password = parts
            else:
                raise ValueError(
                    f"Invalid line format: {line}\n"
                    "If using username and password, *both* must be present on "
                    "the line following the protected url, space-delimited:\n"
                    "url <space> username <space> password"
                )

            urls.append(
                {
                    "url": url,
                    "username": username if len(parts) == 3 else None,
                    "password": password if len(parts) == 3 else None,
                }
            )

    if not urls and not args.quick_add:  # == None
        # now it is fine for it to be empty, if we are using quick_add
        current_dir = os.getcwd()
        log.error(
            "The urls file seems to be empty or urls were not retrieved "
            "properly from it. Please, check your urls file either at "
            f"{current_dir}/urls or at ~/.config/eth-videoz/urls"
        )

    return urls


async def main(args: argparse.Namespace):
    global user_agent

    # check if the urls file exists, if not, create it.
    # add the 3 clicks trick to its first line as a comment,
    # decrement it until it is 0, then remove it from the file.

    starme_message = (
        "Please, star 🌟 the project on Github: "
        "https://github.com/isakovai/eth-videoz\n"
        "Seeing it being useful to others would motivate me to maintain"
        " it 🤩\n\n"
        "█████████████████████████████████████\n"
        "████ ▄▄▄▄▄ █▄▀▀▄▄█▄█▀▀ ▀██ ▄▄▄▄▄ ████\n"
        "████ █   █ ███▄█  ▄▄▀▀█▄▀█ █   █ ████\n"
        "████ █▄▄▄█ ██▄▀▄▀ ██ █▄▄▀█ █▄▄▄█ ████\n"
        "████▄▄▄▄▄▄▄█ █ ▀▄▀ █ █▄█ █▄▄▄▄▄▄▄████\n"
        "████▄▄█▄ █▄█▀ ▄▄▀ ▀ ▄▄█ ▄▀▄▀▀█▀▀▄████\n"
        "████▄▄▀▀ ▄▄ ▄▀  ▀ █▄ ▀▀██▀▀ ▄▀▄█▀████\n"
        "████▄▀██▀ ▄█▀▄█▄▄ ▀▀ █▀█▄█  ▀▀▀▀ ████\n"
        "████▄▄█ ▄ ▄▄  ▄ ▄▄█ ▄█▄ ▄▄▄▀ █ █ ████\n"
        "████▀█▄▀▀▀▄█ ▄▀▀▀▀  ▄█▀ ▄▄██▀▄▀▀█████\n"
        "████  ████▄ ███▄▀█▀▄▀▄▄ ██▀█▄ ▀█▄████\n"
        "████▄▄██▄▄▄█▀ ▄█▄▄ ▀   █ ▄▄▄  ▄▄█████\n"
        "████ ▄▄▄▄▄ ██ ▄█▄█ ▄▀▀█  █▄█ ▄███████\n"
        "████ █   █ █  ▀█▀▄▄▀ █▀▄▄ ▄▄▄▄██ ████\n"
        "████ █▄▄▄█ █▀▄▀█▀▀ ▄▄███▄▄▀ ▄▀ ▄ ████\n"
        "████▄▄▄▄▄▄▄█▄▄▄▄▄██▄▄██▄▄█▄█▄████████\n"
        "█████████████████████████████████████\n\n"
        "I appreciate your support very much!\n\n"
        "This notice will be deactivated after you have run the program"
        " 3 times.\n"
        "See the docs to turn it off immediately.\n"
    )

    if hasattr(args, "urls"):
        filepath = args.urls
    else:
        filepath = _CONFIG_URLS_PATH

    if not os.path.isfile(filepath):
        print(starme_message)
        starme = "### starme = 2\n"  # counting the initial invocation, so 3-1
        dir_path = os.path.dirname(filepath)
        os.makedirs(dir_path, exist_ok=True)
        try:
            async with aiofiles.open(filepath, "w") as f:
                await f.write(starme)
        except Exception as e:
            log.error(
                "🙄 Failed to create initial urls file, you might have to "
                "create it yourself, or file an issue on Github. Sorry..."
                f" Original error: {e}"
            )
            exit()
    else:
        # subtract the star notice counter.
        # turns out there is no easy way to change the first line,
        # I still need to read the original
        # (can be done fast using chunks, after the first line)
        async with aiofiles.open(filepath, "r") as f:
            starme = await f.readline()

        if starme.startswith("### starme = "):
            starme_counter = starme.strip().split("### starme = ")[1]

            if starme_counter.isdigit():
                try:
                    print(starme_message)

                    # create a temporary file for overwriting
                    dir_path = os.path.dirname(filepath) or "."
                    fd, tmp_path = tempfile.mkstemp(dir=dir_path)
                    os.close(fd)

                    starme_counter = int(starme_counter)

                    if starme_counter != 0:
                        starme_counter -= 1

                    async with (
                        aiofiles.open(filepath, "r") as src,
                        aiofiles.open(tmp_path, "w") as dst,
                    ):
                        if starme_counter != 0:
                            # update the first line
                            await dst.write(f"### starme = {starme_counter}\n")
                        # else: skip, effectively removing it

                        # skip the original first line
                        await src.readline()

                        # copy the rest in chunks
                        while True:
                            chunk = await src.read(8192)
                            if not chunk:
                                break
                            await dst.write(chunk)
                    os.rename(tmp_path, filepath)
                except Exception as e:
                    log.error(
                        "🙄 Failed updating initial urls file... Sorry... "
                        f"Original error: {e}"
                    )

    log.debug("Started main")
    log.debug("Starting updater")
    # await updater()
    log.debug("Finished checking for updates")
    # UX:
    print(
        "\nLean back and relax, while I do all the clicking for you 🙌 🙌 🙌\n"
    )

    # connectivity check:
    url = "https://video.ethz.ch/"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=5) as response:
                if response.status != 200:
                    response.raise_for_status()
    except aiohttp.ClientError as e:
        log.error(
            f"⚠️ Error contacting {url}. Please, check your Internet connection"
            f" and try again. Original error: {e}"
        )
        exit()
    except asyncio.TimeoutError as e:
        log.error(
            f"⚠️ Error contacting {url}. Please, check your Internet connection"
            f" and try again. Original error: {e}"
        )
        exit()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        #browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=user_agent,
            viewport={"width": 1920, "height": 1080},
            locale="en-US",
        )

        page = await context.new_page()
        await counter.increment()

        # Step 1: Go to the main page
        await page.goto("https://video.ethz.ch/")
        await counter.increment()
        log.debug("✅ Opened https://video.ethz.ch/")

        # Step 2: Click "Agree" on the cookie popup if present
        agree_button = page.locator("button:has-text('Agree')")
        if await agree_button.is_visible():
            await agree_button.click()
            await counter.increment()
            log.debug("✅ Clicked 'Agree'")
        else:
            log.error(
                "⚠️ Couldn't find 'Agree' button, maybe already accepted. "
                "Trying to proceed..."
            )

        # Not all videos require logging in, so, if any, download openly
        # accessible videos first, without asking for usename / password
        series_urls = await get_urls(args, _CONFIG_URLS_PATH)
        log.debug(f"{series_urls=}")
        # the difference between path and url:
        # path is stripped of prefix 'https://video.ethz.ch'

        # Quickly add a series via a positional argument to the urls file,
        # if it isn't yet int there:
        # Only works for non-protected series, however,
        # TODO can add them in the future with the syntax: url:username:password
        quick_add = {"url": args.quick_add, "username": None, "password": None}
        if quick_add["url"]:  # not None
            # check this is a valid link (simple, but sufficient)
            # note the importance of ending '/', else this is possible:
            if not quick_add["url"].startswith("https://video.ethz.ch/"):
                # 'https://video.ethz.chrootingyourass.xyz/lectures/d-infk/2025/autumn/263-0006-00L'.startswith('https://video.ethz.ch')
                log.error(
                    f"⚠️ Error fetching the series {quick_add['url']}. The link"
                    " must start with 'https://video.ethz.ch'."
                    " Not downloading. Please, try again."
                )
                exit()
            # check if a given page exists at all by making a request to it:
            # async def check_page_status(url):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(quick_add["url"]) as response:
                        if not response.status == 200:
                            log.error(
                                f"⚠️ The page {quick_add['url']} returned"
                                f"status {response.status}. Please, check that "
                                "you can open it in your browser. "
                                "Not downloading. Please, try again."
                            )
                            exit()
            except aiohttp.ClientError as e:
                log.error(f"Request failed: {e}")
                exit()

            # Add to the url file if not already part of it
            if quick_add not in series_urls:
                # Get the title of the series to put as a comment into the urls
                session_cookies = await get_session_cookies(context)
                graphql_request = await intercept_graphql(page)
                quick_add_metadata = await graphql_append_json_metadata(
                    quick_add, graphql_request, session_cookies
                )

                # Open the urls file in append mode ('a').
                # Check all possible locations of it(copy-paste from get_urls())
                if os.path.isfile("urls"):
                    filepath = "urls"
                elif os.path.isfile(_CONFIG_URLS_PATH):
                    filepath = _CONFIG_URLS_PATH
                else:
                    raise FileNotFoundError(
                        f"URL file 'urls' was neither supplied as a command "
                        "line argument, nor found in the current directory ="
                        f"{os.getcwd()}, nor in ~/.config/eth-videoz/"
                    )

                # print(f"{quick_add = }")
                # can also extract from url, but this would be unreliable
                # for recordings of events, better leave it be as is.
                # After all, it's just a comment in the urls file!
                # entries = await extract_video_entries(quick_add["graphql"])
                # print(f"{entries=}")
                # entry_timestamp = entries[0]["created"]
                # timestamp = datetime.fromisoformat(
                # entry_timestamp.replace("Z", "+00:00"))

                async with aiofiles.open(filepath, "a") as f:
                    await f.write(
                        "".join(
                            [
                                "\n# ",
                                # requires login to get year... not viable
                                # timestamp.year,
                                # "_",
                                quick_add_metadata["title"],
                                "\n",
                            ]
                        )
                    )
                    await f.write("".join([quick_add_metadata["url"], "\n"]))

                # Update series_urls dict to contain the newly added series
                # by rereading the urls file
                series_urls = await get_urls(args, _CONFIG_URLS_PATH)
                log.debug(f"{series_urls=}")
            else:
                log.info(
                    f"\n⚠️ The series {quick_add['url']} is already in the urls"
                    f" file at {_CONFIG_URLS_PATH}. "
                    "Proceeding with the download as usual."
                )

        # download videos for series which don't require login,
        # TODO: save download history
        # get cookies (there was no login, hence no login cookie, but cookies
        # are (TODO) still required(?) for making a valid request
        session_cookies = await get_session_cookies(context)
        graphql_request = await intercept_graphql(page)
        tasks = []
        for series_url in series_urls:
            tasks.append(
                graphql_append_json_metadata(
                    series_url, graphql_request, session_cookies
                )
            )
        # series dict + graphql metadata
        series_videos_data = await gather_with_concurrency(10, *tasks)
        log.debug(f"series with the metadata {series_videos_data=} ")

        # check what kind of series we're dealing with:
        # open access / protected / ETH login
        # assumption:
        # "type" is set per-series by the uploaders and not per-video,
        # = no 'open access' and 'protected' videos are intermixed in one series

        # look at the first video page of every series to determine its type:
        for series in series_videos_data:
            entries = await extract_video_entries(series["graphql"])
            if not entries:  # if empty empty
                log.error(
                    f"The series {series=} seems to have no videos in it!"
                    "Exiting. Consider submitting an issue on Github."
                )
                exit()
            # log.debug(f'{entries=}')
            first_entry = entries[0]
            # log.debug(f'{first_entry=}')

            # kind of like process_entry():
            series["login_type"] = await get_series_type(
                context, first_entry, series
            )
            log.debug(f"{series['url']=} -> {series['login_type']=}")

        # Order of download:
        # get open series first
        # then protected
        # then eth login
        open_series, protected_series, eth_series = [], [], []
        # open_series = [x for x in series_videos_data if (x['login_type']
        #                                                  == 'open'])
        for x in series_videos_data:
            if x["login_type"] == "open":
                open_series.append(x)
            if x["login_type"] == "protected":
                protected_series.append(x)
            else:
                eth_series.append(x)

        # Fetch open series first, before the user has to supply any login
        # credentials for eth / protected series
        # if open_series:
        #     print("👉 Fetching videos with open access first.\n")
        for series in open_series:
            entries = await extract_video_entries(series["graphql"])
            log.debug(f"This many videos would be fetched: {len(entries)}")

            tasks = [process_entry(context, entry, series) for entry in entries]
            series["videos_data"] = await gather_with_concurrency(10, *tasks)
            log.debug(f"{series['videos_data']}")
            # UX:
            print(ux_clicking_message)
            # Get exact total file size of the downloads
            # (ugly for now to put this in one func, would refacotr later):
            filesizes = await gather_with_concurrency(
                10,
                *(
                    download_video_subtitles_and_maybe_audio(
                        video_metadata,
                        get_size=True,
                    )
                    for video_metadata in series["videos_data"]
                ),
            )
            log.debug(f"{filesizes=}")

            pretty_sum_filesize = prettyprint_convert_bytes_size(sum(filesizes))
            if len(entries) == 1:
                print(
                    "👉 Now I have the link. Let me try to download this "
                    f"video ({pretty_sum_filesize}) to your computer.\n"
                )
            else:
                print(
                    "👉 I have all the links now. Let me try to download "
                    f"{len(entries)} videos "
                    f"({pretty_sum_filesize}) to your computer.\n"
                )
            # await page.pause()
            await gather_with_concurrency(
                5,
                *(
                    download_video_subtitles_and_maybe_audio(
                        video_metadata,
                    )
                    for video_metadata in series["videos_data"]
                ),
            )
            log.debug("Done downloading openly accessible videos.")
            print("\n\n👍 Done downloading openly accessible videos.\n\n")

        # if there are any eth series, log in with eth credentials
        # Used later. Is important to set here,
        # for the initial case when the urls file is empty
        eth_success = None
        if eth_series:
            # why only 1 var? because the login works for the whole context,
            # whereas for protected series you need to login per-series
            eth_success = await login_eth(args, context)
        # If there are any protected series, log in once per series.
        # TODO: Turns out, the cookie gets saved only if the user is logged in
        # with ETH credentials
        # else, the user has to relogin for each protected video!
        # This complicates things a bit.
        # Because now I need to discern between the two cases
        # Create a new list of protected series successfully logged in to.
        async def login_protected_first_video(context, series):
            page = await context.new_page()
            entries = await extract_video_entries(series["graphql"])
            entry = entries[0]
            entry_id = entry["id"].split("ev", 1)[1]
            video_page_url = f"{series['url']}/v/{entry_id}"
            await page.goto(video_page_url)
            series = await login_protected(context, series, page)
            return series

        tasks = [login_protected_first_video(context, ps) for ps in protected_series]
        # if a series was returned and not None:
        protected_series_logged_in = [x for x in
                                      await gather_with_concurrency(10, *tasks)
                                      if x ]

        # download protected series for which the login was successful
        if not eth_success:
            # if eth is not logged in I must login for each protected video
            # over and over again
            await download_protected_videos(context,
                                            protected_series_logged_in,
                                            protected_without_eth_login=True)

        # download eth_series if login successful
        if eth_success:
            # Get protected series
            # The cookie for protected video seems to get saved only when
            # the user is logged in with ETH login
            # else, need to re-login for each new page()'s protected video
            await download_protected_videos(context,
                                            protected_series_logged_in)
            # print(f"\nTrying to get {len(eth_series)} series with ETH login.")
            for series in eth_series:
                # for ETH videos refetch graphql (the previous values 'graphql'
                # and 'title' would be overwritten), since access was denied
                # during get_series_type():
                # {'__typename': 'NotAllowed', 'dummy': None}
                session_cookies = await get_session_cookies(context)
                graphql_request = await intercept_graphql(page)
                series = await graphql_append_json_metadata(
                    series, graphql_request, session_cookies
                )
                entries = await extract_video_entries(series["graphql"])
                log.debug(f"before graphql extraction: {series=}")

                log.debug(f"This many videos would be fetched: {len(entries)}")

                tasks = [
                    process_entry(context, entry, series) for entry in entries
                ]
                series["videos_data"] = await gather_with_concurrency(
                    10, *tasks
                )
                # UX:
                print(ux_clicking_message)
                # Get exact total file size of the downloads
                # (ugly for now to put this in one func, would refacotr later):
                filesizes = await gather_with_concurrency(
                    10,
                    *(
                        download_video_subtitles_and_maybe_audio(
                            video_metadata,
                            get_size=True,
                        )
                        for video_metadata in series["videos_data"]
                    ),
                )
                log.debug(f"{filesizes=}")

                pretty_sum_filesize = prettyprint_convert_bytes_size(
                    sum(filesizes)
                )
                if len(entries) == 1:
                    print(
                        "👉 Now I have the link. Let me try to download this "
                        f"video ({pretty_sum_filesize}) to your computer.\n"
                    )
                else:
                    print(
                        "👉 I have all the links now. Let me try to download "
                        f"{len(entries)} videos "
                        f"({pretty_sum_filesize}) to your computer.\n"
                    )
                await gather_with_concurrency(
                    5,
                    *(
                        download_video_subtitles_and_maybe_audio(
                            video_metadata,
                        )
                        for video_metadata in series["videos_data"]
                    ),
                )
            log.debug("Done downloading videos requiring ETH login.")
            print("\n\n👍 Done downloading videos requiring ETH login.\n\n")

    await browser.close()


def entry_point():
    """Synchronous wrapper for the package entry point."""
    # global args

    parser = setup_arg_parser()
    args = parser.parse_args()
    setup_logging(args)
    # go async
    try:
        asyncio.run(main(args))
    except KeyboardInterrupt:
        # Handle Ctrl+C gracefully
        print("\nProgram interrupted by the user. Exiting...")


if __name__ == "__main__":
    entry_point()
