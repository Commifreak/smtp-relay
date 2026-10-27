from app.config import address_matches, ip_allowed, validate


def test_address_matches():
    pats = ["dienst@intern.lan", "@example.de", "*@foo.org"]
    assert address_matches("Dienst@Intern.lan", pats)
    assert address_matches("x@example.de", pats)
    assert address_matches("y@foo.org", pats)
    assert not address_matches("x@sub.example.de", pats)
    assert not address_matches("other@intern.lan", pats)
    assert address_matches("any@thing", ["*"])
    assert not address_matches("a@b.c", [])


def test_ip_allowed():
    nets = ["192.168.0.0/16", "::1/128"]
    assert ip_allowed("192.168.1.10", nets)
    assert ip_allowed("::ffff:192.168.1.10", nets)
    assert ip_allowed("::1", nets)
    assert not ip_allowed("10.0.0.1", nets)


def test_validate():
    clean, errors = validate({"allowed_recipients": "a@b.de\n@c.de\n", "relay_timeout": "10"})
    assert not errors
    assert clean["allowed_recipients"] == ["a@b.de", "@c.de"]
    assert clean["relay_timeout"] == 10
    _, errors = validate({"allowed_networks": "nonsense", "allowed_recipients": "no-at-sign"})
    assert len(errors) == 2
    _, errors = validate({"proxy_auth_enabled": True, "proxy_auth_trusted_networks": ""})
    assert errors
