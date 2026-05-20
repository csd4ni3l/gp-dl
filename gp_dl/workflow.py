import logging
import os
import time
from pathlib import Path

from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from .browser import get_driver
from .config import GOOGLE_LANG, LABELS, WEB_DRIVER_WAIT
from .sync import _download_missing_album_items_by_google_id

__labels = LABELS


def login(user: str, password: str, driver_path: str | None = None, headless=True):
    driver = get_driver(driver_path=driver_path, headless=headless)
    driver.get("https://photos.google.com/login")

    usernameFieldPath = "identifierId"
    usernameNextButtonPath = "identifierNext"
    passwordFieldPath = "Passwd"
    passwordNextButtonPath = "passwordNext"

    usernameField = WebDriverWait(driver, WEB_DRIVER_WAIT).until(
        EC.presence_of_element_located((By.ID, usernameFieldPath))
    )
    time.sleep(1)
    usernameField.send_keys(user)

    usernameNextButton = WebDriverWait(driver, WEB_DRIVER_WAIT).until(
        EC.presence_of_element_located((By.ID, usernameNextButtonPath))
    )
    usernameNextButton.click()

    passwordField = WebDriverWait(driver, WEB_DRIVER_WAIT).until(
        EC.presence_of_element_located((By.NAME, passwordFieldPath))
    )
    time.sleep(1)
    passwordField.send_keys(password)

    passwordNextButton = WebDriverWait(driver, WEB_DRIVER_WAIT).until(
        EC.presence_of_element_located((By.ID, passwordNextButtonPath))
    )
    passwordNextButton.click()


def list_albums(
    profile_dir: str | None = None,
    user: str | None = None,
    password: str | None = None,
    driver_path: str | None = None,
    headless=True,
):
    driver = get_driver(driver_path=driver_path, headless=headless)
    if profile_dir is None:
        if user and password:
            login(
                user=user, password=password, driver_path=driver_path, headless=headless
            )
        else:
            logging.fatal(
                "Neither profile_dir nor user and password has been defined, cannot fetch your albums."
            )
            return

    driver.get("https://photos.google.com/albums")
    try:
        album_div = WebDriverWait(driver, WEB_DRIVER_WAIT).until(
            EC.presence_of_element_located(
                (By.CSS_SELECTOR, 'div[aria-label="{albums}"'.format_map(__labels))
            )
        )
    except TimeoutException:
        logging.error(
            "Could not find the '{albums}' section in time.".format_map(__labels)
        )
        logging.error(
            f"Check if GOOGLE_LANG (value={GOOGLE_LANG}, default en) is set to your language and available."
        )
        logging.info("Continuing with next album URL.")
        raise
    links = album_div.find_elements(By.TAG_NAME, "a")
    album_links = [href for link in links if (href := link.get_attribute("href"))]
    return album_links


def download_all_albums(
    profile_dir: str | None = None,
    user: str | None = None,
    password: str | None = None,
    output_dir: str | None = None,
    driver_path: str | None = None,
    headless=True,
):
    if output_dir is None:
        logging.fatal("No output_dir has been defined, cannot download albums.")
        return

    album_urls = list_albums(
        profile_dir=profile_dir,
        user=user,
        password=password,
        driver_path=driver_path,
        headless=headless,
    )
    if not album_urls:
        return

    _ = download_albums(
        album_urls=album_urls,
        output_dir=output_dir,
        driver_path=driver_path,
        profile_dir=profile_dir,
        headless=headless,
    )


def download_albums(
    album_urls: list[str],
    output_dir: str,
    driver_path: str | None = None,
    profile_dir: str | None = None,
    headless: bool = True,
    temp_dir: str | None = None,
    propagate_deletes: bool = False,
) -> tuple[
    list[str],
    list[str],
    list[float],
    list[int],
    list[int],
    list[dict[str, str | int | float]],
]:
    """
    Download missing media items from one or more Google Photos albums using Selenium.

    Downloads run in ID-based sync mode by default: items already present locally by Google ID are skipped.
    """

    temp_dir = temp_dir or output_dir
    temp_dir_path = Path(temp_dir).resolve()
    output_path = Path(output_dir).resolve()
    
    # Create dedicated downloads subdirectory
    downloads_dir = temp_dir_path / ".downloads"
    downloads_dir.mkdir(parents=True, exist_ok=True)
    logging.info(f"Using downloads subdirectory: {downloads_dir}")
    
    driver = get_driver(
        driver_path=driver_path,
        profile_dir=profile_dir,
        headless=headless,
        temp_dir=str(downloads_dir),
    )

    if not os.path.exists(output_dir) or not os.path.isdir(output_dir):
        logging.fatal(
            "Invalid output directory. Please supply a valid and existing directory."
        )
        return [], [], [], [], [], []

    failed_albums: list[str] = []
    successful_albums: list[str] = []
    album_times: list[float] = []
    album_item_counts: list[int] = []
    album_file_counts: list[int] = []
    album_stats: list[dict[str, str | int | float]] = []

    for album_url in album_urls:
        album_start = time.perf_counter()

        if not temp_dir_path.exists() or not temp_dir_path.is_dir():
            logging.info(f"Creating temp directory for downloads: {temp_dir_path}")
            temp_dir_path.mkdir(parents=True, exist_ok=True)

        driver.get(album_url)
        album_title = driver.title.split(" -")[0]
        logging.info(f"Downloading album [{album_title}] ({album_url})...")

        try:
            (
                handled,
                downloaded_count,
                skipped_count,
                failed_count,
                album_item_count,
                deleted_count,
            ) = _download_missing_album_items_by_google_id(
                driver,
                album_title,
                output_path,
                downloads_dir,
                propagate_deletes,
            )
        except Exception as e:
            logging.error(
                f"Individual sync failed for {album_title}. Error: {e}"
            )
            failed_albums.append(album_title)
            duration = time.perf_counter() - album_start
            album_times.append(duration)
            album_stats.append(
                {
                    "album": album_title,
                    "status": "failed",
                    "items_found": 0,
                    "downloaded": 0,
                    "deleted": 0,
                    "failed_items": 0,
                    "duration_seconds": duration,
                }
            )
            logging.info(
                f"Album stats [{album_title}]: status=failed, items=0, downloaded=0, deleted=0, failed_items=0, time={duration:.2f}s"
            )
            if temp_dir_path != output_path:
                try:
                    temp_dir_path.rmdir()
                except OSError:
                    pass
            continue

        if not handled:
            logging.error(
                f"Could not collect item links for {album_title}."
            )
            failed_albums.append(album_title)
            duration = time.perf_counter() - album_start
            album_times.append(duration)
            album_stats.append(
                {
                    "album": album_title,
                    "status": "failed",
                    "items_found": 0,
                    "downloaded": 0,
                    "deleted": 0,
                    "failed_items": 0,
                    "duration_seconds": duration,
                }
            )
            logging.info(
                f"Album stats [{album_title}]: status=failed, items=0, downloaded=0, deleted=0, failed_items=0, time={duration:.2f}s"
            )
            if temp_dir_path != output_path:
                try:
                    temp_dir_path.rmdir()
                except OSError:
                    pass
            continue

        album_item_counts.append(album_item_count)
        album_file_counts.append(downloaded_count)
        duration = time.perf_counter() - album_start
        album_times.append(duration)
        status = "failed" if failed_count else "successful"

        if failed_count:
            failed_albums.append(album_title)
        else:
            successful_albums.append(album_title)

        album_stats.append(
            {
                "album": album_title,
                "status": status,
                "items_found": album_item_count,
                "downloaded": downloaded_count,
                "deleted": deleted_count,
                "failed_items": failed_count,
                "duration_seconds": duration,
            }
        )
        logging.info(
            f"Individual sync complete for [{album_title}]: {downloaded_count} saved, {skipped_count} skipped, {deleted_count} deleted, {failed_count} failed."
        )
        logging.info(
            f"Album stats [{album_title}]: status={status}, items={album_item_count}, downloaded={downloaded_count}, deleted={deleted_count}, failed_items={failed_count}, time={duration:.2f}s"
        )

        if temp_dir_path != output_path:
            try:
                temp_dir_path.rmdir()
            except OSError:
                pass

    driver.quit()
    return (
        successful_albums,
        failed_albums,
        album_times,
        album_item_counts,
        album_file_counts,
        album_stats,
    )
