import os
import sys
import logging
import mergin
import filecmp
import shutil
import tempfile
import json
import threading
import time

# Configure logging
logger = logging.getLogger('mergin_script')
logger.setLevel(logging.DEBUG)
sh = logging.StreamHandler(sys.stdout)
sh.setLevel(logging.DEBUG)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
sh.setFormatter(formatter)
logger.addHandler(sh)

def remove_path(path):
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path)
    else:
        os.unlink(path)


def sync_directory(source, destination):
    if os.path.islink(source) or os.path.islink(destination):
        raise ValueError(f"Refusing to sync symbolic link: {destination}")
    os.makedirs(destination, exist_ok=True)
    source_names = set(os.listdir(source))
    destination_names = set(os.listdir(destination))

    for name in sorted(destination_names - source_names):
        target = os.path.join(destination, name)
        logger.debug(f"Removing {target}")
        remove_path(target)

    for name in sorted(source_names, key=lambda name: (name == '.mergin', name)):
        incoming = os.path.join(source, name)
        target = os.path.join(destination, name)
        if os.path.islink(incoming) or os.path.islink(target):
            raise ValueError(f"Refusing to sync symbolic link: {target}")
        if os.path.isdir(incoming):
            if os.path.exists(target) and not os.path.isdir(target):
                remove_path(target)
            sync_directory(incoming, target)
        elif os.path.isfile(incoming):
            if os.path.isfile(target) and filecmp.cmp(incoming, target, shallow=False):
                continue
            if os.path.isdir(target):
                remove_path(target)
            logger.debug(f"Updating {target}")
            os.replace(incoming, target)
        else:
            raise ValueError(f"Unsupported downloaded file: {incoming}")


def is_up_to_date(destination, remote):
    metadata_path = os.path.join(destination, '.mergin', 'mergin.json')
    try:
        with open(metadata_path, encoding='utf-8') as metadata_file:
            local = json.load(metadata_file)
    except FileNotFoundError:
        return False
    except (OSError, ValueError):
        logger.warning(f"Cannot read metadata for {destination}; downloading again")
        return False
    if not isinstance(local, dict):
        return False
    version = remote.get('version')
    return (isinstance(version, str) and bool(version)
            and local.get('version') == version
            and local.get('id') == remote.get('id'))


def download_with_progress(client, project_path, directory, version):
    started = time.monotonic()
    finished = threading.Event()

    def report_progress():
        while not finished.wait(30):
            logger.info(f"Still downloading {project_path}: {time.monotonic() - started:.0f}s elapsed")

    reporter = threading.Thread(target=report_progress, daemon=True)
    logger.info(f"Downloading {project_path} ({version or 'latest'})")
    reporter.start()
    try:
        client.download_project(project_path=project_path, directory=directory, version=version)
    finally:
        finished.set()
        reporter.join()
    logger.info(f"Downloaded {project_path} in {time.monotonic() - started:.1f}s")


def main():
    logger.debug("Starting script...")
    url = os.getenv('MERGIN_URL')
    login = os.getenv('MERGIN_USERNAME')
    password = os.getenv('MERGIN_PASSWORD')
    data = os.getenv('MERGIN_DATA')

    if not all([url, login, password, data]):
        logger.error("Missing environment variables: MERGIN_URL, MERGIN_USERNAME, MERGIN_PASSWORD, or MERGIN_DATA")
        return 1

    try:
        client = mergin.MerginClient(url=url, login=login, password=password)
        projects = client.projects_list()
        os.makedirs(data, exist_ok=True)
    except Exception:
        logger.exception("Failed to initialize Mergin sync")
        return 1

    started = time.monotonic()
    failed = 0
    skipped = 0
    synced = 0
    for index, project in enumerate(projects, start=1):
        project_path = os.path.join(project['namespace'], project['name'])
        dest_path = os.path.join(data, project_path)
        logger.info(f"[{index}/{len(projects)}] Checking {project_path}")
        try:
            remote = client.project_info(project_path)
            if is_up_to_date(dest_path, remote):
                logger.info(f"Skipping {project_path}: already at {remote['version']}")
                skipped += 1
                continue
            with tempfile.TemporaryDirectory(prefix='.mergin-sync-', dir=data) as temporary:
                temp_path = os.path.join(temporary, 'project')
                download_with_progress(client, project_path, temp_path, remote.get('version'))
                logger.info(f"Comparing and syncing {project_path}")
                filecmp.clear_cache()
                sync_directory(temp_path, dest_path)
            synced += 1
            logger.info(f"Finished {project_path}")
        except Exception:
            failed += 1
            logger.exception(f"Error processing {dest_path}")
    logger.info(f"Sync finished: {synced} synced, {skipped} skipped, {failed} failed "
                f"in {time.monotonic() - started:.1f}s")
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())