"""URL intelligence tests for hermes-endless-research (v0.2.0)."""
import research_project as rp


def _canon(url):
    return rp.canonicalize_url(url)


def test_strips_tracking_params():
    assert _canon("https://site.com/a?utm_source=x&utm_medium=email&id=7") == \
        "https://site.com/a?id=7"


def test_normalises_www():
    assert _canon("https://www.example.com/x") == "https://example.com/x"


def test_strips_fragment():
    assert _canon("https://site.com/a#section") == "https://site.com/a"


def test_sorts_query_params():
    # stable identity regardless of parameter order
    a = _canon("https://site.com/a?b=2&a=1")
    b = _canon("https://site.com/a?a=1&b=2")
    assert a == b == "https://site.com/a?a=1&b=2"


def test_tracking_variants_share_identity():
    assert _canon("https://site.com/article") == \
        _canon("https://www.site.com/article?utm_source=news&fbclid=abc")


def test_content_fingerprint_stable_and_whitespace_agnostic():
    h1 = rp.content_fingerprint("Hello   World")
    h2 = rp.content_fingerprint("hello\nworld")
    assert h1 == h2
    assert h1.startswith("sha256:")


def test_inspect_reports_canonical_and_scope(project, cli):
    code, out, err = cli("inspect", project,
                         "https://www.a.test/p?utm_source=x&id=1")
    assert code == 0
    assert "canonical_url" in out
    assert "https://a.test/p?id=1" in out
    assert "scope" in out
    assert "follow_internal_links" in out
