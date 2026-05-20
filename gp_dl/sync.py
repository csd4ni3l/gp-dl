import logging
import os
import re
import shutil
import time
from pathlib import Path
from urllib.request import Request, urlopen

from .browser import find_completed_download_file
from .config import WEB_DRIVER_WAIT
from .google_photos_ui import (
    _collect_album_photo_items,
    _is_motion_photo_page,
    _photo_image_download_url,
    _start_download_with_keyboard_shortcut,
    _start_google_photos_download,
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
    _item_looks_like_video,
    _normalize_filename,
)


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


def _download_motion_photo_still(
    driver,
    google_id: str,
    target_album_dir: Path,
    output_path: Path,
    bootstrap_from_filename: bool = False,
    trusted_existing_paths: set[Path] | None = None,
    expected_filenames: set[str] | None = None,
) -> Path | None:
    download_url = _photo_image_download_url(driver)
    if not download_url:
        return None

    try:
        cookie_header = "; ".join(
            f"{cookie['name']}={cookie['value']}" for cookie in driver.get_cookies()
        )
        headers = {"User-Agent": "Mozilla/5.0"}
        if cookie_header:
            headers["Cookie"] = cookie_header
        request = Request(download_url, headers=headers)
        with urlopen(request, timeout=max(WEB_DRIVER_WAIT, 10) * 3) as response:
            filename = _download_response_filename(response)
            if not filename:
                logging.debug(
                    f"Could not determine Google-provided filename for direct image download of Google Photos item {google_id}; refusing to infer a filename or extension from the rendered image URL."
                )
                return None

            if expected_filenames:
                expected_names = {
                    _normalized_match_filename(expected)
                    for expected in expected_filenames
                }
                if _normalized_match_filename(filename) not in expected_names:
                    logging.info(
                        f"Rejected direct image download for Google Photos item {google_id}; response filename {filename} did not match expected album filename(s): {sorted(expected_filenames)}."
                    )
                    return None

            target_name = filename
            resolved_target = (target_album_dir / target_name).resolve()
            if not _is_path_within(resolved_target, output_path):
                logging.error(
                    f"Skipping direct still download with unsafe path: {target_name}"
                )
                return None

            if _path_conflicts_case_insensitive(resolved_target):
                if bootstrap_from_filename:
                    existing_target = _find_conflicting_file_case_insensitive(
                        resolved_target
                    )
                    if existing_target is not None and (
                        trusted_existing_paths is None
                        or existing_target.resolve() in trusted_existing_paths
                    ):
                        _record_google_id_file(
                            target_album_dir, google_id, existing_target, output_path
                        )
                        logging.info(
                            f"Matched existing file by filename for direct image download of Google Photos item {google_id}: {existing_target}. Added mapping to {GOOGLE_ID_MANIFEST_FILENAME}."
                        )
                        return existing_target

                target_name = _filename_with_google_id_suffix(filename, google_id)
                resolved_target = (target_album_dir / target_name).resolve()
                if not _is_path_within(resolved_target, output_path):
                    logging.error(
                        f"Skipping direct still download with unsafe path: {target_name}"
                    )
                    return None
                resolved_target = _ensure_unique_path(resolved_target)
                if bootstrap_from_filename:
                    logging.debug(
                        f"No trusted pre-existing filename match found for direct image download of Google Photos item {google_id} during bootstrap collision; saving as {resolved_target.name}."
                    )
                else:
                    logging.debug(
                        f"Filename collision for direct image download of Google Photos item {google_id}; saving as {resolved_target.name}"
                    )

            temp_target = resolved_target.with_suffix(f"{resolved_target.suffix}.tmp")
            with open(temp_target, "wb") as target_file:
                shutil.copyfileobj(response, target_file)
            temp_target.replace(resolved_target)
            _record_google_id_file(
                target_album_dir, google_id, resolved_target, output_path
            )
            logging.info(f"Saved direct image download to {resolved_target}")
            return resolved_target
    except Exception as e:
        logging.debug(
            f"Direct still download failed for Google Photos item {google_id}: {e}"
        )
        return None


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

        files_by_google_id, _ = _local_album_google_id_files(output_path, album_title)
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

        item_filenames = _extract_media_filenames(
            str(item.get("identifiers", "") or "")
        )
        is_motion_photo = _is_motion_photo_page(driver, timeout=min(WEB_DRIVER_WAIT, 3))
        item_looks_like_video = _item_looks_like_video(item)

        if is_motion_photo and item_looks_like_video:
            logging.debug(
                f"Google Photos item {google_id} is a video; direct still-image fallback is disabled so the downloaded file extension is preserved."
            )

        download_started = _start_download_with_keyboard_shortcut(
            driver
        ) and _wait_for_download_start(
            temp_dir_path, {p.name for p in existing_download_files}
        )

        if not download_started:
            logging.debug(
                f"Keyboard shortcut did not start download for Google Photos item {google_id}; falling back to menu."
            )
            download_started = _start_google_photos_download(
                driver, selected_only=True
            ) and _wait_for_download_start(
                temp_dir_path, {p.name for p in existing_download_files}
            )
            if not download_started and is_motion_photo and not item_looks_like_video:
                logging.debug(
                    f"Google Photos controls did not start a download for motion photo item {google_id}; trying direct image fallback."
                )
                still_path = _download_motion_photo_still(
                    driver,
                    google_id,
                    target_album_dir,
                    output_path,
                    bootstrap_from_filename=bootstrap_from_filename,
                    trusted_existing_paths=trusted_existing_paths,
                    expected_filenames=item_filenames,
                )
                if still_path:
                    downloaded_count += 1
                    continue

            if not download_started:
                logging.error(
                    f"Could not start individual download for Google Photos item {google_id}"
                )
                failed_count += 1
                continue

        downloaded_file = None
        deadline = time.perf_counter() + max(WEB_DRIVER_WAIT, 10) * 12
        while time.perf_counter() < deadline and not downloaded_file:
            downloaded_file = find_completed_download_file(
                str(temp_dir_path), {p.name for p in existing_download_files}
            )
            time.sleep(0.1)

        if not downloaded_file:
            logging.error(
                f"Timed out waiting for individual download for Google Photos item {google_id}"
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
                if downloaded_path.exists():
                    downloaded_path.unlink()
                continue

        target_name = downloaded_name
        resolved_target = (target_album_dir / target_name).resolve()

        if not _is_path_within(resolved_target, output_path):
            logging.error(f"Skipping downloaded file with unsafe path: {target_name}")
            failed_count += 1
            continue

        if downloaded_path.suffix.casefold() == ".zip":
            logging.error(
                f"Refusing ZIP download for Google Photos item {google_id}; ZIP downloads are disabled."
            )
            failed_count += 1
            if downloaded_path.exists():
                downloaded_path.unlink()
            continue

        if _path_conflicts_case_insensitive(resolved_target):
            conflicting_path = _find_conflicting_file_case_insensitive(resolved_target)
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
                    if downloaded_path.exists():
                        downloaded_path.unlink()
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
                    if downloaded_path.exists():
                        downloaded_path.unlink()
                    continue
                resolved_target = _ensure_unique_path(resolved_target)
                logging.debug(
                    f"No trusted pre-existing filename match found for Google Photos item {google_id} during bootstrap collision; saving as {resolved_target.name}."
                )

                if downloaded_path.resolve() != resolved_target:
                    _ = shutil.move(str(downloaded_path), str(resolved_target))

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
                owner_ids = path_to_google_ids.get(conflicting_path.resolve(), set())
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
                    if downloaded_path.exists():
                        downloaded_path.unlink()
                    continue

            target_name = _filename_with_google_id_suffix(downloaded_name, google_id)
            resolved_target = (target_album_dir / target_name).resolve()
            if not _is_path_within(resolved_target, output_path):
                logging.error(
                    f"Skipping downloaded file with unsafe path: {target_name}"
                )
                failed_count += 1
                if downloaded_path.exists():
                    downloaded_path.unlink()
                continue
            resolved_target = _ensure_unique_path(resolved_target)
            logging.debug(
                f"Filename collision for Google Photos item {google_id}; saving as {resolved_target.name}"
            )

        if downloaded_path.resolve() != resolved_target:
            _ = shutil.move(str(downloaded_path), str(resolved_target))

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
