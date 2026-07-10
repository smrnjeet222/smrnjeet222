"""
GitHub profile stats SVG updater.

Env:
  ACCESS_TOKEN  — GitHub PAT (required)
  USER_NAME     — GitHub login (default: smrnjeet222)
  BIRTHDAY      — YYYY-MM-DD (default: 1999-11-03)

CLI:
  python today.py           # merge: update visible repos, preserve rest (Actions-safe)
  python today.py --seed    # local: re-walk every repo the token can see; still never
                            # deletes cache rows for repos the token cannot see (orgs)
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import os
import time
from pathlib import Path

import requests
from dateutil import relativedelta
from lxml import etree

ROOT = Path(__file__).resolve().parent
CACHE_DIR = ROOT / "cache"

HEADERS = {"authorization": "token " + os.environ["ACCESS_TOKEN"]}
USER_NAME = os.environ.get("USER_NAME", "smrnjeet222")
DEFAULT_BIRTHDAY = "1999-11-03"
QUERY_COUNT = {
    "user_getter": 0,
    "follower_getter": 0,
    "graph_repos_stars": 0,
    "recursive_loc": 0,
    "graph_commits": 0,
    "loc_query": 0,
}


def daily_readme(birthday):
    diff = relativedelta.relativedelta(datetime.datetime.today(), birthday)
    return "{} {}, {} {}, {} {}{}".format(
        diff.years,
        "year" + format_plural(diff.years),
        diff.months,
        "month" + format_plural(diff.months),
        diff.days,
        "day" + format_plural(diff.days),
        " 🎂" if (diff.months == 0 and diff.days == 0) else "",
    )


def format_plural(unit):
    return "s" if unit != 1 else ""


# Transient GitHub / edge failures worth retrying
_RETRY_STATUS = {429, 500, 502, 503, 504}
_MAX_RETRIES = 6
_BASE_DELAY_S = 2.0


def _retry_delay(attempt: int, response=None) -> float:
    """Exponential backoff; honor Retry-After on 429 when present."""
    delay = _BASE_DELAY_S * (2 ** (attempt - 1))
    if response is not None and response.status_code == 429:
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                delay = max(delay, float(retry_after))
            except ValueError:
                pass
    # cap so a single call can't sleep forever
    return min(delay, 60.0)


def post_graphql(func_name, query, variables):
    """
    POST to GitHub GraphQL with retries on transient errors.
    Returns the final Response (may be non-200 after retries exhausted).
    """
    last_error = None
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            response = requests.post(
                "https://api.github.com/graphql",
                json={"query": query, "variables": variables},
                headers=HEADERS,
                timeout=60,
            )
        except requests.RequestException as exc:
            last_error = exc
            if attempt == _MAX_RETRIES:
                raise Exception(
                    func_name,
                    " network failure after",
                    _MAX_RETRIES,
                    "retries:",
                    exc,
                    QUERY_COUNT,
                ) from exc
            delay = _retry_delay(attempt)
            print(
                f"  {func_name}: network error ({exc}); "
                f"retry {attempt}/{_MAX_RETRIES} in {delay:.0f}s",
                flush=True,
            )
            time.sleep(delay)
            continue

        if response.status_code == 200:
            return response

        retryable = response.status_code in _RETRY_STATUS
        if not retryable or attempt == _MAX_RETRIES:
            return response

        delay = _retry_delay(attempt, response)
        body_preview = (response.text or "")[:120].replace("\n", " ")
        print(
            f"  {func_name}: HTTP {response.status_code} ({body_preview}); "
            f"retry {attempt}/{_MAX_RETRIES} in {delay:.0f}s",
            flush=True,
        )
        time.sleep(delay)

    # unreachable, but keeps type-checkers happy
    raise Exception(func_name, " exhausted retries", last_error, QUERY_COUNT)


def simple_request(func_name, query, variables):
    request = post_graphql(func_name, query, variables)
    if request.status_code != 200:
        raise Exception(
            func_name,
            " has failed with a",
            request.status_code,
            (request.text or "")[:500],
            QUERY_COUNT,
        )
    try:
        payload = request.json()
    except ValueError as exc:
        raise Exception(func_name, " returned non-JSON body", QUERY_COUNT) from exc
    # HTTP 200 can still be a GraphQL failure (data: null + errors)
    if payload.get("data") is None and payload.get("errors"):
        raise Exception(
            func_name,
            " GraphQL error:",
            payload["errors"],
            QUERY_COUNT,
        )
    return request


def graph_repos_stars(count_type, owner_affiliation, cursor=None):
    query_count("graph_repos_stars")
    query = """
    query ($owner_affiliation: [RepositoryAffiliation], $login: String!, $cursor: String) {
        user(login: $login) {
            repositories(first: 100, after: $cursor, ownerAffiliations: $owner_affiliation) {
                totalCount
                edges {
                    node {
                        ... on Repository {
                            nameWithOwner
                            stargazers { totalCount }
                        }
                    }
                }
                pageInfo { endCursor hasNextPage }
            }
        }
    }"""
    variables = {"owner_affiliation": owner_affiliation, "login": USER_NAME, "cursor": cursor}
    request = simple_request(graph_repos_stars.__name__, query, variables)
    if count_type == "repos":
        return request.json()["data"]["user"]["repositories"]["totalCount"]
    if count_type == "stars":
        return stars_counter(request.json()["data"]["user"]["repositories"]["edges"])


def recursive_loc(
    owner,
    repo_name,
    addition_total=0,
    deletion_total=0,
    my_commits=0,
    cursor=None,
):
    """Walk commit history for one repo. Raises LocFetchError on hard API failure."""
    query_count("recursive_loc")
    query = """
    query ($repo_name: String!, $owner: String!, $cursor: String) {
        repository(name: $repo_name, owner: $owner) {
            defaultBranchRef {
                target {
                    ... on Commit {
                        history(first: 100, after: $cursor) {
                            totalCount
                            edges {
                                node {
                                    ... on Commit {
                                        committedDate
                                        author { user { id } }
                                        deletions
                                        additions
                                    }
                                }
                            }
                            pageInfo { endCursor hasNextPage }
                        }
                    }
                }
            }
        }
    }"""
    variables = {"repo_name": repo_name, "owner": owner, "cursor": cursor}
    request = post_graphql(recursive_loc.__name__, query, variables)
    if request.status_code == 200:
        payload = request.json()
        repo = (payload.get("data") or {}).get("repository")
        if repo is None:
            raise LocFetchError(f"{owner}/{repo_name}: repository null in GraphQL", fatal=False)
        ref = repo.get("defaultBranchRef")
        if ref is None:
            return 0, 0, 0
        return loc_counter_one_repo(
            owner,
            repo_name,
            ref["target"]["history"],
            addition_total,
            deletion_total,
            my_commits,
        )
    fatal = request.status_code == 403
    raise LocFetchError(
        f"{owner}/{repo_name}: HTTP {request.status_code}",
        fatal=fatal,
    )


def loc_counter_one_repo(
    owner, repo_name, history, addition_total, deletion_total, my_commits
):
    for node in history["edges"]:
        author = (node.get("node") or {}).get("author") or {}
        user = author.get("user")
        if user == OWNER_ID:
            my_commits += 1
            addition_total += node["node"]["additions"]
            deletion_total += node["node"]["deletions"]

    if history["edges"] == [] or not history["pageInfo"]["hasNextPage"]:
        return addition_total, deletion_total, my_commits
    return recursive_loc(
        owner,
        repo_name,
        addition_total,
        deletion_total,
        my_commits,
        history["pageInfo"]["endCursor"],
    )


def loc_query(owner_affiliation, comment_size=0, force_refresh=False, cursor=None, edges=None):
    if edges is None:
        edges = []
    query_count("loc_query")
    query = """
    query ($owner_affiliation: [RepositoryAffiliation], $login: String!, $cursor: String) {
        user(login: $login) {
            repositories(first: 60, after: $cursor, ownerAffiliations: $owner_affiliation) {
                edges {
                    node {
                        ... on Repository {
                            nameWithOwner
                            defaultBranchRef {
                                target {
                                    ... on Commit {
                                        history { totalCount }
                                    }
                                }
                            }
                        }
                    }
                }
                pageInfo { endCursor hasNextPage }
            }
        }
    }"""
    variables = {"owner_affiliation": owner_affiliation, "login": USER_NAME, "cursor": cursor}
    request = simple_request(loc_query.__name__, query, variables)
    page = request.json()["data"]["user"]["repositories"]
    edges = edges + [e for e in page["edges"] if e and e.get("node")]
    if page["pageInfo"]["hasNextPage"]:
        return loc_query(
            owner_affiliation,
            comment_size,
            force_refresh,
            page["pageInfo"]["endCursor"],
            edges,
        )
    return cache_builder(edges, force_refresh)


class LocFetchError(Exception):
    def __init__(self, message: str, fatal: bool = False):
        super().__init__(message)
        self.fatal = fatal


def legacy_cache_path() -> Path:
    return CACHE_DIR / (hashlib.sha256(USER_NAME.encode("utf-8")).hexdigest() + ".txt")


def repos_cache_dir() -> Path:
    return CACHE_DIR / "repos"


def repo_id_hash(name_with_owner: str) -> str:
    return hashlib.sha256(name_with_owner.encode("utf-8")).hexdigest()


def repo_cache_file(repo_hash: str) -> Path:
    return repos_cache_dir() / f"{repo_hash}.txt"


def parse_repo_cache_text(text: str, fallback_hash: str = "") -> dict | None:
    """
    Per-repo file format:
      line1: owner/name
      line2: branch_commits my_commits additions deletions
    """
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return None
    if len(lines) == 1:
        parts = lines[0].split()
        if len(parts) >= 5:
            return {
                "hash": parts[0],
                "name": "",
                "commits": int(parts[1]),
                "my_commits": int(parts[2]),
                "additions": int(parts[3]),
                "deletions": int(parts[4]),
            }
        return None
    name = lines[0]
    parts = lines[1].split()
    if len(parts) < 4:
        return None
    return {
        "hash": fallback_hash or repo_id_hash(name),
        "name": name,
        "commits": int(parts[0]),
        "my_commits": int(parts[1]),
        "additions": int(parts[2]),
        "deletions": int(parts[3]),
    }


def read_repo_cache(repo_hash: str) -> dict | None:
    path = repo_cache_file(repo_hash)
    if not path.is_file():
        return None
    try:
        return parse_repo_cache_text(path.read_text(encoding="utf-8"), repo_hash)
    except (OSError, ValueError):
        return None


def write_repo_cache(
    repo_hash: str,
    name: str,
    commits: int,
    my_commits: int,
    additions: int,
    deletions: int,
) -> None:
    repos_cache_dir().mkdir(parents=True, exist_ok=True)
    path = repo_cache_file(repo_hash)
    path.write_text(
        f"{name}\n{commits} {my_commits} {additions} {deletions}\n",
        encoding="utf-8",
    )


def load_all_repo_caches() -> dict[str, dict]:
    """Load every per-repo cache file. Never deletes files."""
    migrate_legacy_monolithic_cache()
    out: dict[str, dict] = {}
    d = repos_cache_dir()
    if not d.is_dir():
        return out
    for path in sorted(d.glob("*.txt")):
        h = path.stem
        entry = read_repo_cache(h)
        if entry:
            out[h] = entry
    return out


def migrate_legacy_monolithic_cache() -> None:
    """One-shot: split old cache/<userhash>.txt into cache/repos/<hash>.txt."""
    legacy = legacy_cache_path()
    if not legacy.is_file():
        return
    repos_cache_dir().mkdir(parents=True, exist_ok=True)
    migrated = 0
    for line in legacy.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) < 5:
            continue
        h, commits, my_c, add, delete = parts[0], parts[1], parts[2], parts[3], parts[4]
        dest = repo_cache_file(h)
        if dest.is_file():
            continue
        dest.write_text(
            f"unknown/{h[:8]}\n{commits} {my_c} {add} {delete}\n",
            encoding="utf-8",
        )
        migrated += 1
    if migrated:
        print(
            f"  cache: migrated {migrated} repos from legacy {legacy.name} → cache/repos/",
            flush=True,
        )


def cache_builder(edges, force_refresh=False, loc_add=0, loc_del=0):
    """
    Per-repo merge cache under cache/repos/<hash>.txt:
    - Update / create files for repos visible to the token
    - On API failure: keep existing file unchanged
    - Never delete a per-repo cache file
    """
    by_hash = load_all_repo_caches()
    preserved_before = len(by_hash)
    cached = preserved_before > 0 and not force_refresh

    visible = []
    for edge in edges:
        node = edge.get("node") if edge else None
        if not node:
            continue
        visible.append(node)

    total_repos = len(visible)
    print(
        f"  cache: {preserved_before} repo files, {total_repos} visible to token"
        f"{' [seed refresh]' if force_refresh else ''}",
        flush=True,
    )

    for index, node in enumerate(visible):
        name = node["nameWithOwner"]
        h = repo_id_hash(name)
        prev = by_hash.get(h) or read_repo_cache(h)

        try:
            total = node["defaultBranchRef"]["target"]["history"]["totalCount"]
        except (TypeError, KeyError):
            if prev is None:
                write_repo_cache(h, name, 0, 0, 0, 0)
                by_hash[h] = read_repo_cache(h)
            else:
                print(f"  keep {name}: no defaultBranchRef; old cache intact", flush=True)
            continue

        old_commits = prev["commits"] if prev else -1
        need_walk = force_refresh or prev is None or old_commits != total
        if not need_walk:
            continue

        cached = False
        print(
            f"  LOC [{index + 1}/{total_repos}] {name} ({total} commits)...",
            flush=True,
        )
        owner, repo_name = name.split("/")
        try:
            loc = recursive_loc(owner, repo_name)
        except LocFetchError as exc:
            if prev is not None:
                print(f"  keep {name}: {exc}; old cache intact", flush=True)
            else:
                print(f"  skip {name}: {exc}; no prior cache", flush=True)
            if exc.fatal:
                print("  stopping further LOC walks (rate limit / fatal)", flush=True)
                break
            continue
        except requests.RequestException as exc:
            if prev is not None:
                print(f"  keep {name}: network {exc}; old cache intact", flush=True)
            else:
                print(f"  skip {name}: network {exc}; no prior cache", flush=True)
            continue

        add_n, del_n, my_n = loc
        if (
            my_n == 0
            and add_n == 0
            and del_n == 0
            and prev is not None
            and prev["my_commits"] > 0
        ):
            print(f"  keep {name}: walk returned empty but cache has data", flush=True)
            continue

        write_repo_cache(h, name, total, my_n, add_n, del_n)
        by_hash[h] = {
            "hash": h,
            "name": name,
            "commits": total,
            "my_commits": my_n,
            "additions": add_n,
            "deletions": del_n,
        }

    by_hash = load_all_repo_caches()
    skipped = len(by_hash) - len({repo_id_hash(n["nameWithOwner"]) for n in visible})
    if skipped > 0:
        print(f"  cache: preserved {skipped} repo files not visible to this token", flush=True)

    for entry in by_hash.values():
        loc_add += entry["additions"]
        loc_del += entry["deletions"]
    return [loc_add, loc_del, loc_add - loc_del, cached, len(by_hash)]


def cache_repo_count() -> int:
    return len(load_all_repo_caches())


def stars_counter(data):
    return sum(
        node["node"]["stargazers"]["totalCount"]
        for node in data
        if node and node.get("node")
    )


def svg_overwrite(filename, age_data, commit_data, star_data, repo_data, contrib_data, follower_data, loc_data):
    tree = etree.parse(filename)
    root = tree.getroot()
    if age_data is not None:
        justify_format(root, "age_data", age_data)
    justify_format(root, "commit_data", commit_data)
    justify_format(root, "star_data", star_data)
    justify_format(root, "repo_data", repo_data)
    justify_format(root, "contrib_data", contrib_data)
    justify_format(root, "follower_data", follower_data)
    justify_format(root, "loc_data", loc_data[2])

    add_s = str(loc_data[0])
    del_s = str(loc_data[1])
    find_and_replace(root, "loc_add", add_s)
    find_and_replace(root, "loc_del", del_s)
    # Re-pad detail line so ++/-- stay right-aligned when digit counts change
    detail_dots = root.find(".//*[@id='loc_detail_dots']")
    if detail_dots is not None and detail_dots.get("data-slot"):
        detail = f"( {add_s}++ / {del_s}-- )"
        budget = int(detail_dots.get("data-slot")) - len(detail)
        detail_dots.text = make_dots(budget)

    tree.write(filename, encoding="utf-8", xml_declaration=True)


def make_dots(budget: int) -> str:
    if budget <= 0:
        return ""
    if budget == 1:
        return " "
    if budget == 2:
        return ". "
    if budget == 3:
        return " . "
    return " " + ("." * (budget - 2)) + " "


def justify_format(root, element_id, new_text, length=0):
    """Right-align value using data-slot on `{id}_dots` (chars for dots+value)."""
    if isinstance(new_text, int):
        new_text = f"{'{:,}'.format(new_text)}"
    new_text = str(new_text)
    find_and_replace(root, element_id, new_text)
    dots_el = root.find(f".//*[@id='{element_id}_dots']")
    if dots_el is None:
        return
    slot = dots_el.get("data-slot")
    if slot is not None:
        budget = int(slot) - len(new_text)
        dots_el.text = make_dots(budget)
        return
    # fallback: legacy length-based pad
    just_len = max(0, length - len(new_text))
    dots_el.text = make_dots(just_len if just_len > 0 else 1)


def find_and_replace(root, element_id, new_text):
    element = root.find(f".//*[@id='{element_id}']")
    if element is not None:
        element.text = new_text


def commit_counter(_comment_size=0):
    total_commits = 0
    for entry in load_all_repo_caches().values():
        total_commits += entry["my_commits"]
    return total_commits


def user_getter(username):
    query_count("user_getter")
    query = """
    query($login: String!){
        user(login: $login) { id createdAt }
    }"""
    request = simple_request(user_getter.__name__, query, {"login": username})
    user = request.json()["data"]["user"]
    return {"id": user["id"]}, user["createdAt"]


def follower_getter(username):
    query_count("follower_getter")
    query = """
    query($login: String!){
        user(login: $login) { followers { totalCount } }
    }"""
    request = simple_request(follower_getter.__name__, query, {"login": username})
    return int(request.json()["data"]["user"]["followers"]["totalCount"])


def query_count(funct_id):
    global QUERY_COUNT
    QUERY_COUNT[funct_id] += 1


def perf_counter(funct, *args):
    start = time.perf_counter()
    funct_return = funct(*args)
    return funct_return, time.perf_counter() - start


def formatter(query_type, difference, funct_return=False, whitespace=0):
    print("{:<23}".format(" " + query_type + ":"), sep="", end="")
    if difference > 1:
        print("{:>12}".format("%.4f" % difference + " s "))
    else:
        print("{:>12}".format("%.4f" % (difference * 1000) + " ms"))
    if whitespace:
        return f"{'{:,}'.format(funct_return): <{whitespace}}"
    return funct_return


def parse_birthday():
    raw = os.environ.get("BIRTHDAY", DEFAULT_BIRTHDAY)
    return datetime.datetime.strptime(raw, "%Y-%m-%d")


def parse_args():
    p = argparse.ArgumentParser(description="Update profile stats SVGs")
    p.add_argument(
        "--seed",
        action="store_true",
        help="Local seed: re-walk all repos visible to this token; never delete hidden org rows",
    )
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    force_refresh = args.seed
    if force_refresh:
        print("Mode: --seed (refresh visible repos; preserve invisible cache rows)")
    else:
        print("Mode: merge (Actions-safe; append/update only)")

    print("Calculation times:")
    user_data, user_time = perf_counter(user_getter, USER_NAME)
    OWNER_ID, acc_date = user_data
    formatter("account data", user_time)

    birthday = parse_birthday()
    age_data, age_time = perf_counter(daily_readme, birthday)
    formatter("age calculation", age_time)

    comment_size = 0
    total_loc, loc_time = perf_counter(
        loc_query,
        ["OWNER", "COLLABORATOR", "ORGANIZATION_MEMBER"],
        comment_size,
        force_refresh,
    )
    # total_loc: [add, del, net, cached_flag, cache_repo_count]
    cache_count = total_loc[4]
    if total_loc[3]:
        formatter("LOC (cached)", loc_time)
    else:
        formatter("LOC (updated)", loc_time)
    print(f"  cache repos stored: {cache_count}")

    commit_data, commit_time = perf_counter(commit_counter, comment_size)
    star_data, star_time = perf_counter(graph_repos_stars, "stars", ["OWNER"])
    repo_data, repo_time = perf_counter(graph_repos_stars, "repos", ["OWNER"])
    contrib_api, contrib_time = perf_counter(
        graph_repos_stars, "repos", ["OWNER", "COLLABORATOR", "ORGANIZATION_MEMBER"]
    )
    # Actions token may under-count orgs; never go below committed cache size
    contrib_data = max(int(contrib_api), int(cache_count))
    if contrib_data != contrib_api:
        print(
            f"  contributed: API={contrib_api} cache={cache_count} → using {contrib_data}"
        )
    follower_data, follower_time = perf_counter(follower_getter, USER_NAME)

    loc_for_svg = ["{:,}".format(n) for n in total_loc[:3]]

    for svg_name in ("dark_mode.svg", "light_mode.svg"):
        svg_overwrite(
            str(ROOT / svg_name),
            age_data,
            commit_data,
            star_data,
            repo_data,
            contrib_data,
            follower_data,
            loc_for_svg,
        )

    total = user_time + age_time + loc_time + commit_time + star_time + repo_time + contrib_time
    print("Total function time:", "%.4f" % total, "s")
    print("Total GitHub GraphQL API calls:", sum(QUERY_COUNT.values()))
    for funct_name, count in QUERY_COUNT.items():
        print("{:<28}".format(" " + funct_name + ":"), "{:>6}".format(count))
    print("Cache dir:", repos_cache_dir())
    if force_refresh:
        print("Next: commit cache/ + SVGs, push to profile repo for Actions merge runs.")
