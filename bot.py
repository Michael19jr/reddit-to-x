#!/usr/bin/env python3
"""
Reddit -> X (Twitter) upvote-threshold bot.
 
Polls a subreddit, and when a post crosses a score threshold, publishes it to X.
 
Two posting backends:
  POSTER=buffer  (default, free)  -> pushes to Buffer, which publishes to X
  POSTER=x_api   (paid)           -> posts directly via the X API v2
 
State (which posts have already been tweeted) lives in posted.json, which the
GitHub Actions workflow commits back to the repo after each run.
"""
 
import base64
import json
import os
import sys
import time
from datetime import datetime, timezone
 
import requests
 
# --------------------------------------------------------------------------
# Config (all via environment variables)
# --------------------------------------------------------------------------
 
def env(name, default=None, required=False):
    val = os.environ.get(name, default)
    if required and not val:
        sys.exit(f"ERROR: missing required environment variable {name}")
    return val
 
 
def env_int(name, default):
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        sys.exit(f"ERROR: {name} must be a whole number, got {raw!r}")
 
 
def env_bool(name, default=False):
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")
 
 
SUBREDDIT = env("SUBREDDIT", required=True)
MIN_SCORE = env_int("MIN_SCORE", 500)
 
# inline = title + link in the tweet
# none   = title only, no link
# reply  = title tweet, link posted as a reply (x_api backend only)
LINK_MODE = env("LINK_MODE", "inline").lower()
 
POSTER = env("POSTER", "buffer").lower()
 
# Which Reddit listings to poll. Comma-separated: new, hot, top, rising.
# "hot,top" catches posts once they gain traction; add "new" to catch
# everything the moment it is submitted.
LISTINGS = [
    l.strip().lower()
    for l in env("LISTINGS", "hot,top").split(",")
    if l.strip()
]
 
MAX_POSTS_PER_RUN = env_int("MAX_POSTS_PER_RUN", 3)
MAX_AGE_HOURS = env_int("MAX_AGE_HOURS", 48)
SKIP_NSFW = env_bool("SKIP_NSFW", True)
SKIP_STICKIED = env_bool("SKIP_STICKIED", True)
SKIP_SPOILER = env_bool("SKIP_SPOILER", True)
 
# Optional comma-separated flair allowlist, e.g. "News,Highlight"
FLAIR_ALLOWLIST = [
    f.strip().lower()
    for f in env("FLAIR_ALLOWLIST", "").split(",")
    if f.strip()
]
 
# Optional text prepended/appended to every tweet
TWEET_PREFIX = env("TWEET_PREFIX", "")
TWEET_SUFFIX = env("TWEET_SUFFIX", "")
 
DRY_RUN = env_bool("DRY_RUN", False)
 
# On the very first run, mark everything currently over the threshold as
# already-seen instead of firing a burst of tweets about old posts.
SEED_ON_FIRST_RUN = env_bool("SEED_ON_FIRST_RUN", True)
 
STATE_FILE = env("STATE_FILE", "posted.json")
STATE_RETENTION_DAYS = env_int("STATE_RETENTION_DAYS", 30)
 
USER_AGENT = env("USER_AGENT", "script:reddit-to-x:v1.0 (by /u/unknown)")
 
# X counts every URL as 23 characters regardless of actual length. Leave a
# couple of characters of slack so we never land exactly on the boundary.
TWEET_LIMIT = 278
URL_WEIGHT = 23
 
 
# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------
 
def load_state():
    if not os.path.exists(STATE_FILE):
        return {"seeded": False, "posted": {}}
    try:
        with open(STATE_FILE) as fh:
            state = json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        print(f"WARN: could not read {STATE_FILE} ({exc}); starting fresh")
        return {"seeded": False, "posted": {}}
    state.setdefault("seeded", False)
    state.setdefault("posted", {})
    return state
 
 
def save_state(state):
    cutoff = time.time() - (STATE_RETENTION_DAYS * 86400)
    state["posted"] = {
        pid: meta
        for pid, meta in state["posted"].items()
        if meta.get("posted_at", 0) >= cutoff
    }
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(state, fh, indent=2, sort_keys=True)
    os.replace(tmp, STATE_FILE)
 
 
# --------------------------------------------------------------------------
# Reddit
# --------------------------------------------------------------------------
 
def reddit_token():
    """App-only OAuth. Needs a 'script' app; no Reddit password required."""
    client_id = env("REDDIT_CLIENT_ID", required=True)
    client_secret = env("REDDIT_CLIENT_SECRET", required=True)
    basic = base64.b64encode(
        f"{client_id}:{client_secret}".encode()
    ).decode()
    resp = requests.post(
        "https://www.reddit.com/api/v1/access_token",
        data={"grant_type": "client_credentials"},
        headers={
            "Authorization": f"Basic {basic}",
            "User-Agent": USER_AGENT,
        },
        timeout=30,
    )
    if resp.status_code != 200:
        sys.exit(
            f"ERROR: Reddit auth failed ({resp.status_code}): {resp.text[:300]}"
        )
    return resp.json()["access_token"]
 
 
def fetch_listing(token, path, params):
    resp = requests.get(
        f"https://oauth.reddit.com{path}",
        params=params,
        headers={
            "Authorization": f"Bearer {token}",
            "User-Agent": USER_AGENT,
        },
        timeout=30,
    )
    if resp.status_code != 200:
        print(f"WARN: {path} returned {resp.status_code}: {resp.text[:200]}")
        return []
    return [c["data"] for c in resp.json().get("data", {}).get("children", [])]
 
 
LISTING_PARAMS = {
    "new": {"limit": 100},
    "hot": {"limit": 100},
    "top": {"limit": 100, "t": "day"},
    "rising": {"limit": 100},
}
 
 
def fetch_candidates(token):
    """Pull from each configured listing and dedupe.
 
    hot + top (the default) catches posts once they gain traction.
    Add 'new' to catch every post the moment it is submitted.
    """
    seen = {}
    for name in LISTINGS:
        params = LISTING_PARAMS.get(name)
        if params is None:
            print(f"WARN: unknown listing {name!r}, skipping")
            continue
        for post in fetch_listing(token, f"/r/{SUBREDDIT}/{name}", params):
            seen[post["id"]] = post
    return list(seen.values())
 
 
def eligible(post, state):
    if post["id"] in state["posted"]:
        return False
    if post.get("score", 0) < MIN_SCORE:
        return False
    if SKIP_STICKIED and post.get("stickied"):
        return False
    if SKIP_NSFW and post.get("over_18"):
        return False
    if SKIP_SPOILER and post.get("spoiler"):
        return False
    age_hours = (time.time() - post.get("created_utc", 0)) / 3600
    if age_hours > MAX_AGE_HOURS:
        return False
    if FLAIR_ALLOWLIST:
        flair = (post.get("link_flair_text") or "").strip().lower()
        if flair not in FLAIR_ALLOWLIST:
            return False
    return True
 
 
# --------------------------------------------------------------------------
# Tweet composition
# --------------------------------------------------------------------------
 
def compose(post):
    """Return (main_tweet_text, reply_text_or_None)."""
    permalink = "https://redd.it/" + post["id"]
    title = post.get("title", "").strip()
 
    prefix = f"{TWEET_PREFIX} " if TWEET_PREFIX else ""
    suffix = f" {TWEET_SUFFIX}" if TWEET_SUFFIX else ""
 
    if LINK_MODE == "inline":
        budget = TWEET_LIMIT - len(prefix) - len(suffix) - URL_WEIGHT - 1
        return f"{prefix}{truncate(title, budget)}{suffix} {permalink}", None
 
    budget = TWEET_LIMIT - len(prefix) - len(suffix)
    text = f"{prefix}{truncate(title, budget)}{suffix}"
 
    if LINK_MODE == "reply":
        return text, permalink
    return text, None
 
 
def truncate(text, budget):
    if budget < 10:
        budget = 10
    if len(text) <= budget:
        return text
    return text[: budget - 1].rstrip() + "…"
 
 
# --------------------------------------------------------------------------
# Posting backends
# --------------------------------------------------------------------------
 
class BufferPoster:
    """Free path: hand the tweet to Buffer, which publishes it to X."""
 
    BASE = "https://api.bufferapp.com/1"
 
    def __init__(self):
        self.token = env("BUFFER_ACCESS_TOKEN", required=True)
        self.profile_id = env("BUFFER_PROFILE_ID", "").strip()
        if not self.profile_id:
            self.profile_id = self._discover_profile()
 
    def _discover_profile(self):
        resp = requests.get(
            f"{self.BASE}/profiles.json",
            params={"access_token": self.token},
            timeout=30,
        )
        if resp.status_code != 200:
            sys.exit(
                f"ERROR: Buffer profiles lookup failed ({resp.status_code}): "
                f"{resp.text[:300]}"
            )
        profiles = resp.json()
        twitter = [p for p in profiles if p.get("service") == "twitter"]
        if not twitter:
            sys.exit(
                "ERROR: no X/Twitter channel connected to this Buffer account. "
                "Connect one at buffer.com, then rerun."
            )
        if len(twitter) > 1:
            names = ", ".join(
                f"{p.get('formatted_username')}={p['id']}" for p in twitter
            )
            sys.exit(
                "ERROR: multiple X channels in Buffer. Set BUFFER_PROFILE_ID "
                f"to the one you want: {names}"
            )
        chosen = twitter[0]
        print(
            f"Using Buffer X channel {chosen.get('formatted_username')} "
            f"({chosen['id']})"
        )
        return chosen["id"]
 
    def post(self, text, reply_text=None):
        if reply_text:
            print(
                "NOTE: LINK_MODE=reply is not supported by the Buffer backend; "
                "sending the link as a separate post instead."
            )
        self._create(text)
        if reply_text:
            self._create(reply_text)
 
    def _create(self, text):
        resp = requests.post(
            f"{self.BASE}/updates/create.json",
            data={
                "access_token": self.token,
                "profile_ids[]": self.profile_id,
                "text": text,
                "now": "true",
            },
            timeout=30,
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"Buffer post failed ({resp.status_code}): {resp.text[:300]}"
            )
        body = resp.json()
        if not body.get("success", True):
            raise RuntimeError(f"Buffer post rejected: {body}")
 
 
class XApiPoster:
    """Paid path: post directly through the X API v2 (OAuth 1.0a user context)."""
 
    URL = "https://api.x.com/2/tweets"
 
    def __init__(self):
        from requests_oauthlib import OAuth1
 
        self.auth = OAuth1(
            env("X_API_KEY", required=True),
            env("X_API_SECRET", required=True),
            env("X_ACCESS_TOKEN", required=True),
            env("X_ACCESS_TOKEN_SECRET", required=True),
        )
 
    def post(self, text, reply_text=None):
        tweet_id = self._create({"text": text})
        if reply_text:
            self._create(
                {"text": reply_text, "reply": {"in_reply_to_tweet_id": tweet_id}}
            )
 
    def _create(self, payload):
        resp = requests.post(
            self.URL, json=payload, auth=self.auth, timeout=30
        )
        if resp.status_code not in (200, 201):
            raise RuntimeError(
                f"X post failed ({resp.status_code}): {resp.text[:300]}"
            )
        return resp.json()["data"]["id"]
 
 
def build_poster():
    if DRY_RUN:
        return None
    if POSTER == "buffer":
        return BufferPoster()
    if POSTER == "x_api":
        return XApiPoster()
    sys.exit(f"ERROR: POSTER must be 'buffer' or 'x_api', got {POSTER!r}")
 
 
# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
 
def main():
    if LINK_MODE not in ("inline", "none", "reply"):
        sys.exit(f"ERROR: LINK_MODE must be inline/none/reply, got {LINK_MODE!r}")
 
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"[{now}] r/{SUBREDDIT} | threshold {MIN_SCORE} | "
          f"listings={'+'.join(LISTINGS)} | "
          f"poster={POSTER} link={LINK_MODE} dry_run={DRY_RUN}")
 
    state = load_state()
    token = reddit_token()
    posts = fetch_candidates(token)
    print(f"Fetched {len(posts)} unique posts")
 
    matches = [p for p in posts if eligible(p, state)]
    matches.sort(key=lambda p: p.get("score", 0), reverse=True)
    print(f"{len(matches)} over threshold and not yet posted")
 
    # First run: record everything without tweeting, so the bot starts clean.
    if not state["seeded"] and SEED_ON_FIRST_RUN:
        for post in matches:
            state["posted"][post["id"]] = {
                "posted_at": time.time(),
                "title": post.get("title", "")[:120],
                "score": post.get("score", 0),
                "seeded": True,
            }
        state["seeded"] = True
        save_state(state)
        print(
            f"First run: seeded {len(matches)} existing posts as already-seen. "
            "Nothing tweeted. Future runs will post new risers only."
        )
        return
 
    state["seeded"] = True
    to_post = matches[:MAX_POSTS_PER_RUN]
    if len(matches) > len(to_post):
        print(
            f"Capping at {MAX_POSTS_PER_RUN} this run; "
            f"{len(matches) - len(to_post)} will be picked up next run"
        )
 
    if not to_post:
        save_state(state)
        print("Nothing to post.")
        return
 
    poster = build_poster()
    posted_count = 0
 
    for post in to_post:
        text, reply = compose(post)
        label = f"[{post.get('score')} pts] {post.get('title', '')[:70]}"
 
        if DRY_RUN:
            print(f"\nDRY RUN {label}")
            print(f"  tweet: {text}")
            if reply:
                print(f"  reply: {reply}")
            state["posted"][post["id"]] = {
                "posted_at": time.time(),
                "title": post.get("title", "")[:120],
                "score": post.get("score", 0),
                "dry_run": True,
            }
            posted_count += 1
            continue
 
        try:
            poster.post(text, reply)
        except Exception as exc:
            # Leave it unrecorded so the next run retries it.
            print(f"FAILED {label}\n  {exc}")
            continue
 
        print(f"POSTED {label}")
        state["posted"][post["id"]] = {
            "posted_at": time.time(),
            "title": post.get("title", "")[:120],
            "score": post.get("score", 0),
        }
        posted_count += 1
        time.sleep(2)
 
    save_state(state)
    print(f"\nDone. {posted_count} posted, {len(state['posted'])} tracked.")
 
 
if __name__ == "__main__":
    main()
