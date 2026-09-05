# Reddit → X upvote bot

Watches a subreddit. When a post crosses your upvote threshold, it tweets it. Runs
on GitHub Actions every 15 minutes, free, whether or not your computer is on.

## Why it goes through Buffer

X killed the free API tier in February 2026. Posting directly through the X API is
now pay-per-use: **$0.015 per plain post, $0.20 per post containing a URL**. At a
handful of tweets a day with links, that's real money.

Buffer's free plan connects an X channel and publishes to it at no cost, and Buffer's
API is available on every plan including Free. So this bot hands the tweet to Buffer,
and Buffer publishes it to X. Free end to end.

If you'd rather pay X directly later, flip `POSTER` to `x_api` and fill in the four
X secrets. Nothing else changes.

---

## Setup (~15 minutes)

### 1. Create the repo

Make a new GitHub repo and drop these files in:

```
bot.py
requirements.txt
posted.json
.github/workflows/reddit-to-x.yml
```

A **public** repo gets unlimited free Actions minutes. Private repos get 2,000
min/month on the free plan, and this bot uses roughly 1 minute per run — at every
15 minutes that's ~2,900 min/month, which would exceed it. **Use a public repo**, or
change the cron to `*/30` and stay under the cap.

### 2. Reddit credentials

1. Go to https://www.reddit.com/prefs/apps
2. **Create another app...**
3. Name: anything. Type: **script**. Redirect URI: `http://localhost:8080`
4. Create it. You'll see:
   - the **client ID** — the string just under the app name, top-left
   - the **secret** — labeled `secret`

No Reddit password needed; the bot uses app-only auth.

### 3. Buffer credentials

1. Sign up at https://buffer.com (free plan) and connect your X account as a channel.
2. Go to https://publish.buffer.com/developers/apps and create an app.
   Any name/URL works — nothing is published.
3. After creating it, Buffer generates a **personal access token** for you on that
   page. Copy it.
4. `BUFFER_PROFILE_ID` is optional. Leave it unset and the bot finds your X channel
   automatically; if you have more than one X channel connected, the bot will error
   out and print the IDs so you can pick.

### 4. Add the secrets

Repo → **Settings → Secrets and variables → Actions → New repository secret**:

| Secret | Value |
|---|---|
| `REDDIT_CLIENT_ID` | from step 2 |
| `REDDIT_CLIENT_SECRET` | from step 2 |
| `BUFFER_ACCESS_TOKEN` | from step 3 |
| `BUFFER_PROFILE_ID` | optional — only if you have multiple X channels |

### 5. Set your subreddit and threshold

Edit the `env:` block in `.github/workflows/reddit-to-x.yml`:

```yaml
SUBREDDIT: nba
MIN_SCORE: "2000"
USER_AGENT: "script:reddit-to-x:v1.0 (by /u/YOUR_REDDIT_USERNAME)"
```

Put your real Reddit username in `USER_AGENT` — Reddit rate-limits generic ones harder.

### 6. Test before going live

Repo → **Actions → Reddit to X → Run workflow**, leave **dry run** checked.

The log will show exactly which posts would have been tweeted and what the text
would look like — without posting anything. This is also where you'll find out if a
credential is wrong. Once it looks right, run it again with dry run **unchecked**.

**First real run posts nothing.** It records everything currently above your
threshold as already-seen, so you don't get a burst of tweets about day-old posts.
From the second run on, it only tweets posts that newly cross the line.

---

## Settings

All in the workflow's `env:` block.

| Setting | Default | What it does |
|---|---|---|
| `SUBREDDIT` | — | Subreddit name, no `r/` |
| `MIN_SCORE` | `500` | Upvote threshold |
| `LINK_MODE` | `inline` | `inline` = title + link · `none` = title only · `reply` = link in a reply |
| `POSTER` | `buffer` | `buffer` (free) or `x_api` (paid) |
| `MAX_POSTS_PER_RUN` | `3` | Burst guard. Extras roll to the next run |
| `MAX_AGE_HOURS` | `48` | Ignore posts older than this |
| `SKIP_NSFW` | `true` | Skip NSFW posts |
| `SKIP_STICKIED` | `true` | Skip pinned/mod posts (game threads, etc.) |
| `FLAIR_ALLOWLIST` | — | Comma-separated flairs; only these get tweeted |
| `TWEET_PREFIX` / `TWEET_SUFFIX` | — | Wrap the title, e.g. `TWEET_PREFIX: "[r/nba]"` |

Titles are truncated to fit 280 characters, counting URLs as 23 the way X does.

## How it avoids duplicates

`posted.json` tracks every post ID it has tweeted. The workflow commits it back to
the repo after each run, so state survives across runs. Entries older than 30 days
are pruned. If a tweet fails to send, the ID is *not* recorded, so the next run
retries it.

## Notes

- The bot checks both `hot` and `top of day`, so it catches slow risers, not just
  posts that spike immediately.
- GitHub's cron is best-effort and can run late under load. Harmless here.
- Buffer's free plan caps *queued* posts at 10 per channel. This bot publishes
  immediately (`now=true`) rather than queueing, so that cap shouldn't bind.
- If you moderate the subreddit you're pulling from, double-check its rules on
  automated cross-posting before pointing this at it.

## Local testing

```bash
pip install -r requirements.txt
cp .env.example .env      # fill it in
set -a && source .env && set +a
python bot.py             # DRY_RUN=true in .env keeps it safe

python test_bot.py        # offline logic tests, no credentials needed
```
