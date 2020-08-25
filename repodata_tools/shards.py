import os
import glob
import hashlib
import subprocess
import copy
import hmac
import base64

import rapidjson as json
import joblib
import tenacity
import requests

from .utils import chunk_iterable, compute_md5


def get_old_shard_path(subdir, pkg, n_dirs=12):
    chars = [c for c in pkg if c.isalnum()]
    while len(chars) < n_dirs:
        chars.append("z")

    pth_parts = (
        ["shards", subdir]
        + [chars[i] for i in range(n_dirs)]
        + [pkg + ".json"]
    )

    return os.path.join(*pth_parts)


def get_shard_path(subdir, pkg, n_dirs=3):
    hex = hashlib.sha1(pkg.encode("utf-8")).hexdigest()[0:n_dirs]

    pth_parts = (
        ["shards", subdir]
        + [hex[i] for i in range(n_dirs)]
        + [pkg + ".json"]
    )

    return os.path.join(*pth_parts)


def glob_shards(shards_repo, subdir, n_dirs=3):
    dirs = ["*"] * n_dirs
    tot_pth = (
        [shards_repo, "shards", subdir]
        + dirs
        + ["*.json"]
    )
    return glob.glob(os.path.join(*tot_pth))


def _read_shard_chunk(shard_pths):
    shards = []
    for shard_pth in shard_pths:
        with open(shard_pth, "r") as fp:
            shards.append(json.load(fp))
    return shards


def read_subdir_shards(shards_repo, subdir, all_shards):
    shard_paths = glob_shards(shards_repo, subdir)
    tot = len(shard_paths)
    print("found %d repodata shards for subdir %s" % (tot, subdir), flush=True)

    n_jobs = 8
    with joblib.Parallel(n_jobs=n_jobs, backend="threading", verbose=0) as p:
        shards_lists = p(
            joblib.delayed(_read_shard_chunk)(s)
            for s in chunk_iterable(shard_paths, tot // n_jobs)
        )

    assert sum(len(s) for s in shards_lists) == len(shard_paths)

    for shards in shards_lists:
        for shard in shards:
            subdir_pkg = os.path.join(shard["subdir"], shard["package"])
            all_shards[subdir_pkg] = shard


@tenacity.retry(
    wait=tenacity.wait_random_exponential(multiplier=1, max=60),
    stop=tenacity.stop_after_attempt(10),
    reraise=True,
)
def make_repodata_shard(subdir, pkg, label, feedstock, url, tmpdir, md5_checksum=None):
    os.makedirs(f"{tmpdir}/noarch", exist_ok=True)
    os.makedirs(f"{tmpdir}/{subdir}", exist_ok=True)
    subprocess.run(
        f"curl -L {url} > {tmpdir}/{subdir}/{pkg}",
        shell=True,
        check=True,
    )

    if md5_checksum is not None:
        local_md5 = compute_md5(f"{tmpdir}/{subdir}/{pkg}")
        if not hmac.compare_digest(local_md5, md5_checksum):
            raise RuntimeError("md5 chechsum is incorrect! exiting!")

    subprocess.run(
        f"conda index --no-progress {tmpdir}",
        shell=True,
        check=True,
    )

    with open(f"{tmpdir}/channeldata.json", "r") as fp:
        cd = json.load(fp)

    with open(f"{tmpdir}/{subdir}/repodata.json", "r") as fp:
        rd = json.load(fp)

    shard = {}
    shard["labels"] = [label]
    shard["repodata_version"] = rd["repodata_version"]
    shard["repodata"] = copy.deepcopy(rd["packages"][pkg])
    shard["subdir"] = subdir
    shard["package"] = pkg
    shard["url"] = url
    shard["feedstock"] = feedstock

    # we are hacking at this
    shard["channeldata_version"] = cd["channeldata_version"]
    shard["channeldata"] = copy.deepcopy(
        cd["packages"][rd["packages"][pkg]["name"]]
    )

    return shard


@tenacity.retry(
    wait=tenacity.wait_random_exponential(multiplier=1, max=60),
    stop=tenacity.stop_after_attempt(10),
    reraise=True,
)
def shard_exists(shard_pth):
    r = requests.get(
        "https://api.github.com/repos/regro/"
        "repodata-shards/contents/%s" % shard_pth,
        headers={"Authorization": "token %s" % os.environ["GITHUB_TOKEN"]},
    )
    if r.status_code == 200:
        return True
    elif r.status_code == 404:
        return False
    else:
        r.raise_for_status()


@tenacity.retry(
    wait=tenacity.wait_random_exponential(multiplier=1, max=60),
    stop=tenacity.stop_after_attempt(10),
    reraise=True,
)
def push_shard(shard, shard_pth, subdir, pkg):
    if not shard_exists(shard_pth):
        edata = base64.standard_b64encode(
            json.dumps(shard, sort_keys=True, indent=2).encode("utf-8")
        ).decode("ascii")

        data = {
            "message": (
                "[ci skip] [skip ci] [cf admin skip] ***NO_CI*** added "
                "%s/%s" % (subdir, pkg)
            ),
            "content": edata,
            "branch": "master",
        }

        r = requests.put(
            "https://api.github.com/repos/regro/"
            "repodata-shards/contents/%s" % shard_pth,
            headers={"Authorization": "token %s" % os.environ["GITHUB_TOKEN"]},
            json=data
        )

        if r.status_code != 201:
            r.raise_for_status()