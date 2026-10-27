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
    clean, errors = validate({"allowed_senders": "a@b.de\n@c.de\n", "relay_port": "587"})
    assert not errors
    assert clean["allowed_senders"] == ["a@b.de", "@c.de"]
    assert clean["relay_port"] == 587
    _, errors = validate({"allowed_networks": "nonsense", "allowed_senders": "no-at-sign"})
    assert len(errors) == 2
    _, errors = validate({"rewrite_enabled": True, "rewrite_from_address": ""})
    assert errors
