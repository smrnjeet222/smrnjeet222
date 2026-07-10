"""
GitHub profile stats SVG updater.

Env:
  ACCESS_TOKEN  — GitHub PAT (required)
  USER_NAME     — GitHub login (default: smrnjeet222)
  BIRTHDAY      — YYYY-MM-DD (default: 1999-11-03)
"""
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
    data,
    cache_comment,
    addition_total=0,
    deletion_total=0,
    my_commits=0,
    cursor=None,
):
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
            return 0
        ref = repo.get("defaultBranchRef")
        if ref is not None:
            return loc_counter_one_repo(
                owner,
                repo_name,
                data,
                cache_comment,
                ref["target"]["history"],
                addition_total,
                deletion_total,
                my_commits,
            )
        return 0
    force_close_file(data, cache_comment)
    if request.status_code == 403:
        raise Exception("Too many requests — hit GitHub anti-abuse / rate limit")
    raise Exception(
        "recursive_loc() failed with",
        request.status_code,
        (request.text or "")[:500],
        QUERY_COUNT,
    )


def loc_counter_one_repo(
    owner, repo_name, data, cache_comment, history, addition_total, deletion_total, my_commits
):
    for node in history["edges"]:
        if node["node"]["author"]["user"] == OWNER_ID:
            my_commits += 1
            addition_total += node["node"]["additions"]
            deletion_total += node["node"]["deletions"]

    if history["edges"] == [] or not history["pageInfo"]["hasNextPage"]:
        return addition_total, deletion_total, my_commits
    return recursive_loc(
        owner,
        repo_name,
        data,
        cache_comment,
        addition_total,
        deletion_total,
        my_commits,
        history["pageInfo"]["endCursor"],
    )


def loc_query(owner_affiliation, comment_size=0, force_cache=False, cursor=None, edges=None):
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
    # GraphQL can return null nodes (deleted / inaccessible repos)
    edges = edges + [e for e in page["edges"] if e and e.get("node")]
    if page["pageInfo"]["hasNextPage"]:
        return loc_query(
            owner_affiliation,
            comment_size,
            force_cache,
            page["pageInfo"]["endCursor"],
            edges,
        )
    return cache_builder(edges, comment_size, force_cache)


def cache_path():
    return CACHE_DIR / (hashlib.sha256(USER_NAME.encode("utf-8")).hexdigest() + ".txt")


def cache_builder(edges, comment_size, force_cache, loc_add=0, loc_del=0):
    cached = True
    filename = cache_path()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    try:
        with open(filename, "r") as f:
            data = f.readlines()
    except FileNotFoundError:
        data = []
        if comment_size > 0:
            for _ in range(comment_size):
                data.append("This line is a comment block. Write whatever you want here.\n")
        with open(filename, "w") as f:
            f.writelines(data)

    if len(data) - comment_size != len(edges) or force_cache:
        cached = False
        flush_cache(edges, filename, comment_size)
        with open(filename, "r") as f:
            data = f.readlines()

    cache_comment = data[:comment_size]
    data = data[comment_size:]
    total_repos = len(edges)
    for index in range(total_repos):
        name = edges[index]["node"]["nameWithOwner"]
        repo_hash, commit_count, *_rest = data[index].split()
        if repo_hash == hashlib.sha256(name.encode("utf-8")).hexdigest():
            try:
                total = edges[index]["node"]["defaultBranchRef"]["target"]["history"]["totalCount"]
                if int(commit_count) != total:
                    print(f"  LOC [{index + 1}/{total_repos}] {name} ({total} commits)...", flush=True)
                    owner, repo_name = name.split("/")
                    loc = recursive_loc(owner, repo_name, data, cache_comment)
                    data[index] = (
                        f"{repo_hash} {total} {loc[2]} {loc[0]} {loc[1]}\n"
                    )
            except TypeError:
                data[index] = repo_hash + " 0 0 0 0\n"
        with open(filename, "w") as f:
            f.writelines(cache_comment)
            f.writelines(data)
    for line in data:
        loc = line.split()
        loc_add += int(loc[3])
        loc_del += int(loc[4])
    return [loc_add, loc_del, loc_add - loc_del, cached]


def flush_cache(edges, filename, comment_size):
    with open(filename, "r") as f:
        data = f.readlines()[:comment_size] if comment_size > 0 else []
    with open(filename, "w") as f:
        f.writelines(data)
        for edge in edges:
            node = edge.get("node") if edge else None
            if not node:
                continue
            f.write(
                hashlib.sha256(node["nameWithOwner"].encode("utf-8")).hexdigest()
                + " 0 0 0 0\n"
            )


def force_close_file(data, cache_comment):
    filename = cache_path()
    with open(filename, "w") as f:
        f.writelines(cache_comment)
        f.writelines(data)
    print("Partial cache saved to", filename)


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


def commit_counter(comment_size):
    total_commits = 0
    with open(cache_path(), "r") as f:
        data = f.readlines()[comment_size:]
    for line in data:
        total_commits += int(line.split()[2])
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


if __name__ == "__main__":
    print("Calculation times:")
    user_data, user_time = perf_counter(user_getter, USER_NAME)
    OWNER_ID, acc_date = user_data
    formatter("account data", user_time)

    birthday = parse_birthday()
    age_data, age_time = perf_counter(daily_readme, birthday)
    formatter("age calculation", age_time)

    comment_size = 0
    total_loc, loc_time = perf_counter(
        loc_query, ["OWNER", "COLLABORATOR", "ORGANIZATION_MEMBER"], comment_size
    )
    formatter("LOC (cached)", loc_time) if total_loc[-1] else formatter("LOC (no cache)", loc_time)

    commit_data, commit_time = perf_counter(commit_counter, comment_size)
    star_data, star_time = perf_counter(graph_repos_stars, "stars", ["OWNER"])
    repo_data, repo_time = perf_counter(graph_repos_stars, "repos", ["OWNER"])
    contrib_data, contrib_time = perf_counter(
        graph_repos_stars, "repos", ["OWNER", "COLLABORATOR", "ORGANIZATION_MEMBER"]
    )
    follower_data, follower_time = perf_counter(follower_getter, USER_NAME)

    for index in range(len(total_loc) - 1):
        total_loc[index] = "{:,}".format(total_loc[index])

    for svg_name in ("dark_mode.svg", "light_mode.svg"):
        svg_overwrite(
            str(ROOT / svg_name),
            age_data,
            commit_data,
            star_data,
            repo_data,
            contrib_data,
            follower_data,
            total_loc[:-1],
        )

    total = user_time + age_time + loc_time + commit_time + star_time + repo_time + contrib_time
    print("Total function time:", "%.4f" % total, "s")
    print("Total GitHub GraphQL API calls:", sum(QUERY_COUNT.values()))
    for funct_name, count in QUERY_COUNT.items():
        print("{:<28}".format(" " + funct_name + ":"), "{:>6}".format(count))
