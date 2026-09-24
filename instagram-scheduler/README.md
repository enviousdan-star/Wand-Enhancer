# igsched: Instagram scheduler and comment manager for @nyacespb

A small command-line tool that uses the official Instagram Graph API to:

- **Schedule photo and carousel posts** (up to 10 images) and publish them on time.
- **Tag sponsors on a regular schedule.** Each sponsor gets a cadence: every post, every Nth post, or taking turns in a rotation. Sponsors can be tagged on the photo, @mentioned in the caption, and/or invited as a Collab.
- **Add hashtag sets** (e.g. `default`, `gameday`). It removes duplicates, keeps you under Instagram's 30-hashtag limit, and can put hashtags in the caption or in the first comment.
- **Keep images high resolution.** Photos are kept at up to 1440 px wide (the most Instagram stores) and never upscaled. It uses high-quality JPEG with 4:4:4 chroma, so sponsor logos and text stay sharp, and converts colour to sRGB. Photos are fitted to Instagram's 4:5–1.91:1 range by padding or cropping, and kept under the API's 8 MB limit. It warns you when a source photo is too small to look crisp.
- **Manage comments.** It syncs comments from recent posts, lists the unanswered ones, and lets you reply, hide, unhide or delete. It can also hide spam by keyword and send one auto-reply per comment for common questions (e.g. "tickets").

It doesn't depend on the rest of this repository.

## 1. One-time Instagram/Meta setup

The Graph API works only with **professional** Instagram accounts (Business or Creator).

1. In the Instagram app, switch @nyacespb to a Professional account if it isn't one already.
2. Go to <https://developers.facebook.com/apps>, create an app (type **Business**), and add the **Instagram** product. Pick one of these:
   - **Instagram API with Instagram Login.** This is the simplest option and doesn't need a Facebook Page. Set `api_host = "graph.instagram.com"`. Permissions: `instagram_business_basic`, `instagram_business_content_publish`, `instagram_business_manage_comments`.
   - **Instagram API with Facebook Login.** The account must be linked to a Facebook Page. Keep `api_host = "graph.facebook.com"`. Permissions: `instagram_basic`, `instagram_content_publish`, `instagram_manage_comments`, `pages_show_list`, `pages_read_engagement`.
3. Generate a **long-lived access token** for @nyacespb and note the numeric **Instagram user id** (the app dashboard shows both). Long-lived tokens expire after 60 days, so refresh them before then.
4. While the app is in Development mode it only works for accounts that have a role on the app. Adding @nyacespb as a tester or admin is enough for your own use. You don't need App Review.

## 2. Install

```bash
cd instagram-scheduler
python3 -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt                         # add boto3 if you use S3/R2 hosting
cp config.example.toml config.toml                      # config.toml is git-ignored
export IG_ACCESS_TOKEN="EAAG..."                        # Windows: set IG_ACCESS_TOKEN=EAAG...
python -m igsched check                                 # confirms the token and account
```

Python 3.11 or newer is required.

### Image hosting (required)

Instagram's publishing API doesn't take file uploads. At publish time it **downloads each JPEG from a public HTTPS URL**, so the prepared images need to live somewhere public:

- `mode = "s3"` (recommended): the tool uploads to an S3-compatible bucket. Cloudflare R2 has free egress. Set `s3_bucket` and `s3_endpoint_url`, and set `public_base_url` to the bucket's public domain (or leave it empty to use presigned URLs).
- `mode = "local"`: the tool writes files into `public_dir`. Something you run must serve that folder at `public_base_url`, for example your website's server, `cloudflared tunnel`, or `ngrok`.

## 3. Configure sponsors and hashtags

Edit `config.toml`. The sponsor block looks like this:

```toml
[[sponsors]]
handle = "main_sponsor"   # Instagram username, no @
cadence = "always"        # always | every_n | rotate | manual
tag_in_photo = true       # tag on the image (cover image for carousels)
mention_in_caption = true # added to "Thanks to our partners @..."
collaborator = false      # Collab invite; post also appears on their profile once they accept (max 3)

[[sponsors]]
handle = "venue_partner"
cadence = "every_n"
every_n = 3               # posts 1, 4, 7, ...
```

Sponsors with `cadence = "rotate"` take turns. `captions.rotation_slots` sets how many of them appear on each post. To preview who gets tagged next:

```bash
python -m igsched sponsors --next 8
python -m igsched hashtags
```

## 4. Schedule posts

```bash
# Single photo, default hashtags, automatic sponsors
python -m igsched schedule -i photos/win.jpg --caption "What a match! 🏐" --at "2026-10-03 19:30"

# Carousel with game-day hashtags, extra tags, alt text, and a guest sponsor on top of the rotation
python -m igsched schedule -i a.jpg -i b.jpg -i c.jpg \
    --caption-file captions/opener.txt --at "2026-10-04 12:00" \
    --hashtags default,gameday --tag "SeasonOpener,Brooklyn" \
    --sponsors auto,guest_brand --alt "Team huddle" --alt "Serve" --alt "Celebration"

# Preview without saving or uploading
python -m igsched schedule -i a.jpg --caption "..." --at now --dry-run

# Check a photo before you schedule it (writes a.instagram.jpg next to it)
python -m igsched prep-image a.jpg --ratio portrait
```

`--sponsors` accepts `auto` (the default), `none`, a list of handles, or `auto,+extra`.
`--ratio` accepts `portrait` (4:5, which takes up the most space in the feed), `square`, `landscape`, or `W:H`.
Carousels are automatically fitted to the cover image's ratio, because Instagram displays every slide at that ratio.

Managing the queue:

```bash
python -m igsched list            # upcoming + failed   (--all for history)
python -m igsched show 12
python -m igsched reschedule 12 --at "2026-10-05 09:00"
python -m igsched publish-now 12
python -m igsched cancel 12
```

## 5. Keep the publisher running

Posts only go out while the publisher is running:

```bash
python -m igsched run             # checks every 60 s, syncs comments every 15 min
```

You can run it on an always-on machine (a small VPS, Raspberry Pi, or home server) with `systemd`, `pm2`, or `tmux`. Alternatively, schedule a single pass every few minutes:

```cron
*/5 * * * * cd /path/to/instagram-scheduler && IG_ACCESS_TOKEN=... .venv/bin/python -m igsched run --once
```

On Windows, Task Scheduler running `python -m igsched run --once` every 5 minutes does the same job.

Retries: temporary API errors are retried up to 3 times, 5 minutes apart. Permanent errors mark the post `failed` and record the reason, which you can see with `show`. After fixing the problem, use `reschedule` or `publish-now`.

## 6. Comments

```bash
python -m igsched comments sync                 # pull comments + apply hide/auto-reply rules
python -m igsched comments list --unanswered
python -m igsched comments reply 17912345 "Thanks! See you Saturday 🙌"
python -m igsched comments hide 17912345        # or unhide / delete
```

Rules live under `[comments]` in the config: `hide_keywords`, plus `[[comments.auto_replies]]` entries with `match` keywords and a `reply` (where `{username}` gets filled in). Each comment gets at most one automatic action. A comment counts as answered once @nyacespb has replied to it, including replies made from the Instagram app.

## Limits to know (from the Instagram API)

- Feed photos and carousels only. Reels and Stories aren't supported by this tool.
- JPEG only (the tool converts for you). Aspect ratio must be 4:5–1.91:1. 8 MB max per image, 10 items per carousel.
- 30 hashtags, 20 photo tags and 2,200 caption characters at most. Your account can publish up to 100 API posts per 24 h (`check` shows your usage).
- Photo tags and @mentions only work for public accounts. A private or misspelled sponsor handle makes Instagram reject the post, and you'll see the error in `show`.
- Instagram's **Paid partnership** label can't be set through the API. Collab invites (`collaborator = true`) are sent, but each sponsor has to accept them in the app.
- If the publisher is killed in the middle of a publish, the post is marked `failed` with a note to check Instagram first. That way it never posts twice by accident.

## Tests

```bash
pip install pytest && python -m pytest -q
```
