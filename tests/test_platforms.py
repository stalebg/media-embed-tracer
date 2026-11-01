"""URL -> canonical id matching."""

import pytest

from media_embed_tracer.platforms import match_url


@pytest.mark.parametrize(("url", "key", "author"), [
    # --- X ---
    ("https://twitter.com/user_a/status/123456789", "x:123456789", "@user_a"),
    ("https://www.twitter.com/user_a/status/123456789?s=20", "x:123456789", "@user_a"),
    ("https://mobile.twitter.com/user_a/status/123456789", "x:123456789", "@user_a"),
    ("https://www.x.com/user_a/status/123456789/photo/1", "x:123456789", "@user_a"),
    ("https://mobile.x.com/user_a/statuses/123456789", "x:123456789", "@user_a"),
    ("https://x.com/i/web/status/123456789", "x:123456789", ""),
    ("https://x.com/i/status/123456789", "x:123456789", ""),          # not "@i"
    ("https://platform.twitter.com/embed/Tweet.html?id=123456789", "x:123456789", ""),
    # --- Bluesky ---
    ("https://bsky.app/profile/did:plc:abcdefghijklmnopqrstuvwx/post/3kabc", "bluesky:at://did:plc:abcdefghijklmnopqrstuvwx/app.bsky.feed.post/3kabc", "did:plc:abcdefghijklmnopqrstuvwx"),
    ("https://bsky.app/profile/did:web:example.com/post/3kabc", "bluesky:at://did:web:example.com/app.bsky.feed.post/3kabc", "did:web:example.com"),
    ("https://embed.bsky.app/embed/did:plc:abcdefghijklmnopqrstuvwx/app.bsky.feed.post/3kabc?id=1", "bluesky:at://did:plc:abcdefghijklmnopqrstuvwx/app.bsky.feed.post/3kabc", "did:plc:abcdefghijklmnopqrstuvwx"),
    ("at://did:plc:abcdefghijklmnopqrstuvwx/app.bsky.feed.post/3kabc", "bluesky:at://did:plc:abcdefghijklmnopqrstuvwx/app.bsky.feed.post/3kabc", "did:plc:abcdefghijklmnopqrstuvwx"),
    ("https://bsky.app/profile/Some.Handle.bsky.social/post/3kabc", "bluesky:at://some.handle.bsky.social/app.bsky.feed.post/3kabc", "@some.handle.bsky.social"),
    # --- TikTok ---
    ("https://www.tiktok.com/@user.x/video/7300000000000000001?lang=en", "tiktok:7300000000000000001", "@user.x"),
    ("https://www.tiktok.com/@user.x/photo/7300000000000000001", "tiktok:7300000000000000001", "@user.x"),
    ("https://www.tiktok.com/embed/v2/7300000000000000001", "tiktok:7300000000000000001", ""),
    ("https://www.tiktok.com/player/v1/7300000000000000001?embed_from=oembed", "tiktok:7300000000000000001", ""),
    ("https://m.tiktok.com/v/7300000000000000001.html", "tiktok:7300000000000000001", ""),
    # --- Instagram ---
    ("https://www.instagram.com/p/CAbc123/", "instagram:CAbc123", ""),
    ("https://instagram.com/reel/CAbc123/?igsh=x", "instagram:CAbc123", ""),
    ("https://www.instagram.com/reels/CAbc123/", "instagram:CAbc123", ""),
    ("https://www.instagram.com/some.user/p/CAbc123/", "instagram:CAbc123", "@some.user"),
    ("https://www.instagram.com/p/CAbc123/embed/captioned/", "instagram:CAbc123", ""),
    # --- Facebook ---
    ("https://www.facebook.com/watch/?v=111111", "facebook:video:111111", ""),
    ("https://www.facebook.com/photo/?fbid=111111", "facebook:photo:111111", ""),
    ("https://www.facebook.com/photo.php?fbid=222222&set=a.1", "facebook:photo:222222", ""),
    ("https://www.facebook.com/permalink.php?story_fbid=333333&amp;amp;id=444444", "facebook:post:333333", "444444"),
    ("https://www.facebook.com/share/p/AbCdE12345/", "facebook:share:p:AbCdE12345", ""),
    ("https://www.facebook.com/share/v/AbCdE12345/", "facebook:share:v:AbCdE12345", ""),
    ("https://www.facebook.com/share/r/AbCdE12345/", "facebook:share:r:AbCdE12345", ""),
    ("https://www.facebook.com/groups/mygroup/posts/555555/", "facebook:post:555555", "group:mygroup"),
    ("https://www.facebook.com/groups/mygroup/permalink/555555/", "facebook:post:555555", "group:mygroup"),
    ("https://m.facebook.com/somepage/posts/666666", "facebook:post:666666", "somepage"),
    ("https://web.facebook.com/somepage/videos/777777/", "facebook:video:777777", "somepage"),
    ("https://www.facebook.com/somepage/videos/a-title/777777/", "facebook:video:777777", "somepage"),
    ("https://www.facebook.com/reel/888888", "facebook:video:888888", ""),
    ("https://fb.watch/abcDEF_12/", "facebook:fbwatch:abcDEF_12", ""),
    ("https://www.facebook.com/plugins/video.php?href=https%3A%2F%2Fwww.facebook.com%2Fwatch%2F%3Fv%3D999999", "facebook:video:999999", ""),
    # --- New platforms ---
    ("https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ", "youtube:dQw4w9WgXcQ", ""),
    ("https://m.youtube.com/watch?v=dQw4w9WgXcQ", "youtube:dQw4w9WgXcQ", ""),
    ("https://youtu.be/dQw4w9WgXcQ", "youtube:dQw4w9WgXcQ", ""),
    ("https://www.threads.com/@u.ser/post/DAbc123", "threads:DAbc123", "@u.ser"),
    ("https://www.threads.net/@u.ser/post/DAbc123/", "threads:DAbc123", "@u.ser"),
    ("https://mastodon.social/@user/113000000000000001", "mastodon:mastodon.social/113000000000000001", "@user@mastodon.social"),
    ("https://mastodon.social/@user/42/embed", "mastodon:mastodon.social/42", "@user@mastodon.social"),
    ("https://www.reddit.com/r/sub/comments/abc123/title/", "reddit:t3_abc123", ""),
    ("https://embed.reddit.com/r/sub/comments/abc123/title/def456/", "reddit:t1_def456", ""),
    ("https://redd.it/abc123", "reddit:t3_abc123", ""),
    ("https://t.me/SomeChannel/123", "telegram:somechannel/123", "@SomeChannel"),
    ("https://t.me/SomeChannel/123?embed=1", "telegram:somechannel/123", "@SomeChannel"),
    ("https://t.me/c/1234567890/12", "telegram:c/1234567890/12", ""),
    ("https://www.linkedin.com/embed/feed/update/urn:li:ugcPost:7100000000000000001", "linkedin:urn:li:ugcPost:7100000000000000001", ""),
    ("https://www.linkedin.com/feed/update/urn%3Ali%3Aactivity%3A7100000000000000001/", "linkedin:urn:li:activity:7100000000000000001", ""),
])
def test_match(url, key, author):
    ref = match_url(url)
    assert ref is not None, url
    assert ref.key == key
    assert ref.author == author


@pytest.mark.parametrize("url", [
    "https://x.com/someuser",
    "https://twitter.com/intent/tweet?text=hi",
    "https://www.instagram.com/reels/audio/1234567890123/",   # audio page, not a post
    "https://www.instagram.com/some.user/",
    "https://www.instagram.com/explore/p/CAbc123/",
    "https://www.facebook.com/sharer/sharer.php?u=https%3A%2F%2Fexample.com",
    "https://www.facebook.com/somepage",
    "https://www.facebook.com/watch/",
    "https://www.youtube.com/embed/videoseries?list=PL1",
    "https://www.youtube.com/channel/UC123",
    "https://medium.com/@user/12345678901234567890",
    "https://t.me/joinchat/AAAA",
    "https://www.linkedin.com/in/someone/",
    "https://bsky.app/profile/someone.bsky.social",
    "https://example.com/@user/notanid",
    "mailto:someone@example.com",
    "",
])
def test_no_match(url):
    assert match_url(url) is None


def test_facebook_video_and_photo_do_not_collapse():
    """watch?v= and photo?fbid= get different keys."""
    a = match_url("https://www.facebook.com/watch/?v=123456")
    b = match_url("https://www.facebook.com/photo?fbid=123456")
    c = match_url("https://www.facebook.com/watch/?v=654321")
    assert len({a.key, b.key, c.key}) == 3


def test_x_dedup_on_status_id():
    """Different hosts and users for the same status share one key."""
    urls = ["https://twitter.com/a/status/42", "https://x.com/A/status/42?s=1",
            "https://x.com/i/web/status/42", "https://platform.twitter.com/embed/Tweet.html?id=42"]
    assert {match_url(u).key for u in urls} == {"x:42"}


def test_instagram_p_and_reel_same_shortcode():
    assert match_url("https://www.instagram.com/p/CXyz12/").key == match_url(
        "https://www.instagram.com/reel/CXyz12/").key


def test_tiktok_short_link_is_marked_unresolved():
    ref = match_url("https://vm.tiktok.com/ZMabc123/")
    assert ref.unresolved == "tiktok-short"
    assert match_url("https://www.tiktok.com/t/ZTabc123/").unresolved == "tiktok-short"


# --- archive snapshot links are matched as the original URL ---------------------------
@pytest.mark.parametrize(("url", "key"), [
    ("https://web.archive.org/web/20260209200936mp_/https://bsky.app/profile/x.bsky.social/post/abc",
     "bluesky:at://x.bsky.social/app.bsky.feed.post/abc"),
    ("https://web.archive.org/web/20260209200936/https://x.com/u/status/123?s=20", "x:123"),
    ("https://web.archive.org/web/20260209200936id_/https://twitter.com/u/status/123", "x:123"),
    ("http://web.archive.org/web/20260209200936if_/https://www.youtube.com/embed/dQw4w9WgXcQ",
     "youtube:dQw4w9WgXcQ"),
    ("https://web.archive.org/web/2026/http://x.com/u/status/123", "x:123"),
    ("https://web.archive.org/web/20260209200936/https:/x.com/u/status/123", "x:123"),    # collapsed //
    ("https://web.archive.org/web/20260209200936/x.com/u/status/123", "x:123"),            # no scheme
    ("https://web.archive.org/web/1/https://web.archive.org/web/2/https://x.com/u/status/123", "x:123"),
    ("https://archive.org/web/20260209200936/https://x.com/u/status/123", "x:123"),
    ("https://archive.ph/2026.02.09-200936/https://x.com/u/status/123", "x:123"),
    ("https://archive.today/o/AbCd1/https://x.com/u/status/123", "x:123"),
    ("https://archive.is/newest/https://x.com/u/status/123", "x:123"),
])
def test_archive_links_unwrap(url, key):
    ref = match_url(url)
    assert ref is not None and ref.key == key


@pytest.mark.parametrize("url", [
    "https://web.archive.org/web/20260209200936/https://news.test/article",   # not a post
    "https://archive.ph/AbCdE",                                               # short id: no original URL
    "https://example.org/web/20260209200936/https://x.com/u/status/123",      # not an archive host
])
def test_archive_links_without_a_post_do_not_match(url):
    assert match_url(url) is None
