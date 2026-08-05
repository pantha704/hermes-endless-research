"""URL intelligence tests for hermes-endless-research (conservative canonicalisation)."""
import research_project as rp


def _canon(url):
    return rp.canonicalize_url(url)


def test_always_strips_utm_tracking_params():
    assert _canon("https://site.com/a?utm_source=x&utm_medium=email&id=7") == \
        "https://site.com/a?id=7"


def test_always_strips_fbclid_and_gclid():
    assert _canon("https://site.com/a?fbclid=abc&x=1") == "https://site.com/a?x=1"


def test_does_NOT_strip_www_by_default():
    # www.example.com and example.com can be different sites -> keep host as-is.
    assert _canon("https://www.example.com/x") == "https://www.example.com/x"


def test_strip_www_when_opt_in():
    assert rp.canonicalize_url("https://www.example.com/x", strip_www=True) == \
        "https://example.com/x"


def test_does_NOT_strip_ref_by_default():
    # ref can be semantically meaningful on some sites -> preserved.
    assert _canon("https://site.com/a?ref=home") == "https://site.com/a?ref=home"


def test_strips_conditional_param_when_opted_in():
    assert rp.canonicalize_url("https://site.com/a?ref=home&id=1",
                               conditional_params=frozenset({"ref"})) == \
        "https://site.com/a?id=1"


def test_ref_variants_do_NOT_collide_by_default():
    # Two ref values must NOT merge into one page by default.
    assert _canon("https://site.com/a?ref=home") != _canon("https://site.com/a?ref=about")


def test_strips_fragment():
    assert _canon("https://site.com/a#section") == "https://site.com/a"


def test_sorts_query_params():
    a = _canon("https://site.com/a?b=2&a=1")
    b = _canon("https://site.com/a?a=1&b=2")
    assert a == b == "https://site.com/a?a=1&b=2"


def test_tracking_variants_share_identity():
    # Same host, same path, only always-safe tracking params differ -> identical.
    assert _canon("https://site.com/article") == \
        _canon("https://site.com/article?utm_source=news&fbclid=abc")


def test_www_variants_do_NOT_share_identity_by_default():
    assert _canon("https://site.com/a") != _canon("https://www.site.com/a")


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
    # conservative: www kept, utm stripped
    assert "https://www.a.test/p?id=1" in out
    assert "scope" in out
    assert "follow_internal_links" in out
