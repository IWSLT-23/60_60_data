#!/home/cxiao7/miniconda3/envs/speech2text/bin/python3
# 2022 Cihan Xiao

from pathlib import Path
import requests
import os
from glob import glob
import m3u8
from tqdm import tqdm
from concurrent.futures import ThreadPoolExecutor
from utils import mkdir_if_not_exist, read_video_dates
import json
from config import DATA_DIR, MTHREAD
import time
import datetime
import argparse


def read_playlists(data_dir):
    """
    Read playlist.m3u8 links from the metadata directory.
    @return: The playlist.m3u8 links in the format {session: {mid: {lang: link}}}.
    """
    results = {}
    playlists_path = os.path.join(
        data_dir, "metadata", "global", "playlists.json")

    with open(playlists_path, "r") as f:
        results = json.load(f)

    return results


def ts_fname_sort_func(ts_fname):
    """
    Key function that sorts the .ts files by its indices.
    e.g.
    media_w1676455105_2.ts -> 2,
    media_w1676455105_13.ts -> 13, etc.
    """
    return int(ts_fname.strip(".ts").split("_")[-1])


def merge_ts(fname, download_path, rm_tmp=True):
    read_path = os.path.join(download_path, "tmp")
    write_path = os.path.join(download_path, fname)
    files = sorted(glob(os.path.join(read_path, "*.ts")),
                   key=ts_fname_sort_func)

    size = 0
    # Read from .ts files and write into a single .mp4 file
    with open(write_path, "wb") as fw:
        for file in files:
            with open(file, "rb") as fr:
                data = fr.read()
                fw.write(data)
                size += len(data)
            if rm_tmp:
                os.remove(file)
    if rm_tmp:
        os.rmdir(read_path)

    print(f"Merged {len(files)} files with total size {size/1024/1024:.2f}MB")


def download_segment(seg, tmp_path):
    """
    Download a single video segment (.ts) to the directory specified by tmp_path.
    """
    fname = os.path.join(tmp_path, seg.uri)

    res = requests.get(seg.absolute_uri)
    data = res.content

    with open(fname, "wb") as f:
        f.write(data)

    return len(data)


def download_from_playlist_m3u8(
    link, mid, data_dir, lang="can", mthread=10, merge=True, log_progress=True,
    proglog=None,
):
    """
    Download a video using the provided playlist.m3u8 link.
    @param link: The playlist.m3u8 link of the video.
    @param mid: The meeting id of the video.
    @param data_dir: The data directory to store and extract data/metadata.
    @param lang: The language of the video.
    @param mthread: The number of thread used for downloading, default is 10.
    @param merge: Whether the downloaded .ts files will be merged into a full video, default is True.
    @param log_progress: Whether the download progress will be logged, default is True.
    """
    print(f"Downloading {mid}_{lang} with {mthread} threads...")

    # Define the download location
    download_path = os.path.join(data_dir, "video", mid, lang)
    mkdir_if_not_exist(download_path)

    # Define the tmp location for storing .ts segments
    tmp_path = os.path.join(download_path, "tmp")
    mkdir_if_not_exist(tmp_path)

    # Parse the playlist.m3u8 from the provided link
    playlist = m3u8.load(link)

    # Extract the chunklist m3u8 link that contains the actual segments
    sublinks = []
    for sublist in playlist.playlists:
        sublinks.append(sublist.absolute_uri)

    # Download all segments from each chunklist
    for i, sublink in enumerate(sublinks):
        sublist = m3u8.load(sublink)
        size = 0
        # Single thread downloading
        if mthread == 1:
            for j, seg in tqdm(enumerate(sublist.segments)):
                size += download_segment(seg, tmp_path)
        # Multi-thread downloading
        elif mthread > 1:
            with ThreadPoolExecutor(max_workers=mthread) as pool:
                list(
                    tqdm(
                        pool.map(
                            download_segment,
                            sublist.segments,
                            [tmp_path] * len(sublist.segments),
                        ),
                        total=len(sublist.segments),
                    )
                )

    fname = "_".join([mid, lang]) + ".mp4"

    # Merge the .ts files
    if merge:
        merge_ts(fname, download_path)

    # The downloading progress will be stored at data_dir/metadata/global/downloaded.json
    if log_progress:
        downloaded_fname = os.path.join(
            data_dir, "metadata", "global", "downloaded.json") if not proglog else proglog
        if not os.path.exists(downloaded_fname):
            with open(downloaded_fname, "w") as f:
                json.dump([fname], f)
        else:
            with open(downloaded_fname, "r") as f:
                downloaded = list(json.load(f))
            downloaded.append(fname)
            with open(downloaded_fname, "w") as f:
                json.dump(downloaded, f)

    print(
        f"Successfully downloaded {mid}_{lang} (i.e. {read_video_dates(data_dir)[mid]}) at {datetime.datetime.now()}.")


def download_single_meeting(
    m3u8_links, mid, data_dir, target_lang="all", mthread=10, merge=True, log_progress=True
):
    """
    Download a single meeting (with all languages) for downloading demos.
    @param m3u8_links: The m3u8 links of a meeting (with all languages).
    @param mid: The meeting id.
    @param data_dir: The data directory to store and extract data/metadata.
    @param target_lang: The target language for downloading, default is all.
    @param mthread: The number of thread used for downloading, default is 10.
    @param merge: Whether the downloaded .ts files will be merged into a full video, default is True.
    @param log_progress: Whether the download progress will be logged, default is True.
    """
    assert target_lang in ["can", "man", "eng", "all"]

    for lang, link in m3u8_links.items():
        if lang != target_lang and target_lang != "all":
            continue
        download_from_playlist_m3u8(
            link=link,
            mid=mid,
            data_dir=data_dir,
            lang=lang,
            mthread=mthread,
            merge=merge,
            log_progress=log_progress,
        )


def download_meetings(data_dir, session="all", mthread=16, merge=True, target_lang="all", proglog=None):
    """
    Download meetings from the pre-fetched and preprocessed playlist.m3u8 link metadata.
    @param data_dir: The data directory to store and extract data/metadata.
    @param session: The target session for downloading, e.g. "1617", "1718". By default is all.
    @param mthread: The number of thread used for downloading, default is 10.
    @param merge: Whether the downloaded .ts files will be merged into a full video, default is True.
    """
    all_sessions = ["1617", "1718", "1819", "1920", "2021"]
    session = [session] if not isinstance(
        session, list) and session != "all" else session
    assert session == "all" or set(session).issubset(set(all_sessions))

    downloaded_fname = os.path.join(
        data_dir, "metadata", "global", "downloaded.json") if not proglog else proglog
    if os.path.exists(downloaded_fname):
        with open(downloaded_fname, "r") as f:
            downloaded = json.load(f)
    else:
        downloaded = []

    m3u8_links = read_playlists(data_dir)
    for session_id, mids in m3u8_links.items():
        if session != "all" and session_id not in session:
            continue
        for mid, langs in mids.items():
            for lang, link in langs.items():
                fname = "_".join([mid, lang]) + ".mp4"
                if fname in downloaded or target_lang != "all" and lang != target_lang:
                    continue
                download_from_playlist_m3u8(
                    link=link,
                    mid=mid,
                    data_dir=data_dir,
                    lang=lang,
                    mthread=mthread,
                    merge=merge,
                    log_progress=True,
                    proglog=proglog
                )


def main():
    # session_choices = ["1617", "1718", "1819", "1920", "2021", "all"]
    session_choices = ["1213", "1314", "1415", "1516", "all"]

    parser = argparse.ArgumentParser(
        description='Download HKLEGCO videos.')
    parser.add_argument('--session', type=str, choices=session_choices, default="all",
                        help='Target session (e.g. "1617", "1718", "1819", "1920", "2021") to download, default is all')
    parser.add_argument('--proglog', type=Path, default=os.path.join(f"{DATA_DIR}", "metadata", "global", "downloaded.json"),
                        help='Path to the json file for storing the progress.')
    args = parser.parse_args()

    while True:
        try:
            download_meetings(data_dir=DATA_DIR, session=args.session,
                              target_lang="can", mthread=MTHREAD, proglog=args.proglog)
        except:
            print("Connection timeout, will retry in 20s...")
            time.sleep(20)
        else:
            break


if __name__ == "__main__":
    main()

# sample_mid = "M16100003"
# sample_m3u8_link = "https://5b4c10ababf6d.streamlock.net//VODonSAN/_definst_/s02/2016/10/mp4:M16100003_VS15.mp4/playlist.m3u8"
# download_from_playlist_m3u8(sample_m3u8_link, sample_mid, DATA_DIR, lang="can", mthread=10, log_progress=False)

# m3u8_links = read_playlists(DATA_DIR)
# download_single_meeting(
#     m3u8_links["1617"]["M17020002"], "M17020002", DATA_DIR, target_lang="man", mthread=10, log_progress=False
# )

# downloaded_fname = os.path.join(DATA_DIR, "metadata", "global", "downloaded.json")
# with open(downloaded_fname, "w") as f:
#     json.dump(["M16100003_can.mp4", "M17020002_can.mp4", "M17020002_eng.mp4", "M17020002_man.mp4"], f)
