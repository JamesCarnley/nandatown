"""What `a2a test` says about an endpoint it cannot even address."""

import pytest

from nandatown.a2a_adapter import probe_endpoint


@pytest.mark.parametrize("url", [
    pytest.param("http://xn--.localhost:9", id="empty-a-label"),
    pytest.param("http://xn--a.localhost:9", id="undecodable-a-label"),
    pytest.param("http://[v1.fe80::a+en1]:9", id="bad-ipv6"),
    pytest.param("http://[::1", id="unparseable"),
    pytest.param("http://", id="no-host"),
])
def test_an_unusable_url_is_reported_rather_than_raised(url):
    """`a2a test` exists to say what is wrong with an endpoint.

    httpx.InvalidURL is not an httpx.HTTPError, so an address httpx
    cannot parse escaped the probe and ended the command in a
    traceback instead of appearing in its problems list.
    """
    report = probe_endpoint(url)

    assert report["ok"] is False
    assert report["problems"]
