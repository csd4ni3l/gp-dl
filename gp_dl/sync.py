import json
import logging
import mimetypes
import os
import re
import shutil
import time
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen

from .browser import find_completed_download_file
from .config import WEB_DRIVER_WAIT
from .google_photos_ui import (
    _collect_album_photo_items,
    _is_motion_photo_page,
    _photo_image_download_url,
    _start_download_with_keyboard_shortcut,
    _wait_for_download_start,
)
from .local_state import (
    _album_output_dirs,
    _is_path_within,
    _local_album_files_without_google_id,
    _local_album_google_id_files,
    _record_google_id_file,
    _sanitize_path_component,
)
from .manifest import (
    GOOGLE_ID_MANIFEST_FILENAME,
    _load_google_id_manifest,
    _save_google_id_manifest,
)
from .parsing import (
    _download_response_filename,
    _extract_google_id_from_filename,
    _extract_media_filenames,
    _normalize_filename,
)

MOTION_PHOTO_DIRECT_SAVE_ONLY = False


def _filename_with_google_id_suffix(filename: str, google_id: str) -> str:
    path = Path(filename)
    return f"{path.stem}__gp-{google_id}{path.suffix}"


def _path_conflicts_case_insensitive(path: Path) -> bool:
    if path.exists():
        return True

    parent = path.parent
    if not parent.exists() or not parent.is_dir():
        return False

    target_name = path.name.casefold()
    try:
        for child in parent.iterdir():
            if child.is_file() and child.name.casefold() == target_name:
                return True
    except OSError:
        return path.exists()

    return False


def _ensure_unique_path(path: Path) -> Path:
    if not _path_conflicts_case_insensitive(path):
        return path

    counter = 1
    while True:
        candidate = path.with_name(f"{path.stem}__{counter}{path.suffix}")
        if not _path_conflicts_case_insensitive(candidate):
            return candidate
        counter += 1


def _best_effort_unlink(path: Path | None, context: str) -> None:
    if path is None:
        return
    try:
        if path.exists():
            path.unlink()
    except OSError as e:
        logging.debug(f"Could not remove {context} {path}: {e}")


def _move_download_file_with_retries(
    source: Path, target: Path, google_id: str, retries: int = 5, delay: float = 0.25
) -> bool:
    if source.resolve() == target.resolve():
        return True

    last_error: OSError | None = None
    for attempt in range(1, retries + 1):
        try:
            _ = shutil.move(str(source), str(target))
            return True
        except OSError as e:
            last_error = e
            logging.debug(
                f"Could not move downloaded file for Google Photos item {google_id} (attempt {attempt}/{retries}) from {source} to {target}: {e}"
            )
            time.sleep(delay)

    logging.error(
        f"Could not move downloaded file for Google Photos item {google_id} from {source} to {target}: {last_error}"
    )
    return False


def _snapshot_completed_files(
    temp_dir_path: Path,
) -> dict[str, tuple[int, int]]:
    snapshot: dict[str, tuple[int, int]] = {}
    if not temp_dir_path.exists() or not temp_dir_path.is_dir():
        return snapshot

    try:
        for child in temp_dir_path.iterdir():
            if not child.is_file() or child.suffix.casefold() in {
                ".crdownload",
                ".tmp",
                ".html",
                ".htm",
            }:
                continue
            stat = child.stat()
            snapshot[child.name] = (int(stat.st_size), int(stat.st_mtime_ns))
    except OSError:
        return snapshot

    return snapshot


def _find_completed_download_with_overwrite_detection(
    temp_dir_path: Path,
    excluded_names: set[str],
    initial_snapshot: dict[str, tuple[int, int]],
) -> str | None:
    detected = find_completed_download_file(str(temp_dir_path), excluded_names)
    if detected:
        return detected

    if not temp_dir_path.exists() or not temp_dir_path.is_dir():
        return None

    try:
        for child in temp_dir_path.iterdir():
            if not child.is_file() or child.suffix.casefold() in {
                ".crdownload",
                ".tmp",
                ".html",
                ".htm",
            }:
                continue
            if child.name not in excluded_names:
                return child.name

            stat = child.stat()
            current = (int(stat.st_size), int(stat.st_mtime_ns))
            if initial_snapshot.get(child.name) != current:
                return child.name
    except OSError:
        return None

    return None


def _download_motion_photo_still(
    driver,
    temp_dir_path: Path,
    google_id: str,
    item_url: str | None = None,
    timeout: float | None = None,
    image_url: str | None = None,
    referer_url: str | None = None,
) -> str | None:
    original_rect = None
    try:
        if not image_url:
            try:
                # Force a navigation even when Chrome reports the same URL. A failed
                # motion-photo Shift+D can leave Google Photos on an error screen at
                # the item URL while the DOM still contains the previous photo's
                # image element.
                if item_url:
                    driver.get(item_url)
            except Exception:
                pass

            time.sleep(0.5)

            try:
                original_rect = driver.get_window_rect()
            except Exception:
                original_rect = None

            try:
                driver.set_window_rect(width=3000, height=3000)
            except Exception:
                try:
                    driver.set_window_size(3000, 3000)
                except Exception as e:
                    logging.debug(
                        f"Could not resize browser window for motion photo fallback {google_id}: {e}"
                    )

            time.sleep(0.5)

            image_url = _photo_image_download_url(
                driver, timeout=timeout if timeout is not None else 3.0
            )
            if not image_url:
                logging.error(
                    f"Could not determine motion photo image URL for Google Photos item {google_id}."
                )
                return None

        if referer_url is None:
            try:
                referer_url = driver.current_url
            except Exception:
                referer_url = item_url

        headers = {"Referer": referer_url or item_url or "https://photos.google.com/"}
        try:
            user_agent = driver.execute_script("return navigator.userAgent")
            if user_agent:
                headers["User-Agent"] = str(user_agent)
        except Exception:
            pass

        try:
            cookies = driver.get_cookies() or []
            cookie_header = "; ".join(
                f"{cookie.get('name')}={cookie.get('value')}"
                for cookie in cookies
                if cookie.get("name") is not None and cookie.get("value") is not None
            )
            if cookie_header:
                headers["Cookie"] = cookie_header
        except Exception:
            pass

        request = Request(image_url, headers=headers)
        timeout_seconds = timeout if timeout is not None else 10.0

        with urlopen(request, timeout=timeout_seconds) as response:
            filename = _download_response_filename(response)
            if not filename:
                filename = os.path.basename(urlparse(response.geturl()).path)

            if not filename:
                content_type = response.headers.get_content_type()
                guessed_extension = (
                    mimetypes.guess_extension(content_type or "") or ".jpg"
                )
                filename = f"{google_id}{guessed_extension}"
            else:
                filename = os.path.basename(unquote(filename.strip().strip('"')))
                if not Path(filename).suffix:
                    content_type = response.headers.get_content_type()
                    guessed_extension = (
                        mimetypes.guess_extension(content_type or "") or ".jpg"
                    )
                    filename = f"{filename}{guessed_extension}"

            temp_dir_path.mkdir(parents=True, exist_ok=True)
            save_path = _ensure_unique_path((temp_dir_path / filename).resolve())
            with save_path.open("wb") as output_file:
                shutil.copyfileobj(response, output_file)

            logging.info(
                f"Saved motion photo still directly from page as {save_path.name}"
            )
            return save_path.name
    except (HTTPError, URLError, OSError) as e:
        logging.error(
            f"Could not save motion photo still for Google Photos item {google_id}: {e}"
        )
        return None
    finally:
        if original_rect:
            try:
                driver.set_window_rect(
                    x=original_rect.get("x", 0),
                    y=original_rect.get("y", 0),
                    width=original_rect.get("width", 0),
                    height=original_rect.get("height", 0),
                )
            except Exception:
                try:
                    driver.set_window_size(
                        original_rect.get("width", 0), original_rect.get("height", 0)
                    )
                except Exception:
                    pass


def _motion_photo_page_looks_broken(driver) -> bool:
    try:
        title = (driver.title or "").casefold()
    except Exception:
        title = ""

    try:
        current_url = (driver.current_url or "").casefold()
    except Exception:
        current_url = ""

    try:
        body_text = driver.execute_script(
            "return document.body ? document.body.innerText : '';"
        )
        body_text = str(body_text or "").casefold()
    except Exception:
        body_text = ""

    haystack = " ".join([title, current_url, body_text])
    return "500" in haystack and ("error" in haystack or "server" in haystack)


def _find_conflicting_file_case_insensitive(path: Path) -> Path | None:
    parent = path.parent
    if not parent.exists() or not parent.is_dir():
        return None

    target_name = path.name.casefold()
    try:
        for child in parent.iterdir():
            if child.is_file() and child.name.casefold() == target_name:
                return child.resolve()
    except OSError:
        return None

    return None


GOOGLE_ID_SUFFIX_IN_STEM_RE = re.compile(r"__gp-[A-Za-z0-9_-]+$", re.IGNORECASE)


def _normalized_match_filename(filename: str) -> str:
    path = Path(filename)
    stem = GOOGLE_ID_SUFFIX_IN_STEM_RE.sub("", path.stem)
    return _normalize_filename(f"{stem}{path.suffix}")


def _path_match_priority(path: Path) -> tuple[int, str]:
    has_id_suffix = _extract_google_id_from_filename(path.name) is not None
    return (0 if not has_id_suffix else 1, path.name.casefold())


def _find_existing_album_file_for_filename(
    output_path: Path,
    album_title: str,
    filename: str,
    exclude_paths: set[Path] | None = None,
    allowed_paths: set[Path] | None = None,
) -> Path | None:
    normalized = _normalized_match_filename(filename)
    files_by_name, _ = _local_album_files_by_normalized_name(output_path, album_title)
    for candidate in sorted(
        files_by_name.get(normalized, []), key=_path_match_priority
    ):
        resolved = candidate.resolve()
        if exclude_paths and resolved in exclude_paths:
            continue
        if allowed_paths is not None and resolved not in allowed_paths:
            continue
        if resolved.exists() and resolved.is_file():
            return resolved
    return None


def _local_album_files_by_normalized_name(
    output_path: Path, album_title: str
) -> tuple[dict[str, list[Path]], list[Path]]:
    album_dirs = _album_output_dirs(output_path, album_title)
    files_by_name: dict[str, list[Path]] = {}

    for album_dir in album_dirs:
        if not album_dir.exists() or not album_dir.is_dir():
            continue
        for root, _, files in os.walk(album_dir):
            for filename in files:
                if filename == GOOGLE_ID_MANIFEST_FILENAME:
                    continue
                normalized = _normalized_match_filename(filename)
                extension = os.path.splitext(normalized)[1]
                if not extension:
                    continue
                file_path = (Path(root) / filename).resolve()
                if not file_path.exists() or not file_path.is_file():
                    continue
                if not _is_path_within(file_path, output_path):
                    continue
                if file_path not in files_by_name.setdefault(normalized, []):
                    files_by_name[normalized].append(file_path)

    return files_by_name, album_dirs


def _record_google_id_for_existing_path(
    album_dirs: list[Path],
    google_id: str,
    file_path: Path,
    target_album_dir: Path,
    output_path: Path,
) -> None:
    album_dir = next(
        (
            candidate
            for candidate in album_dirs
            if candidate.exists()
            and candidate.is_dir()
            and _is_path_within(file_path, candidate)
        ),
        target_album_dir,
    )
    _record_google_id_file(album_dir, google_id, file_path, output_path)


def _prepare_download_focus(driver, google_id: str) -> None:
    try:
        body = driver.execute_script("return document.body")
        if body is not None:
            try:
                if body.focus:
                    body.focus()
            except Exception:
                pass
        _ = driver.execute_script(
            """
            try {
              const media = document.querySelector('video, img');
              if (media && media.focus) media.focus();
            } catch (e) {}
            return true;
            """
        )
    except Exception as e:
        logging.debug(
            f"Could not prepare focus for Google Photos item {google_id}: {e}"
        )


def _capture_download_failure_artifacts(
    driver,
    output_path: Path,
    album_title: str,
    google_id: str,
    reason: str,
    item_url: str,
    temp_dir_path: Path,
) -> None:
    try:
        safe_album = re.sub(r"[^A-Za-z0-9._-]+", "_", album_title) or "album"
        safe_id = re.sub(r"[^A-Za-z0-9._-]+", "_", google_id) or "item"
        ts = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
        artifact_dir = (
            output_path / ".gp-dl-failures" / safe_album / f"{ts}_{safe_id}"
        ).resolve()
        artifact_dir.mkdir(parents=True, exist_ok=True)

        screenshot_path = artifact_dir / "screenshot.png"
        html_path = artifact_dir / "page.html"
        meta_path = artifact_dir / "meta.json"
        browser_log_path = artifact_dir / "browser.log"
        performance_log_path = artifact_dir / "performance.log"
        download_dir_snapshot_path = artifact_dir / "download_dir_snapshot.txt"

        try:
            driver.save_screenshot(str(screenshot_path))
        except Exception as e:
            logging.debug(f"Could not capture screenshot for {google_id}: {e}")

        try:
            html_path.write_text(driver.page_source or "", encoding="utf-8")
        except Exception as e:
            logging.debug(f"Could not capture page source for {google_id}: {e}")

        meta = {
            "google_id": google_id,
            "album": album_title,
            "reason": reason,
            "requested_item_url": item_url,
            "current_url": None,
            "title": None,
            "active_element": None,
            "has_focus": None,
            "timestamp_utc": ts,
            "temp_dir": str(temp_dir_path),
        }

        try:
            meta["current_url"] = driver.current_url
        except Exception:
            pass
        try:
            meta["title"] = driver.title
        except Exception:
            pass
        try:
            active = driver.execute_script(
                """
                const el = document.activeElement;
                if (!el) return null;
                return {
                  tag: el.tagName,
                  id: el.id || null,
                  className: el.className || null,
                  ariaLabel: el.getAttribute ? el.getAttribute('aria-label') : null
                };
                """
            )
            meta["active_element"] = active
        except Exception:
            pass
        try:
            meta["has_focus"] = driver.execute_script("return document.hasFocus();")
        except Exception:
            pass

        try:
            meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        except Exception as e:
            logging.debug(f"Could not write meta.json for {google_id}: {e}")

        try:
            browser_entries = driver.get_log("browser")
            browser_log_path.write_text(
                "\n".join(json.dumps(entry) for entry in browser_entries),
                encoding="utf-8",
            )
        except Exception as e:
            logging.debug(f"Could not capture browser console log for {google_id}: {e}")

        try:
            perf_entries = driver.get_log("performance")
            performance_log_path.write_text(
                "\n".join(json.dumps(entry) for entry in perf_entries),
                encoding="utf-8",
            )
        except Exception as e:
            logging.debug(f"Could not capture performance log for {google_id}: {e}")

        try:
            if temp_dir_path.exists() and temp_dir_path.is_dir():
                names = sorted(path.name for path in temp_dir_path.iterdir())
                download_dir_snapshot_path.write_text(
                    "\n".join(names), encoding="utf-8"
                )
            else:
                download_dir_snapshot_path.write_text(
                    "<missing download directory>", encoding="utf-8"
                )
        except Exception as e:
            logging.debug(f"Could not snapshot download directory for {google_id}: {e}")

        logging.error(
            f"Captured failure artifacts for Google Photos item {google_id} at {artifact_dir}"
        )
    except Exception as e:
        logging.debug(f"Could not capture failure artifacts for {google_id}: {e}")


def _rewrite_full_album_manifest(output_path: Path, album_title: str) -> None:
    album_dirs = _album_output_dirs(output_path, album_title)
    target_album_dir = next(
        (path for path in album_dirs if path.exists() and path.is_dir()),
        output_path / _sanitize_path_component(album_title),
    )
    target_album_dir.mkdir(parents=True, exist_ok=True)

    preferred_by_id: dict[str, Path] = {}
    for album_dir in album_dirs:
        if not album_dir.exists() or not album_dir.is_dir():
            continue
        manifest = _load_google_id_manifest(album_dir)
        for google_id, filename in manifest.items():
            resolved = (album_dir / filename).resolve()
            if (
                resolved.exists()
                and resolved.is_file()
                and _is_path_within(resolved, output_path)
            ):
                preferred_by_id.setdefault(google_id.casefold(), resolved)

    files_by_google_id, _ = _local_album_google_id_files(output_path, album_title)
    full_manifest: dict[str, str] = {}

    for google_id_key, paths in files_by_google_id.items():
        valid_paths = [
            path.resolve()
            for path in paths
            if path.exists()
            and path.is_file()
            and _is_path_within(path.resolve(), output_path)
        ]
        if not valid_paths:
            continue

        chosen = preferred_by_id.get(google_id_key)
        if chosen is None:
            chosen = sorted(valid_paths, key=lambda value: str(value).casefold())[0]

        if not _is_path_within(chosen, target_album_dir):
            continue

        relative_name = chosen.relative_to(target_album_dir.resolve()).as_posix()
        full_manifest[google_id_key] = relative_name

    _save_google_id_manifest(target_album_dir, full_manifest)


def _cleanup_bootstrap_plain_duplicates(
    output_path: Path, album_title: str, album_items: list[dict[str, str]]
) -> None:
    # Bootstrap mode should preserve existing plain filenames and avoid destructive
    # cleanup. We keep this function as a no-op for backwards compatibility.
    _ = (output_path, album_title, album_items)
    return


def _delete_duplicate_files_for_manifest_ids(
    output_path: Path, album_title: str
) -> None:
    album_dirs = _album_output_dirs(output_path, album_title)
    manifest_id_paths: dict[str, Path] = {}
    manifest_listed_paths: set[Path] = set()

    for album_dir in album_dirs:
        if not album_dir.exists() or not album_dir.is_dir():
            continue
        manifest = _load_google_id_manifest(album_dir)
        for google_id, filename in manifest.items():
            resolved = (album_dir / filename).resolve()
            if not resolved.exists() or not resolved.is_file():
                continue
            if not _is_path_within(resolved, output_path):
                continue
            manifest_listed_paths.add(resolved)
            manifest_id_paths.setdefault(google_id.casefold(), resolved)

    if not manifest_id_paths:
        return

    for album_dir in album_dirs:
        if not album_dir.exists() or not album_dir.is_dir():
            continue
        for root, _, files in os.walk(album_dir):
            for filename in files:
                if filename == GOOGLE_ID_MANIFEST_FILENAME:
                    continue
                google_id = _extract_google_id_from_filename(filename)
                if not google_id:
                    continue

                resolved = (Path(root) / filename).resolve()
                if not resolved.exists() or not resolved.is_file():
                    continue
                if not _is_path_within(resolved, output_path):
                    continue

                winner = manifest_id_paths.get(google_id.casefold())
                if winner is None or resolved == winner:
                    continue
                if resolved in manifest_listed_paths:
                    continue

                _delete_local_album_file(
                    resolved,
                    output_path,
                    f"duplicate for Google ID {google_id}; {GOOGLE_ID_MANIFEST_FILENAME} mapping wins",
                )


def _ensure_album_manifest_mappings(
    output_path: Path,
    album_title: str,
    album_items: list[dict[str, str]],
    cleanup_duplicates: bool = True,
    trusted_existing_paths: set[Path] | None = None,
) -> None:
    album_dirs = _album_output_dirs(output_path, album_title)
    target_album_dir = next(
        (path for path in album_dirs if path.exists() and path.is_dir()),
        output_path / _sanitize_path_component(album_title),
    )
    target_album_dir.mkdir(parents=True, exist_ok=True)

    files_by_name, album_dirs = _local_album_files_by_normalized_name(
        output_path, album_title
    )
    files_by_google_id, _ = _local_album_google_id_files(output_path, album_title)
    manifest_has_entries = any(
        _load_google_id_manifest(album_dir) for album_dir in album_dirs
    )
    assigned_paths = {
        path.resolve()
        for paths in files_by_google_id.values()
        for path in paths
        if path.exists() and path.is_file()
    }

    if manifest_has_entries:
        for paths in files_by_name.values():
            for path in paths:
                google_id = _extract_google_id_from_filename(path.name)
                if not google_id:
                    continue
                google_id_key = google_id.casefold()
                existing_paths = [
                    existing_path.resolve()
                    for existing_path in files_by_google_id.get(google_id_key, [])
                    if existing_path.exists() and existing_path.is_file()
                ]
                if existing_paths:
                    continue
                resolved = path.resolve()
                _record_google_id_for_existing_path(
                    album_dirs,
                    google_id,
                    resolved,
                    target_album_dir,
                    output_path,
                )
                assigned_paths.add(resolved)

    files_by_google_id, _ = _local_album_google_id_files(output_path, album_title)
    assigned_paths = {
        path.resolve()
        for paths in files_by_google_id.values()
        for path in paths
        if path.exists() and path.is_file()
    }

    for item in album_items:
        google_id = str(item.get("google_id", "")).strip()
        if not google_id:
            continue

        existing_paths = files_by_google_id.get(google_id.casefold(), [])
        if any(path.exists() and path.is_file() for path in existing_paths):
            continue

        candidate_filenames = _extract_media_filenames(
            str(item.get("identifiers", "") or "")
        )
        matched_existing = None
        for filename in candidate_filenames:
            normalized = _normalized_match_filename(filename)
            candidate_paths = sorted(
                files_by_name.get(normalized, []), key=_path_match_priority
            )
            for path in candidate_paths:
                resolved = path.resolve()
                if resolved in assigned_paths:
                    continue
                if (
                    trusted_existing_paths is not None
                    and resolved not in trusted_existing_paths
                ):
                    continue
                matched_existing = resolved
                break
            if matched_existing is not None:
                break

        if matched_existing is None:
            continue

        _record_google_id_for_existing_path(
            album_dirs,
            google_id,
            matched_existing,
            target_album_dir,
            output_path,
        )
        assigned_paths.add(matched_existing)

    if cleanup_duplicates:
        _delete_duplicate_files_for_manifest_ids(output_path, album_title)


def _download_individual_album_items(
    driver,
    items: list[dict[str, str]],
    album_title: str,
    output_path: Path,
    temp_dir_path: Path,
    bootstrap_from_filename: bool = False,
    trusted_existing_paths: set[Path] | None = None,
) -> tuple[int, int, int]:
    album_dirs = _album_output_dirs(output_path, album_title)
    target_album_dir = next(
        (path for path in album_dirs if path.exists() and path.is_dir()),
        output_path / _sanitize_path_component(album_title),
    )
    target_album_dir.mkdir(parents=True, exist_ok=True)

    downloaded_count = 0
    skipped_count = 0
    failed_count = 0

    files_by_name, album_dirs = _local_album_files_by_normalized_name(
        output_path, album_title
    )
    consumed_name_match_paths: set[Path] = set()

    for item in items:
        google_id = item["google_id"]
        existing_download_files = (
            set(temp_dir_path.iterdir()) if temp_dir_path.is_dir() else set()
        )
        existing_download_names = {p.name for p in existing_download_files}
        existing_download_snapshot = _snapshot_completed_files(temp_dir_path)
        downloaded_path: Path | None = None
        try:
            global MOTION_PHOTO_DIRECT_SAVE_ONLY

            files_by_google_id, _ = _local_album_google_id_files(
                output_path, album_title
            )
            existing_with_id = files_by_google_id.get(google_id.casefold(), [])
            if existing_with_id:
                logging.info(
                    f"Skipping Google Photos item {google_id}; already saved as {existing_with_id[0]}"
                )
                skipped_count += 1
                continue

            if bootstrap_from_filename:
                candidate_filenames = _extract_media_filenames(
                    str(item.get("identifiers", "") or "")
                )
                assigned_paths = {
                    path.resolve()
                    for existing_paths in files_by_google_id.values()
                    for path in existing_paths
                }
                matched_existing = None
                for filename in candidate_filenames:
                    normalized = _normalized_match_filename(filename)
                    paths = sorted(
                        files_by_name.get(normalized, []), key=_path_match_priority
                    )
                    for path in paths:
                        resolved = path.resolve()
                        if (
                            resolved in consumed_name_match_paths
                            or resolved in assigned_paths
                        ):
                            continue
                        if (
                            trusted_existing_paths is not None
                            and resolved not in trusted_existing_paths
                        ):
                            continue
                        matched_existing = resolved
                        break
                    if matched_existing is not None:
                        break

                if matched_existing is not None:
                    consumed_name_match_paths.add(matched_existing)

                if matched_existing is not None:
                    _record_google_id_for_existing_path(
                        album_dirs,
                        google_id,
                        matched_existing,
                        target_album_dir,
                        output_path,
                    )
                    logging.info(
                        f"Matched existing file by filename for Google Photos item {google_id}: {matched_existing}. Added mapping to {GOOGLE_ID_MANIFEST_FILENAME}."
                    )
                    skipped_count += 1
                    continue

            logging.info(f"Downloading missing Google Photos item {google_id}")
            driver.get(item["url"])
            _prepare_download_focus(driver, google_id)

            downloaded_file = None
            motion_photo_still_url = None
            motion_photo_page = _is_motion_photo_page(driver, timeout=0.5)
            if motion_photo_page:
                motion_photo_still_url = _photo_image_download_url(driver, timeout=3.0)
                if MOTION_PHOTO_DIRECT_SAVE_ONLY:
                    logging.info(
                        f"Motion photo controls detected for Google Photos item {google_id}; using direct save fallback for this session."
                    )
                    downloaded_file = _download_motion_photo_still(
                        driver,
                        temp_dir_path,
                        google_id,
                        item_url=item["url"],
                        timeout=3.0,
                        image_url=motion_photo_still_url,
                        referer_url=item["url"],
                    )
                    if not downloaded_file:
                        failed_count += 1
                        continue
                else:
                    logging.info(
                        f"Motion photo controls detected for Google Photos item {google_id}."
                    )

            download_started = False
            if downloaded_file:
                pass
            elif motion_photo_page and not MOTION_PHOTO_DIRECT_SAVE_ONLY:
                triggered = _start_download_with_keyboard_shortcut(driver)
                broken_motion_page = _motion_photo_page_looks_broken(driver)
                download_started = bool(
                    triggered
                    and not broken_motion_page
                    and _wait_for_download_start(
                        temp_dir_path,
                        existing_download_names,
                        timeout=2.0,
                    )
                )
                if not download_started:
                    MOTION_PHOTO_DIRECT_SAVE_ONLY = True
                    if broken_motion_page:
                        logging.info(
                            f"Motion photo download hit the Google Photos error screen for item {google_id}; using direct save fallback for this session."
                        )
                    else:
                        logging.info(
                            f"Motion photo download failed for Google Photos item {google_id}; using direct save fallback for this session."
                        )
                    downloaded_file = _download_motion_photo_still(
                        driver,
                        temp_dir_path,
                        google_id,
                        item_url=item["url"],
                        timeout=3.0,
                        image_url=motion_photo_still_url,
                        referer_url=item["url"],
                    )
                    if not downloaded_file:
                        failed_count += 1
                        continue
                else:
                    downloaded_file = None
                    deadline = time.perf_counter() + 4.0
                    while time.perf_counter() < deadline and not downloaded_file:
                        downloaded_file = (
                            _find_completed_download_with_overwrite_detection(
                                temp_dir_path,
                                existing_download_names,
                                existing_download_snapshot,
                            )
                        )
                        time.sleep(0.1)

                    if not downloaded_file:
                        MOTION_PHOTO_DIRECT_SAVE_ONLY = True
                        logging.info(
                            f"Motion photo download stalled for Google Photos item {google_id}; using direct save fallback for this session."
                        )
                        downloaded_file = _download_motion_photo_still(
                            driver,
                            temp_dir_path,
                            google_id,
                            item_url=item["url"],
                            timeout=3.0,
                            image_url=motion_photo_still_url,
                            referer_url=item["url"],
                        )
                        if not downloaded_file:
                            failed_count += 1
                            continue
            else:
                download_started = False
                for attempt in range(1, 4):
                    triggered = _start_download_with_keyboard_shortcut(driver)
                    if triggered and _wait_for_download_start(
                        temp_dir_path,
                        existing_download_names,
                        timeout=max(6.0, float(WEB_DRIVER_WAIT)),
                    ):
                        download_started = True
                        break
                    logging.debug(
                        f"Keyboard shortcut attempt {attempt}/3 did not start a download for Google Photos item {google_id}."
                    )
                    time.sleep(0.35)

                if not download_started:
                    logging.info(
                        f"Retrying keyboard download start for Google Photos item {google_id} after reloading the item page."
                    )
                    driver.get(item["url"])
                    _prepare_download_focus(driver, google_id)
                    for attempt in range(1, 3):
                        triggered = _start_download_with_keyboard_shortcut(driver)
                        if triggered and _wait_for_download_start(
                            temp_dir_path,
                            existing_download_names,
                            timeout=max(4.0, float(WEB_DRIVER_WAIT) / 2),
                        ):
                            download_started = True
                            break
                        logging.debug(
                            f"Keyboard shortcut reload retry {attempt}/2 did not start a download for Google Photos item {google_id}."
                        )
                        time.sleep(0.35)

                    if not download_started:
                        logging.error(
                            f"Could not start individual download for Google Photos item {google_id} using keyboard shortcut (Shift+D)."
                        )
                        _capture_download_failure_artifacts(
                            driver,
                            output_path,
                            album_title,
                            google_id,
                            "download_not_started_shift_d",
                            item.get("url", ""),
                            temp_dir_path,
                        )
                        failed_count += 1
                        continue

                downloaded_file = None
                deadline = time.perf_counter() + max(WEB_DRIVER_WAIT, 10) * 12
                while time.perf_counter() < deadline and not downloaded_file:
                    downloaded_file = _find_completed_download_with_overwrite_detection(
                        temp_dir_path,
                        existing_download_names,
                        existing_download_snapshot,
                    )
                    time.sleep(0.1)

            if not downloaded_file:
                logging.error(
                    f"Timed out waiting for individual download for Google Photos item {google_id}"
                )
                _capture_download_failure_artifacts(
                    driver,
                    output_path,
                    album_title,
                    google_id,
                    "download_timeout_after_start",
                    item.get("url", ""),
                    temp_dir_path,
                )
                failed_count += 1
                continue

            downloaded_path = temp_dir_path / downloaded_file
            downloaded_name = downloaded_path.name

            if bootstrap_from_filename:
                matched_existing = _find_existing_album_file_for_filename(
                    output_path,
                    album_title,
                    downloaded_name,
                    exclude_paths={downloaded_path.resolve()},
                    allowed_paths=trusted_existing_paths,
                )
                if matched_existing is not None:
                    _record_google_id_for_existing_path(
                        album_dirs,
                        google_id,
                        matched_existing,
                        target_album_dir,
                        output_path,
                    )
                    logging.info(
                        f"Matched existing file after download by filename for Google Photos item {google_id}: {matched_existing}. Added mapping to {GOOGLE_ID_MANIFEST_FILENAME}."
                    )
                    skipped_count += 1
                    _best_effort_unlink(downloaded_path, "downloaded file")
                    continue

            target_name = downloaded_name
            resolved_target = (target_album_dir / target_name).resolve()

            if not _is_path_within(resolved_target, output_path):
                logging.error(
                    f"Skipping downloaded file with unsafe path: {target_name}"
                )
                failed_count += 1
                continue

            if downloaded_path.suffix.casefold() in {".zip", ".html", ".htm"}:
                logging.error(
                    f"Refusing unsupported download format for Google Photos item {google_id}."
                )
                failed_count += 1
                _best_effort_unlink(downloaded_path, "downloaded file")
                continue

            if _path_conflicts_case_insensitive(resolved_target):
                conflicting_path = _find_conflicting_file_case_insensitive(
                    resolved_target
                )
                files_by_google_id, _ = _local_album_google_id_files(
                    output_path, album_title
                )
                path_to_google_ids: dict[Path, set[str]] = {}
                for existing_google_id, existing_paths in files_by_google_id.items():
                    for existing_path in existing_paths:
                        resolved_existing = existing_path.resolve()
                        if (
                            not resolved_existing.exists()
                            or not resolved_existing.is_file()
                        ):
                            continue
                        path_to_google_ids.setdefault(resolved_existing, set()).add(
                            existing_google_id
                        )

                if bootstrap_from_filename:
                    bootstrap_match = _find_existing_album_file_for_filename(
                        output_path,
                        album_title,
                        downloaded_name,
                        exclude_paths={downloaded_path.resolve()},
                        allowed_paths=trusted_existing_paths,
                    )
                    if bootstrap_match is None and conflicting_path is not None:
                        resolved_conflict = conflicting_path.resolve()
                        if resolved_conflict != downloaded_path.resolve() and (
                            trusted_existing_paths is None
                            or resolved_conflict in trusted_existing_paths
                        ):
                            bootstrap_match = resolved_conflict

                    if bootstrap_match is not None:
                        _record_google_id_for_existing_path(
                            album_dirs,
                            google_id,
                            bootstrap_match,
                            target_album_dir,
                            output_path,
                        )
                        logging.info(
                            f"Matched existing file by bootstrap filename collision for Google Photos item {google_id}: {bootstrap_match}. Added mapping to {GOOGLE_ID_MANIFEST_FILENAME}."
                        )
                        skipped_count += 1
                        _best_effort_unlink(downloaded_path, "downloaded file")
                        continue

                    target_name = _filename_with_google_id_suffix(
                        downloaded_name, google_id
                    )
                    resolved_target = (target_album_dir / target_name).resolve()
                    if not _is_path_within(resolved_target, output_path):
                        logging.error(
                            f"Skipping downloaded file with unsafe path: {target_name}"
                        )
                        failed_count += 1
                        _best_effort_unlink(downloaded_path, "downloaded file")
                        continue
                    resolved_target = _ensure_unique_path(resolved_target)
                    logging.debug(
                        f"No trusted pre-existing filename match found for Google Photos item {google_id} during bootstrap collision; saving as {resolved_target.name}."
                    )

                    if not _move_download_file_with_retries(
                        downloaded_path, resolved_target, google_id
                    ):
                        failed_count += 1
                        continue

                    final_name = resolved_target.name
                    _record_google_id_file(
                        target_album_dir, google_id, resolved_target, output_path
                    )
                    if final_name != downloaded_name:
                        logging.info(
                            f"Saved individual download as {final_name} (renamed from {downloaded_name})"
                        )
                    else:
                        logging.info(f"Saved individual download as {final_name}")
                    downloaded_count += 1
                    continue

                if conflicting_path is not None:
                    owner_ids = path_to_google_ids.get(
                        conflicting_path.resolve(), set()
                    )
                    if not owner_ids or google_id.casefold() in owner_ids:
                        _record_google_id_for_existing_path(
                            album_dirs,
                            google_id,
                            conflicting_path,
                            target_album_dir,
                            output_path,
                        )
                        logging.info(
                            f"Matched existing file by filename collision for Google Photos item {google_id}: {conflicting_path}. Added mapping to {GOOGLE_ID_MANIFEST_FILENAME}."
                        )
                        skipped_count += 1
                        _best_effort_unlink(downloaded_path, "downloaded file")
                        continue

                target_name = _filename_with_google_id_suffix(
                    downloaded_name, google_id
                )
                resolved_target = (target_album_dir / target_name).resolve()
                if not _is_path_within(resolved_target, output_path):
                    logging.error(
                        f"Skipping downloaded file with unsafe path: {target_name}"
                    )
                    failed_count += 1
                    _best_effort_unlink(downloaded_path, "downloaded file")
                    continue
                resolved_target = _ensure_unique_path(resolved_target)
                logging.debug(
                    f"Filename collision for Google Photos item {google_id}; saving as {resolved_target.name}"
                )

            if not _move_download_file_with_retries(
                downloaded_path, resolved_target, google_id
            ):
                failed_count += 1
                continue

            final_name = resolved_target.name
            _record_google_id_file(
                target_album_dir, google_id, resolved_target, output_path
            )
            if final_name != downloaded_name:
                logging.info(
                    f"Saved individual download as {final_name} (renamed from {downloaded_name})"
                )
            else:
                logging.info(f"Saved individual download as {final_name}")
            downloaded_count += 1
        except Exception as e:
            logging.error(
                f"Individual sync failed for Google Photos item {google_id}; continuing with next item. Error: {e}"
            )
            failed_count += 1
            _best_effort_unlink(downloaded_path, "downloaded file")
            continue

    return downloaded_count, skipped_count, failed_count


def _delete_local_album_file(path: Path, output_path: Path, reason: str) -> bool:
    try:
        if not path.exists():
            return False
        if not _is_path_within(path, output_path):
            logging.error(f"Skipping delete for unsafe path: {path}")
            return False
        path.unlink()
        logging.info(f"Deleted local file {reason}: {path}")
        return True
    except OSError as e:
        logging.error(f"Could not delete local file {path}: {e}")
        return False


def _propagate_album_deletes(
    output_path: Path,
    album_title: str,
    album_google_ids: set[str],
    delete_without_google_id: bool = True,
) -> int:
    files_by_google_id, album_dirs = _local_album_google_id_files(
        output_path, album_title
    )
    deleted_count = 0

    manifest_listed_paths: set[Path] = set()
    for album_dir in album_dirs:
        manifest = _load_google_id_manifest(album_dir)
        for filename in manifest.values():
            resolved = (album_dir / filename).resolve()
            if not resolved.exists() or not resolved.is_file():
                continue
            if not _is_path_within(resolved, output_path):
                continue
            manifest_listed_paths.add(resolved)

    for google_id, paths in files_by_google_id.items():
        if google_id in album_google_ids:
            continue
        for path in paths:
            resolved = path.resolve()
            if resolved in manifest_listed_paths:
                logging.info(
                    f"Keeping local file listed in {GOOGLE_ID_MANIFEST_FILENAME}: {resolved}"
                )
                continue
            if _delete_local_album_file(resolved, output_path, "missing from album"):
                deleted_count += 1

    if delete_without_google_id:
        for path in _local_album_files_without_google_id(output_path, album_title):
            resolved = path.resolve()
            if resolved in manifest_listed_paths:
                logging.info(
                    f"Keeping local file listed in {GOOGLE_ID_MANIFEST_FILENAME}: {resolved}"
                )
                continue
            if _delete_local_album_file(resolved, output_path, "without Google ID"):
                deleted_count += 1

    result = (
        f"deleted {deleted_count} local file(s)."
        if deleted_count
        else "nothing to delete."
    )
    logging.info(f"Propagated album deletes for {album_title}: {result}")
    return deleted_count


def _download_missing_album_items_by_google_id(
    driver,
    album_title: str,
    output_path: Path,
    temp_dir_path: Path,
    propagate_deletes: bool,
) -> tuple[bool, int, int, int, int, int]:
    album_items = _collect_album_photo_items(driver)
    if not album_items:
        logging.info(
            "Could not collect individual Google Photos item links; individual sync cannot proceed."
        )
        return False, 0, 0, 0, 0, 0

    album_dirs = _album_output_dirs(output_path, album_title)
    manifest_present_initially = any(
        (album_dir / GOOGLE_ID_MANIFEST_FILENAME).exists() for album_dir in album_dirs
    )

    trusted_existing_paths: set[Path] | None = None
    if not manifest_present_initially:
        files_by_name_bootstrap, _ = _local_album_files_by_normalized_name(
            output_path, album_title
        )
        trusted_existing_paths = {
            path.resolve()
            for paths in files_by_name_bootstrap.values()
            for path in paths
            if path.exists() and path.is_file()
        }

    _ensure_album_manifest_mappings(
        output_path,
        album_title,
        album_items,
        cleanup_duplicates=manifest_present_initially,
        trusted_existing_paths=trusted_existing_paths,
    )

    files_by_google_id, album_dirs = _local_album_google_id_files(
        output_path, album_title
    )

    if not manifest_present_initially:
        files_by_name, _ = _local_album_files_by_normalized_name(
            output_path, album_title
        )
        claimed_paths = {
            path.resolve()
            for paths in files_by_google_id.values()
            for path in paths
            if path.exists() and path.is_file()
        }
        target_album_dir = next(
            (path for path in album_dirs if path.exists() and path.is_dir()),
            output_path / _sanitize_path_component(album_title),
        )

        for item in album_items:
            google_id = str(item.get("google_id", "")).strip()
            if not google_id:
                continue
            if google_id.casefold() in files_by_google_id:
                continue

            candidate_filenames = _extract_media_filenames(
                str(item.get("identifiers", "") or "")
            )
            matched_existing = None
            for filename in sorted(candidate_filenames):
                normalized = _normalized_match_filename(filename)
                candidate_paths = sorted(
                    files_by_name.get(normalized, []), key=_path_match_priority
                )
                for path in candidate_paths:
                    resolved = path.resolve()
                    if (
                        not resolved.exists()
                        or not resolved.is_file()
                        or resolved in claimed_paths
                    ):
                        continue
                    if (
                        trusted_existing_paths is not None
                        and resolved not in trusted_existing_paths
                    ):
                        continue
                    matched_existing = resolved
                    break
                if matched_existing is not None:
                    break

            if matched_existing is None:
                continue

            _record_google_id_for_existing_path(
                album_dirs,
                google_id,
                matched_existing,
                target_album_dir,
                output_path,
            )
            logging.info(
                f"Matched existing file before download for Google Photos item {google_id}: {matched_existing}. Added mapping to {GOOGLE_ID_MANIFEST_FILENAME}."
            )
            claimed_paths.add(matched_existing)

        files_by_google_id, _ = _local_album_google_id_files(output_path, album_title)

    existing_google_ids = set(files_by_google_id)
    album_google_ids = {item["google_id"].casefold() for item in album_items}

    if propagate_deletes and not manifest_present_initially:
        logging.info(
            f"Skipping delete propagation for files without Google ID in album [{album_title}] because {GOOGLE_ID_MANIFEST_FILENAME} did not exist before this run."
        )

    deleted_count = (
        _propagate_album_deletes(
            output_path,
            album_title,
            album_google_ids,
            delete_without_google_id=manifest_present_initially,
        )
        if propagate_deletes
        else 0
    )

    missing_items = [
        item
        for item in album_items
        if item["google_id"].casefold() not in existing_google_ids
    ]
    existing_count = len(album_items) - len(missing_items)

    logging.info(
        f"Album [{album_title}] contains {len(album_items)} Google Photos item(s): {existing_count} already saved by Google ID, {len(missing_items)} missing."
    )

    if not missing_items:
        if not manifest_present_initially:
            _cleanup_bootstrap_plain_duplicates(output_path, album_title, album_items)
        _rewrite_full_album_manifest(output_path, album_title)
        return True, 0, existing_count, 0, len(album_items), deleted_count

    downloaded_count, skipped_count, failed_count = _download_individual_album_items(
        driver,
        missing_items,
        album_title,
        output_path,
        temp_dir_path,
        bootstrap_from_filename=not manifest_present_initially,
        trusted_existing_paths=trusted_existing_paths,
    )
    _ensure_album_manifest_mappings(
        output_path,
        album_title,
        album_items,
        cleanup_duplicates=manifest_present_initially,
        trusted_existing_paths=trusted_existing_paths,
    )
    if not manifest_present_initially:
        _cleanup_bootstrap_plain_duplicates(output_path, album_title, album_items)
    _rewrite_full_album_manifest(output_path, album_title)
    return (
        True,
        downloaded_count,
        existing_count + skipped_count,
        failed_count,
        len(album_items),
        deleted_count,
    )
