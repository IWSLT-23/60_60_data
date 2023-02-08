#!/Users/borrison/miniconda3/envs/speech2text/bin/python3
# 2022 Cihan Xiao

"""
Script for extracting the m3u8 link from the website using selenium.
See the link below for reference of some code
https://www.geeksforgeeks.org/scraping-data-in-network-traffic-using-python/
"""

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.desired_capabilities import DesiredCapabilities
import json
from bs4 import BeautifulSoup
from urllib.parse import urlparse, parse_qs, unquote, urlunparse
import re
from pathlib import PurePosixPath
from utils import mkdir_if_not_exist
import os
import requests
from tqdm.auto import tqdm
from concurrent.futures import ThreadPoolExecutor, wait
from config import DATA_DIR, USER_AGENT
import itertools
import logging

# To fix the certificate error
import ssl
import certifi
from urllib.request import Request, urlopen
ssl._create_default_https_context = ssl._create_unverified_context


def download_metadata(data_dir, multilingual=True, session_id="all", mthread=10):
    # assert session_id in ["all", "1213", "1314", "1415", "1516"]

    # Prepare arguments for threading
    sessions = read_vp_links(data_dir)
    session_links = [list(session.values()) for session in sessions.values(
    )] if session_id == "all" else [list(sessions[session_id].values())]
    vp_links = list(itertools.chain.from_iterable(session_links))

    with ThreadPoolExecutor(max_workers=mthread) as pool:
        list(
            tqdm(
                pool.map(
                    get_speech_metadata,
                    vp_links,
                    [multilingual] * len(vp_links),
                    [data_dir] * len(vp_links)
                ),
                total=len(vp_links),
                position=0,
                leave=True,
            )
        )


def get_speech_metadata(vp_link, multilingual=True, data_dir=None):
    """
    @param vp_link: Link to the video page (in any language).
    @param multilingual: Returns chinese/english metadata as well.
    @param data_dir: If specified, the results will be stored as json.
    @return: A dictionary with language ID as key and the corresponding
    metadata list of tuples as value. If multilingual=True, all three
    languages will be returned, otherwise only the vp_link's language's
    metadata will be returned. The tuple is in the format:
    (label, video_time).
    """
    # Language tags in the url
    lang_tags = {"zh-hk": "can", "zh-cn": "man", "en-us": "eng"}

    options = Options()

    # Chrome will start in Headless mode
    options.add_argument("headless")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--ignore-certificate-errors")
    options.add_argument("--allow-running-insecure-content")

    # Crucial for the website to load videos
    user_agent = USER_AGENT
    options.add_argument(f"user-agent={user_agent}")

    # Start the chrome webdriver with executable path
    driver = webdriver.Chrome(
        service=Service(ChromeDriverManager().install()),
        options=options,
    )

    # The code below first extracts the language id's position
    # from the url, and replace it if multilingual
    parsed_url = urlparse(vp_link)
    mid = parse_qs(parsed_url.query)["MeetingID"][0]
    url_path = PurePosixPath(unquote(parsed_url.path))
    splitted_path = list(url_path.parts)
    lang_tag_pos = -1  # The position of the language string in the path
    base_lang = None
    for lang_tag, lang in lang_tags.items():
        if lang_tag in splitted_path:
            lang_tag_pos = splitted_path.index("zh-hk")
            base_lang = lang

    if not base_lang:
        raise ValueError("Invalid video page URL")

    results = {}
    vp_links = {base_lang: vp_link}

    if multilingual:
        vp_links = {}
        for lang_tag, lang in lang_tags.items():
            new_splitted_path = splitted_path
            new_splitted_path[lang_tag_pos] = lang_tag
            new_parsed_url = parsed_url
            vp_links[lang] = urlunparse(
                new_parsed_url._replace(
                    path=str(PurePosixPath(*new_splitted_path)))
            )

    # Extract the video time in the anonymous js function before it is converted
    # to the actual meeting time. This approach does not need to wait until the
    # video is loaded (which takes about 20s)
    time_regex = re.compile(r"(convertTimeToNum\(\')(\d\d:\d\d:\d\d)(\'\))")
    time_regex_2 = re.compile(r"(\")(\d{2}\:\d{2}\:\d{2})(\")")
    for lang, link in vp_links.items():
        results[lang] = []
        driver.get(link)
        driver.execute_script(f"openagenda('{mid}')")
        res = driver.page_source

        soup = BeautifulSoup(res, "html.parser")

        agenda = soup.find(
            lambda tag: tag.name == "div" and tag.has_attr(
                "id") and tag["id"] == "agenda_content"
        )
        rows = agenda.find_all("div", {"class": "row"})

        for row in rows:
            time_div = row.find(
                lambda tag: tag.name == "span"
                and tag.has_attr("style")
                and tag["style"] == "float: left; padding-right: 10px;"
            )

            # Get the text of the event/speaker
            label = row.find("div", {"class": "col-lg-8 col-6 nopadding"})
            # breakpoint()
            if label:
                onclick_func = time_div.find(
                    lambda tag: tag.name == "a" and tag.has_attr("onclick")
                )
                # Fix the layout change for 2012-2016 sessions
                if not time_regex.search(onclick_func["onclick"]):
                    results[lang].append(
                        (label.text, time_regex_2.search(
                            onclick_func["onclick"]).group(2)))
                else:
                    results[lang].append(
                        (label.text, time_regex.search(
                            onclick_func["onclick"]).group(2)))

    driver.close()
    driver.quit()

    if data_dir:
        metadata_path = os.path.join(data_dir, "metadata", mid)
        mkdir_if_not_exist(metadata_path)
        with open(os.path.join(metadata_path, "clips.json"), "w") as f:
            json.dump(results, f)

    return results


def get_playlist_m3u8_link(vp_link, multilingual=True):
    """
    @param vp_link: Link to the video page (in any language).
    @param multilingual: Returns chinese/english audio playlist link as well.
    @return: A dictionary with language ID as key and the corresponding
    playlist.m3u8 link as value. If multilingual=True, all three languages
    will be returned, otherwise only the Cantonese playlist link will be returned.
    """
    # Get MeetingID as a delimiter
    parsed_url = urlparse(vp_link)
    mid = parse_qs(parsed_url.query)["MeetingID"][0]

    # Enable performance logging to record network requests/responses
    desired_capabilities = DesiredCapabilities.CHROME
    desired_capabilities["goog:loggingPrefs"] = {"performance": "ALL"}

    options = Options()

    # Chrome will start in Headless mode
    options.add_argument("headless")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--ignore-certificate-errors")
    options.add_argument("--allow-running-insecure-content")

    # Crucial for the website to load videos
    user_agent = USER_AGENT
    options.add_argument(f"user-agent={user_agent}")

    # Start the chrome webdriver with executable path
    driver = webdriver.Chrome(
        # executable_path="./chromedriver",
        service=Service(ChromeDriverManager().install()),
        options=options,
        desired_capabilities=desired_capabilities,
    )

    # Send a request to the website and let it load
    driver.get(vp_link)
    WebDriverWait(driver, 20).until(
        EC.presence_of_element_located(
            (By.XPATH,
             "//div[@class='jw-icon jw-icon-inline jw-text jw-reset jw-text-duration']")
        )
    )

    # Gets all the logs from performance in Chrome
    logs = driver.get_log("performance")

    # The logic below is based on the obervation that the playlist.m3u8 link follows
    # the following pattern:
    # hk: https://5b4c10ababf6d.streamlock.net//VODonSAN/_definst_/s02/2016/10/mp4:M16100003_VC15.mp4/playlist.m3u8
    # cn: https://5b4c10ababf6d.streamlock.net//VODonSAN/_definst_/s02/2016/10/mp4:M16100003_VP15.mp4/playlist.m3u8
    # en: https://5b4c10ababf6d.streamlock.net//VODonSAN/_definst_/s02/2016/10/mp4:M16100003_VE15.mp4/playlist.m3u8
    # where the pattern is indicated by the variable in an element:
    # <span class="ctrl-group ctrl-onoff-on" data-ctrl-group="lang" data-value="C" id="ctrl-can2" tabindex="0">粵語</span>
    if multilingual:
        soup = BeautifulSoup(driver.page_source, "html.parser")
        hk_var = soup.find("span", {"id": "ctrl-can2"})["data-value"]
        cn_var = soup.find("span", {"id": "ctrl-pu2"})["data-value"]
        en_var = soup.find("span", {"id": "ctrl-eng2"})["data-value"]

    # Useful code snippet for saving a screenshot of the browser for debugging
    # driver.get_screenshot_as_file("screenshot.png")

    # Iterates every log and parses it using JSON
    result = {}
    for log in logs:
        network_log = json.loads(log["message"])["message"]

        # Filter logs to find only interested entries
        if (
            (
                "Network.response" in network_log["method"]
                or "Network.request" in network_log["method"]
            )
            and "request" in network_log["params"].keys()
            and "url" in network_log["params"]["request"]
            and network_log["params"]["request"]["url"].endswith("playlist.m3u8")
        ):
            playlist_link = network_log["params"]["request"]["url"]
            break

    driver.close()
    driver.quit()

    delim = mid + "_V"
    splitted = playlist_link.split(delim)
    result["can"] = "".join([splitted[0] + delim, hk_var + splitted[1][1:]])
    if multilingual:
        result["man"] = "".join(
            [splitted[0] + delim, cn_var + splitted[1][1:]])
        result["eng"] = "".join(
            [splitted[0] + delim, en_var + splitted[1][1:]])

    return result


def download_single_playlist_link(save_path, vp_link, multilingual=True):
    """
    Download a single playlist.m3u8 link given the video page link and store the
    results in a specified path.
    @param save_path: The path in which the m3u8 link will be stored.
    @param vp_link: The link to the video page.
    @param multilingual: Download multilingual m3u8 links if True.
    """
    results = get_playlist_m3u8_link(
        vp_link=vp_link, multilingual=multilingual)
    with open(save_path, "w") as f:
        json.dump(results, f)


def download_playtlist_m3u8_links(data_dir, multilingual=True, mthread=1):
    """
    Download all playlist.m3u8 links based on the video page links stored in the
    metadata directory (data_dir/metadata/vp_links.json).
    Note that the download_vp_links function must be executed first to retrieve
    the vp_links. Also note that the results will be stored in the file
    data_dir/metadata/global/playlists.json

    @param data_dir: The data directory to store and extract data/metadata.
    @param multilingual: Download multilingual m3u8 links if True.
    @param mthread: The number of threads used to download the links.
    """
    vp_links = read_vp_links(data_dir)
    save_path = os.path.join(data_dir, "metadata", "global", "playlists.json")
    tmp_path = os.path.join(data_dir, "metadata", "global", "tmp")
    session_dirs = {}
    results = {}
    os.environ["WDM_LOG"] = "0"  # Disable webdriver-manager logging

    with ThreadPoolExecutor(max_workers=mthread) as pool:
        param_save_paths = []
        param_vp_links = []
        for session, meetings in vp_links.items():
            # Create parameters for the threadpool's map function
            session_tmp_path = os.path.join(tmp_path, session)
            session_dirs[session] = session_tmp_path
            mkdir_if_not_exist(session_tmp_path)
            for mid, vp_link in meetings.items():
                # Download and store links separately
                save_file_path = os.path.join(session_tmp_path, mid + ".json")
                param_save_paths.append(save_file_path)
                param_vp_links.append(vp_link)

        list(
            tqdm(
                pool.map(
                    download_single_playlist_link,
                    param_save_paths,
                    param_vp_links,
                    [multilingual] * len(param_save_paths),
                ),
                total=len(param_save_paths),
                position=0,
                leave=True,
            )
        )

    # Merge session-level results, e.g. 1617 represents the 2016-2017 session
    session_results = {}
    for session, session_dir in session_dirs.items():
        session_result = {}
        for fname in os.listdir(session_dir):
            if fname.endswith(".json"):
                file_path = os.path.join(session_dir, fname)
                with open(os.path.join(session_dir, fname), "r") as f:
                    session_result[fname.split(".json")[0]] = json.load(f)
                os.remove(file_path)
        os.rmdir(session_dir)
        session_results[session] = session_result

    # Store results in the file data_dir/metadata/global/playlists.json
    with open(save_path, "w") as f:
        json.dump(session_results, f)
    os.rmdir(tmp_path)


def get_video_page_link(index_page_link):
    """
    Find all links to video pages given the index page link.
    @param index_page_link: The link to the index page, e.g.
    https://www.legco.gov.hk/general/chinese/counmtg/yr16-20/mtg_1617.htm#toptbl
    @return: A dictionary with the MeetingID as the key and video page link as value.
    """
    options = Options()

    # Chrome will start in Headless mode
    options.add_argument("headless")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--ignore-certificate-errors")
    options.add_argument("--allow-running-insecure-content")

    # Crucial for the website to load videos
    user_agent = USER_AGENT
    options.add_argument(f"user-agent={user_agent}")

    # Start the chrome webdriver with executable path
    driver = webdriver.Chrome(
        service=Service(ChromeDriverManager().install()),
        options=options,
    )

    # Send a request to the website and let it load
    driver.get(index_page_link)
    res = driver.page_source

    soup = BeautifulSoup(res, "html.parser")

    # Get page langauge
    # lang = soup.find("html").attrs["lang"]

    table = soup.find(
        lambda tag: tag.name == "table" and tag.has_attr(
            "border") and tag["border"] == "1"
    )
    rows = table.find_all(lambda tag: tag.name == "tr")
    results = {}
    for row in rows:
        found_vp_links = row.find_all("a", {"class": "webcast_link"})
        for found_vp_link in found_vp_links:
            if "href" in found_vp_link.attrs:
                parsed_url = urlparse(found_vp_link["href"])
                mid = parse_qs(parsed_url.query)["MeetingID"][0]
                results[mid] = found_vp_link["href"]

    driver.close()
    driver.quit()

    return results


def read_index_page_links(data_dir):
    """
    Read the index page links from disk. Note that this information is collected manually
    and stored at data_dir/metadata/global/index_page_links.json.
    @return: A dictionary with the format {session, vp_link},e.g.
    {"1617": "https://www.legco.gov.hk/general/chinese/counmtg/yr16-20/mtg_1617.htm#toptbl"}
    """
    ip_links_path = os.path.join(
        data_dir, "metadata", "global", "index_page_links.json")
    if not os.path.exists(ip_links_path):
        raise ValueError(f"ERROR: index_page_links.json not found")

    with open(ip_links_path, "r") as f:
        results = json.load(f)

    return results


def read_vp_links(data_dir):
    """
    Read the video page links from disk.
    """
    vp_links_path = os.path.join(
        data_dir, "metadata", "global", "vp_links.json")
    if not os.path.exists(vp_links_path):
        raise ValueError(f"ERROR: vp_links.json not found")

    with open(vp_links_path, "r") as f:
        results = json.load(f)

    return results


def download_vp_links(data_dir):
    """
    Download the video page links and store the results in data_dir/metadata/global/vp_links.json.
    Note that the index page links must be stored already in the metadata directory.
    The json object stored is in the format {session: {mid: vp_link}}.
    """
    ip_links = read_index_page_links(data_dir)
    save_path = os.path.join(data_dir, "metadata", "global", "vp_links.json")
    results = {}

    for session, vp_link in ip_links.items():
        results[session] = get_video_page_link(vp_link)

    with open(save_path, "w") as f:
        json.dump(results, f)


def download_session_scripts(index_page_link, data_dir):
    """
    Download all pdf scripts given the index page link.
    @param index_page_link: The link to the index page, e.g.
    https://www.legco.gov.hk/general/chinese/counmtg/yr16-20/mtg_1617.htm#toptbl
    The downloaded pdf will be stored at data_dir/txt/mid/lang/mid_lang.pdf, e.g.
    ../data/txt/M16100003/eng/M16100003_eng.pdf
    """
    root_domain = urlparse(index_page_link).hostname

    options = Options()

    # Chrome will start in Headless mode
    options.add_argument("headless")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--ignore-certificate-errors")
    options.add_argument("--allow-running-insecure-content")

    # Crucial for the website to load videos
    user_agent = USER_AGENT
    options.add_argument(f"user-agent={user_agent}")

    # Start the chrome webdriver with executable path
    driver = webdriver.Chrome(
        service=Service(ChromeDriverManager().install()),
        options=options,
    )

    # Send a request to the website and let it load
    driver.get(index_page_link)
    res = driver.page_source

    soup = BeautifulSoup(res, "html.parser")

    table = soup.find(
        lambda tag: tag.name == "table" and tag.has_attr(
            "border") and tag["border"] == "1"
    )
    rows = table.find_all(lambda tag: tag.name == "tr")

    headers = {"User-Agent": USER_AGENT}
    txt_path = os.path.join(data_dir, "txt")

    for row in tqdm(rows):
        td_cells = row.find_all("td", {"valign": "top", "align": "center"})
        # Valid rows contain 4 cells with centering format
        if len(td_cells) < 4:
            continue
        can_script_cell = td_cells[-1]
        eng_script_cell = td_cells[-2]
        can_script_page_as = can_script_cell.find_all(
            lambda tag: tag.name == "a"
            and tag.has_attr("href")
            and not tag["href"].endswith(".pdf")
        )
        eng_script_page_as = eng_script_cell.find_all(
            lambda tag: tag.name == "a"
            and tag.has_attr("href")
            and not tag["href"].endswith(".pdf")
        )
        can_script_page_links = [
            "https://" + root_domain + a["href"] for a in can_script_page_as]
        eng_script_page_links = [
            "https://" + root_domain + a["href"].replace("chinese", "english")
            for a in eng_script_page_as
        ]

        for i, script_page_link in enumerate(can_script_page_links):
            # Using script date as identifier to resolve video-script many-to-one mapping
            parsed_script_url = urlparse(script_page_link)
            script_date = parse_qs(parsed_script_url.query)["date"][0]
            driver.get(script_page_link)
            WebDriverWait(driver, 3).until(
                EC.presence_of_element_located(
                    (By.XPATH, "//a[@class='pdf-links item1']"))
            )
            sp_soup = BeautifulSoup(driver.page_source, "html.parser")
            pdf_link_a = sp_soup.find("a", {"class": "pdf-links item1"})
            pdf_link = "https:" + pdf_link_a["href"]

            # pdf_res = requests.get(pdf_link, headers=headers)
            request = Request(pdf_link, headers=headers)
            pdf_res = urlopen(
                request, context=ssl._create_default_https_context(cafile=certifi.where()))
            save_dir = os.path.join(txt_path, script_date, "can")
            mkdir_if_not_exist(save_dir)
            save_file = os.path.join(save_dir, script_date + "_can.pdf")
            with open(save_file, "wb") as f:
                # f.write(pdf_res.content)
                f.write(pdf_res.read())

        for i, script_page_link in enumerate(eng_script_page_links):
            # Using script date as identifier to resolve video-script many-to-one mapping
            parsed_script_url = urlparse(script_page_link)
            script_date = parse_qs(parsed_script_url.query)["date"][0]
            driver.get(script_page_link)
            WebDriverWait(driver, 3).until(
                EC.presence_of_element_located(
                    (By.XPATH, "//a[@class='pdf-links item1']"))
            )
            sp_soup = BeautifulSoup(driver.page_source, "html.parser")
            pdf_link_a = sp_soup.find("a", {"class": "pdf-links item1"})
            pdf_link = "https:" + pdf_link_a["href"]

            # pdf_res = requests.get(pdf_link, headers=headers)
            request = Request(pdf_link, headers=headers)
            pdf_res = urlopen(
                request, context=ssl._create_default_https_context(cafile=certifi.where()))
            save_dir = os.path.join(txt_path, script_date, "eng")
            mkdir_if_not_exist(save_dir)
            save_file = os.path.join(save_dir, script_date + "_eng.pdf")
            with open(save_file, "wb") as f:
                # f.write(pdf_res.content)
                f.write(pdf_res.read())

    driver.close()
    driver.quit()


def download_target_scripts(data_dir, target_sessions="all", mthread=5):
    """
    Download all sessions scripts.
    @param data_dir: The data directory for storage and reading pre-stored index pages.
    @param target_sessions: The list of target sessions. Default is all.
    """
    target_sessions = (
        [target_sessions]
        if target_sessions != "all" and not isinstance(target_sessions, list)
        else target_sessions
    )
    mthread = min(len(target_sessions),
                  mthread) if target_sessions != "all" else mthread
    ip_links = read_index_page_links(data_dir=data_dir)
    with ThreadPoolExecutor(max_workers=mthread) as pool:
        futures = []
        for session, ip_link in ip_links.items():
            if session in target_sessions and target_sessions != "all" or target_sessions == "all":
                futures.append(pool.submit(
                    download_session_scripts, ip_link, data_dir))

        # For exception passing
        results = wait(futures)
        for result in results.done:
            if result.exception() is not None:
                raise result.exception()


def get_video_dates(eng_index_page_link):
    """
    Get the mapping between meeting date and meeting id.
    @param eng_index_page_link: The english index page link.
    @return: A dictionary {mid: date}.
    """
    options = Options()

    # Chrome will start in Headless mode
    options.add_argument("headless")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--ignore-certificate-errors")
    options.add_argument("--allow-running-insecure-content")

    # Crucial for the website to load videos
    user_agent = USER_AGENT
    options.add_argument(f"user-agent={user_agent}")

    # Start the chrome webdriver with executable path
    driver = webdriver.Chrome(
        service=Service(ChromeDriverManager().install()),
        options=options,
    )

    # Send a request to the website and let it load
    driver.get(eng_index_page_link)
    res = driver.page_source

    soup = BeautifulSoup(res, "html.parser")

    table = soup.find(
        lambda tag: tag.name == "table" and tag.has_attr(
            "border") and tag["border"] == "1"
    )
    rows = table.find_all(lambda tag: tag.name == "tr")
    results = {}

    # Using English index page for simplicity, allowing for simple regex matching of
    # d(d).m(m).yyyy
    date_regex = re.compile(r"(\d{1,2}).(\d{1,2}).(\d\d\d\d)$")
    for row in rows:
        found_vp_links = row.find_all("a", {"class": "webcast_link"})
        for found_vp_link in found_vp_links:
            if "href" in found_vp_link.attrs:
                parsed_url = urlparse(found_vp_link["href"])
                mid = parse_qs(parsed_url.query)["MeetingID"][0]
                title = found_vp_link.find(lambda tag: tag.name == "img" and tag.has_attr("title"))[
                    "title"
                ]
                date = "-".join(
                    [
                        date_regex.search(title).group(3),
                        date_regex.search(title).group(2).zfill(2),
                        date_regex.search(title).group(1).zfill(2),
                    ]
                )
                results[mid] = date

    driver.close()
    driver.quit()

    return results


def download_all_video_dates(data_dir):
    """
    Download, parse and save all videos' meeting date (all sessions).
    @param data_dir: The data directory in which to store the metadata.
    """
    metadata_dir = os.path.join(data_dir, "metadata", "global")
    ip_links = read_index_page_links(data_dir=data_dir)
    results = {}

    for _, ip_link in ip_links.items():
        results = {**results, **
                   get_video_dates(ip_link.replace("chinese", "english"))}

    with open(os.path.join(metadata_dir, "dates.json"), "w") as f:
        json.dump(results, f)


def main():
    logging.basicConfig(
        level="INFO",
        format="%(asctime)s (%(module)s:%(lineno)d) %(levelname)s: %(message)s",
    )

    # Suppress webdirver from logging
    os.environ['WDM_LOG'] = '0'

    # sample_index_page_link = (
    #     "https://www.legco.gov.hk/general/chinese/counmtg/yr16-20/mtg_1617.htm#toptbl"
    # )
    # Download 2012-2016 data
    # sample_index_page_link = (
    #     "https://www.legco.gov.hk/general/chinese/counmtg/yr12-16/mtg_1516.htm"
    # )
    # sample_vp_links = get_video_page_link(sample_index_page_link)
    # download_session_scripts(sample_index_page_link, data_dir=DATA_DIR)
    # download_target_scripts(DATA_DIR, target_sessions="1617")
    # download_target_scripts(DATA_DIR)
    # download_all_video_dates(DATA_DIR)

    # sample_vp_link = list(sample_vp_links.keys())[0]
    # sample_vp_link = "http://webcast.legco.gov.hk/public/zh-hk/SearchResult?MeetingID=M16100003"
    # print(get_playlist_m3u8_link(sample_vp_link))
    # print(get_speech_metadata(sample_vp_link, data_dir=DATA_DIR))

    # download_vp_links(DATA_DIR)
    # download_playtlist_m3u8_links(DATA_DIR, multilingual=True, mthread=6)
    download_metadata(DATA_DIR, multilingual=True,
                      session_id="all", mthread=10)

    # sample_vp_link2 = "http://webcast.legco.gov.hk/public/zh-hk/SearchResult?MeetingID=M15100012"
    # print(get_speech_metadata(sample_vp_link2, data_dir=DATA_DIR))


if __name__ == "__main__":
    main()
