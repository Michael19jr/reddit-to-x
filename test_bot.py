"""Offline test harness: fakes Reddit so the logic can be exercised end to end."""

import importlib
import json
import os
import subprocess
import sys
import time

FIXTURE = [
    # id, title, score, age_hours, extra flags
    ("aaa111", "Giannis drops 64 in a losing effort, most ever in a loss", 48000, 3, {}),
    ("bbb222", "Below threshold post that should be ignored", 120, 2, {}),
    ("ccc333", "Stickied game thread that should be skipped", 9000, 5, {"stickied": True}),
    ("ddd444", "NSFW post that should be skipped", 7000, 4, {"over_18": True}),
    ("eee555", "Old post past the age cutoff", 30000, 200, {}),
    ("fff666", "A very long title that runs on and on and keeps going well past "
               "any reasonable limit so we can confirm the truncation logic trims "
               "it down to fit inside the two hundred and eighty character budget "
               "while still leaving room for the permalink at the end of the tweet "
               "without ever spilling over the cap", 5000, 6, {}),
    ("ggg777", "Fourth qualifying post to test the per-run cap", 4000, 7, {}),
    ("hhh888", "Fifth qualifying post", 3500, 8, {}),
    ("iii999", "Sixth qualifying post", 3000, 9, {}),
]


def fake_posts():
    now = time.time()
    out = []
    for pid, title, score, age_h, extra in FIXTURE:
        post = {
            "id": pid,
            "title": title,
            "score": score,
            "created_utc": now - age_h * 3600,
            "stickied": False,
            "over_18": False,
            "spoiler": False,
            "link_flair_text": None,
        }
        post.update(extra)
        out.append(post)
    return out


def load_bot():
    for mod in list(sys.modules):
        if mod == "bot":
            del sys.modules[mod]
    bot = importlib.import_module("bot")
    bot.reddit_token = lambda: "fake-token"
    bot.fetch_candidates = lambda token: fake_posts()
    return bot


def setenv(**kwargs):
    base = {
        "SUBREDDIT": "nba",
        "MIN_SCORE": "1000",
        "LINK_MODE": "inline",
        "MAX_POSTS_PER_RUN": "3",
        "MAX_AGE_HOURS": "48",
        "DRY_RUN": "true",
        "STATE_FILE": "test_state.json",
        "SEED_ON_FIRST_RUN": "false",
    }
    base.update({k: str(v) for k, v in kwargs.items()})
    for k in list(os.environ):
        if k in ("SUBREDDIT", "MIN_SCORE", "LINK_MODE", "MAX_POSTS_PER_RUN",
                 "MAX_AGE_HOURS", "DRY_RUN", "STATE_FILE", "SEED_ON_FIRST_RUN",
                 "POSTER", "TWEET_PREFIX", "TWEET_SUFFIX", "FLAIR_ALLOWLIST"):
            del os.environ[k]
    os.environ.update(base)


def reset_state():
    if os.path.exists("test_state.json"):
        os.remove("test_state.json")


def read_state():
    with open("test_state.json") as fh:
        return json.load(fh)


failures = []


def check(label, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {label}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(label)


print("=" * 70)
print("TEST 1: filtering + per-run cap + truncation")
print("=" * 70)
reset_state()
setenv()
bot = load_bot()
bot.main()
state = read_state()
ids = set(state["posted"])
check("below-threshold post excluded", "bbb222" not in ids)
check("stickied post excluded", "ccc333" not in ids)
check("nsfw post excluded", "ddd444" not in ids)
check("stale post excluded", "eee555" not in ids)
check("top post included", "aaa111" in ids)
check("cap of 3 respected", len(ids) == 3, f"got {len(ids)}")

print()
print("=" * 70)
print("TEST 2: dedupe — rerun should post the leftovers, not repeats")
print("=" * 70)
first_batch = set(read_state()["posted"])
bot = load_bot()
bot.main()
state = read_state()
new = set(state["posted"]) - first_batch
check("no reposts of the first batch", first_batch <= set(state["posted"]))
check("leftovers picked up on rerun", len(new) == 2, f"got {len(new)}")
check("run 2 brings tracked total to 5", len(state["posted"]) == 5, f"got {len(state['posted'])}")

print()
print("=" * 70)
print("TEST 3: rerun with nothing new posts nothing")
print("=" * 70)
before = len(read_state()["posted"])
bot = load_bot()
bot.main()
check("no new posts on third run", len(read_state()["posted"]) == before)

print()
print("=" * 70)
print("TEST 4: tweet length under 280 in every link mode")
print("=" * 70)
for mode in ("inline", "none", "reply"):
    reset_state()
    setenv(LINK_MODE=mode, TWEET_PREFIX="[r/nba]")
    bot = load_bot()
    longest = max(fake_posts(), key=lambda p: len(p["title"]))
    text, reply = bot.compose(longest)
    weighted = len(text)
    if mode == "inline":
        # X counts a URL as 23 chars; our permalink is shorter than that,
        # so measure the worst case.
        weighted = len(text) - len("https://redd.it/" + longest["id"]) + bot.URL_WEIGHT
    check(f"{mode}: weighted length {weighted} <= 280", weighted <= 280, f"got {weighted}")
    check(f"{mode}: link present as expected",
          ("redd.it" in text) == (mode == "inline")
          or (mode == "reply" and reply and "redd.it" in reply))

print()
print("=" * 70)
print("TEST 5: seeding on first run tweets nothing")
print("=" * 70)
reset_state()
setenv(SEED_ON_FIRST_RUN="true")
bot = load_bot()
bot.main()
state = read_state()
check("seeded flag set", state["seeded"] is True)
check("all 5 qualifying posts recorded as seeded", len(state["posted"]) == 5, f"got {len(state['posted'])}")
check("all marked seeded, none tweeted",
      all(v.get("seeded") for v in state["posted"].values()))

print()
print("=" * 70)
print("TEST 6: flair allowlist")
print("=" * 70)
reset_state()
setenv(FLAIR_ALLOWLIST="Highlight")
bot = load_bot()
bot.main()
check("nothing matches a flair no post has", len(read_state()["posted"]) == 0)

print()
print("=" * 70)
print("TEST 7: workflow YAML is valid")
print("=" * 70)
try:
    import yaml
    with open(".github/workflows/reddit-to-x.yml") as fh:
        wf = yaml.safe_load(fh)
    check("YAML parses", True)
    check("has schedule trigger", "schedule" in wf[True] if True in wf else "schedule" in wf.get("on", {}))
    check("job defined", "run" in wf["jobs"])
except ImportError:
    print("  [SKIP] pyyaml not installed")

print()
print("=" * 70)
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("ALL TESTS PASSED")
