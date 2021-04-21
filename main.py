import fnmatch
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from datetime import datetime
from io import BytesIO

import yaml
from PIL import Image, ImageStat

error_list = []
theme_id = None

get_middle_item = lambda image_list: image_list[len(image_list) // 2] if image_list else -1


def find_new_themes():
    new_themes = []
    theme_data = load_theme_db()

    for filename in glob.glob("themes/*.yaml"):
        with open(filename, 'r') as fileobj:
            theme_list = yaml.safe_load(fileobj)
            if not theme_list:
                continue

            for theme_id, theme_urls in theme_list.items():
                if theme_id not in theme_data or theme_urls[0] != theme_data[theme_id]["themeUrl"]:
                    theme_type = os.path.basename(os.path.splitext(filename)[0])
                    new_themes.append((theme_id, theme_urls[0], theme_type))

                    if not re.match(r"^[\w-]+$", theme_id):
                        add_error(f"Theme ID '{theme_id}' contains invalid characters (only alphanumeric, hyphen, and underscore allowed)")

    if not new_themes:
        print("No new themes found in this PR")
        sys.exit(0)
    else:
        print(f"{len(new_themes)} new theme(s) found: " + ", ".join(theme[0] for theme in new_themes))
        print(f"::set-output name=changed::true")
        return new_themes


def load_theme_db():
    if not os.path.isfile("theme-db.json"):
        return {}

    with open("theme-db.json", 'r') as fileobj:
        return json.load(fileobj)


def print_errors_and_exit():
    for error in error_list:
        print(f"::error ::{error}")
    sys.exit(len(error_list))


def add_error(message, is_fatal=False):
    if theme_id:
        message = f"[{theme_id}] {message}"
    error_list.append(message)
    if is_fatal:
        sys.exit()


def setup_env(theme_url):
    if os.path.isdir("temp"):
        shutil.rmtree("temp")
    os.mkdir("temp")

    print(f"Downloading {theme_url}...")
    theme_path, date_modified = download_theme(theme_url)

    os.mkdir("temp/unzipped")
    print(f"Extracting {os.path.basename(theme_path)}...")
    extract_theme(theme_path)

    return theme_path, date_modified


def download_theme(theme_url):
    try:
        subprocess.run("bash ../mediafire-direct-dl.sh " + theme_url, cwd="temp", shell=True)
        mediafire_id, filename = re.search(r"mediafire\.com/file/(.+?)/(.+?)/file", theme_url).groups()

        if os.path.splitext(filename)[1] != ".ddw":
            add_error("Theme URL is not a MediaFire download (must be a .ddw file)", True)

        filename = "temp/" + filename
        date_modified = datetime.utcfromtimestamp(os.path.getmtime(filename))
        return filename, date_modified
    except Exception as e:
        add_error(f"Failed to download theme from {theme_url}: {e}", True)


def extract_theme(theme_path):
    try:
        subprocess.run(["unzip", theme_path, "-d", "temp/unzipped"])
    except Exception as e:
        add_error(f"Failed to extract theme from {theme_path}: {e}", True)


def load_theme_config():
    json_path = next(glob.iglob("temp/unzipped/*.json"), None)
    if not json_path:
        add_error("Theme package does not contain theme.json file", True)

    try:
        with open(json_path, 'r') as fileobj:
            theme_config = json.load(fileobj)
        print(theme_config)
        return theme_config
    except Exception as e:
        add_error(f"Failed to load JSON file {os.path.basename(json_path)}: {e}", True)


def on_pull_request(theme_id, theme_url, theme_type):
    setup_env(theme_url)
    theme_config = load_theme_config()
    validate_theme_config(theme_config)
    validate_theme_files(theme_config)
    validate_image_size(theme_config)
    validate_image_brightness(theme_config)


def validate_theme_config(theme_config):
    missing_keys = []
    for key in ("dayImageList", "imageCredits", "imageFilename", "nightImageList"):
        if not theme_config.get(key):
            missing_keys.append(key)
    if missing_keys:
        add_error(f"Required keys are missing from theme.json: " + ", ".join(missing_keys), True)


def validate_theme_files(theme_config):
    extra_paths = []
    json_found = False
    for filename in os.listdir("temp/unzipped"):
        if (not fnmatch.fnmatch(filename, theme_config["imageFilename"]) and
                not (filename.endswith(".json") and not json_found)):
            extra_paths.append(filename)
        if filename.endswith(".json"):
            json_found = True
    if extra_paths:
        add_error(f"Unused files in theme package: " + ", ".join(extra_paths))

    image_ids = []
    if theme_config.get("sunriseImageList"):
        image_ids.extend(theme_config["sunriseImageList"])
    image_ids.extend(theme_config["dayImageList"])
    if theme_config.get("sunsetImageList"):
        image_ids.extend(theme_config["sunsetImageList"])
    image_ids.extend(theme_config["nightImageList"])
    missing_paths = []
    for img_id in image_ids:
        img_path = theme_config["imageFilename"].replace("*", str(img_id))
        if not os.path.isfile(f"temp/unzipped/{img_path}"):
            missing_paths.append(img_path)
    if missing_paths:
        add_error(f"Missing image files in theme package: " + ", ".join(missing_paths), True)


def validate_image_size(theme_config):
    image_filename = theme_config["imageFilename"].replace("*", str(theme_config["dayImageList"][0]))
    img = Image.open(f"temp/unzipped/{image_filename}")
    w, h = img.size
    if w < 1920 or h < 1080:
        add_error("Image size is too small (must be at least 1920x1080)")

    if w < h:
        add_error("Image orientation is portrait (must be landscape or square)")


def validate_image_brightness(theme_config):
    expected_light_id = theme_config.get("dayHighlight") or get_middle_item(theme_config["dayImageList"])
    expected_dark_id = theme_config.get("nightHighlight") or get_middle_item(theme_config["nightImageList"])
    image_pattern = "temp/unzipped/" + theme_config["imageFilename"]
    star_lindex = image_pattern.index("*")
    star_rindex = len(image_pattern) - star_lindex - 1
    image_filenames = sorted(glob.glob(image_pattern), key=lambda x: int(x[star_lindex:-star_rindex]))
    image_data = []
    for filename in image_filenames:
        img = Image.open(filename).convert("L")
        img_stat = ImageStat.Stat(img)
        image_data.append(img_stat.mean[0])
    actual_light_id = image_data.index(max(image_data)) + 1
    actual_dark_id = image_data.index(min(image_data)) + 1
    # if not (expected_light_id - 1 <= actual_light_id <= expected_light_id + 1):
    #     add_error(f"Brightest image is {actual_light_id}, expected {expected_light_id}")

    # if not (expected_dark_id - 1 <= actual_dark_id <= expected_dark_id + 1):
    #     add_error(f"Darkest image is {actual_dark_id}, expected {expected_dark_id}")


def on_push_to_master(theme_id, theme_url, theme_type):
    theme_path, date_modified = setup_env(theme_url)
    theme_config = load_theme_config()
    theme_data = load_theme_db()

    with open(theme_path, 'rb') as fileobj:
        ddw_data = fileobj.read()

    theme_data[theme_id] = {
        "themeUrl": theme_url,
        "themeType": theme_type,
        "displayName": theme_config.get("displayName"),
        "imageCredits": theme_config.get("imageCredits"),
        "fileHash": hashlib.md5(ddw_data).hexdigest(),
        "fileSize": os.path.getsize(theme_path),
        "dateAdded": str(date_modified.date()),
        "imageSize": make_thumbnails(theme_config),
        "sunPhases": make_previews(theme_config)
    }

    save_theme_db(theme_data)


def resize_16x9(img, width):
    w, h = img.size
    if ((w / h) > (16 / 9)):  # Too wide
        w2 = int(h * 16 / 9)
        x = (w - w2) / 2
        img = img.crop((x, 0, x + w2, h))
    elif ((w / h) < (16 / 9)):  # Too high
        h2 = int(w * 9 / 16)
        y = (h - h2) / 2
        img = img.crop((0, y, w, y + h2))
    return img.resize((width, int(width * 9 / 16)))


def make_previews(theme_config):
    os.makedirs("out/previews", exist_ok=True)

    sunrise_image_id = get_middle_item(theme_config.get("sunriseImageList"))
    day_image_id = theme_config.get("dayHighlight") or get_middle_item(theme_config["dayImageList"])
    sunset_image_id = get_middle_item(theme_config.get("sunsetImageList"))
    night_image_id = theme_config.get("nightHighlight") or get_middle_item(theme_config["nightImageList"])

    image_filenames = {}
    if sunrise_image_id != -1 and sunrise_image_id != day_image_id and sunrise_image_id != night_image_id:
        image_filenames["sunrise"] = theme_config["imageFilename"].replace("*", str(sunrise_image_id))
    image_filenames["day"] = theme_config["imageFilename"].replace("*", str(day_image_id))
    if sunset_image_id != -1 and sunset_image_id != day_image_id and sunset_image_id != night_image_id:
        image_filenames["sunset"] = theme_config["imageFilename"].replace("*", str(sunset_image_id))
    image_filenames["night"] = theme_config["imageFilename"].replace("*", str(night_image_id))

    for phase, filename in image_filenames.items():
        img = Image.open(f"temp/unzipped/{filename}")
        img = resize_16x9(img, 1920)
        img.save(f"out/previews/{theme_id}_{phase}.jpg", quality=75)

    return list(image_filenames.keys())


def make_thumbnails(theme_config, ddw_path=None):
    os.makedirs("out/thumbnails", exist_ok=True)

    light_image_id = theme_config.get("dayHighlight") or get_middle_item(theme_config["dayImageList"])
    light_image_filename = theme_config["imageFilename"].replace("*", str(light_image_id))
    dark_image_id = theme_config.get("nightHighlight") or get_middle_item(theme_config["nightImageList"])
    dark_image_filename = theme_config["imageFilename"].replace("*", str(dark_image_id))

    if not ddw_path:
        img1 = Image.open(f"temp/unzipped/{light_image_filename}")
        img2 = Image.open(f"temp/unzipped/{dark_image_filename}")
    else:
        with zipfile.ZipFile(ddw_path, 'r') as zipobj:
            img1 = Image.open(BytesIO(zipobj.read(light_image_filename)))
            img2 = Image.open(BytesIO(zipobj.read(dark_image_filename)))

    w, h = img1.size
    img1 = resize_16x9(img1, 384)
    img1.save(f"out/thumbnails/{theme_id}_day.png")
    img2 = resize_16x9(img2, 384)
    img2.save(f"out/thumbnails/{theme_id}_night.png")

    return (w, h)


def save_theme_db(theme_data):
    get_theme_key = lambda ti, td: td.get("displayName") or ti.replace(" ", "_")
    theme_db = dict(sorted(theme_data.items(), key=lambda theme: get_theme_key(*theme).lower()))
    with open("theme-db.json", 'w', newline='\n') as fileobj:
        json.dump(theme_db, fileobj, indent=4)


def process_private_themes():
    global theme_id
    private_themes = glob.glob("private/**/*.ddw", recursive=True)
    if not private_themes:
        error_list.append("No private themes found")
        print_errors_and_exit()

    with open("themes/_paid.yaml", 'r') as fileobj:
        theme_list = yaml.safe_load(fileobj)
    theme_data = load_theme_db()

    for ddw_path in private_themes:
        print(f"Processing {os.path.basename(ddw_path)}...")
        theme_id = os.path.splitext(os.path.basename(ddw_path))[0]

        with zipfile.ZipFile(ddw_path, 'r') as zipobj:
            with zipobj.open(f"{theme_id}.json", 'r') as fileobj:
                theme_config = json.load(fileobj)

        display_name = theme_config["displayName"]
        if "free" in ddw_path:
            display_name = f"24 Hour {display_name}"

        with open(ddw_path, 'rb') as fileobj:
            ddw_data = fileobj.read()

        theme_data[theme_id] = {
            "themeUrl": theme_list[theme_id][0],
            "themeType": "photos" if "free" in ddw_path else "paid",
            "displayName": display_name,
            "imageCredits": theme_config["imageCredits"],
            "fileHash": hashlib.md5(ddw_data).hexdigest(),
            "fileSize": os.path.getsize(ddw_path),
            "dateAdded": str(datetime.utcfromtimestamp(os.path.getmtime(ddw_path)).date()),
            "imageSize": make_thumbnails(theme_config, ddw_path),
            "sunPhases": None
        }

    save_theme_db(theme_data)


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else None
    if action == "pull_request":
        for theme in find_new_themes():
            theme_id = theme[0]
            try:
                on_pull_request(*theme)
            except SystemExit:
                pass
    elif action == "push":
        for theme in find_new_themes():
            theme_id = theme[0]
            try:
                on_push_to_master(*theme)
            except SystemExit:
                pass
    else:
        process_private_themes()

    print_errors_and_exit()
