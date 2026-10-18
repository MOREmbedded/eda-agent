# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""The credential-gated distributor catalogues.

These sources yield part IDENTITY (MPN, datasheet, stock) and never
geometry, so most of what is worth guarding is about how they FAIL. An
unconfigured or rejected catalogue that returns an empty list tells the
caller the part does not exist, which is both wrong and unrecoverable:
they stop looking.

The sharpest case here is measured rather than imagined. Mouser answers
an invalid API key with HTTP **200** and an ``Errors`` array, confirmed
against the live endpoint. Any client that judged success by status code
alone would report a rejected credential as a successful search that
matched nothing.
"""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

from eda_agent.libimport.providers._distributor import (
    DistributorProvider,
    first_string,
)
from eda_agent.libimport.providers.base import (
    ProviderError,
    ProviderUnavailable,
)
from eda_agent.libimport.providers.distributors import (
    DigiKeyProvider,
    Element14Provider,
    MouserProvider,
    NexarProvider,
    TmeProvider,
)

ALL_DISTRIBUTORS = (DigiKeyProvider, Element14Provider, MouserProvider,
                    NexarProvider, TmeProvider)


@pytest.fixture(autouse=True)
def _no_ambient_credentials(monkeypatch):
    """A developer's real keys must not change what these tests prove."""
    for cls in ALL_DISTRIBUTORS:
        for var in cls.env_vars:
            monkeypatch.delenv(var, raising=False)


def _respond(monkeypatch, payload, status=200):
    """Stub the transport with one JSON reply."""
    body = json.dumps(payload).encode("utf-8")

    class _Response(io.BytesIO):
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(
        "eda_agent.libimport.providers._distributor.urllib.request.urlopen",
        lambda *a, **k: _Response(body))


def _raise(monkeypatch, exc):
    def _boom(*a, **k):
        raise exc
    monkeypatch.setattr(
        "eda_agent.libimport.providers._distributor.urllib.request.urlopen",
        _boom)


# ---- refusing to run unconfigured ------------------------------------

@pytest.mark.parametrize("cls", ALL_DISTRIBUTORS)
def test_unconfigured_is_unavailable_not_empty(cls):
    """"No key" must never look like "no such part".

    An empty list here would end the search: the caller has no way to
    tell a silent source from an exhaustive one.
    """
    with pytest.raises(ProviderUnavailable) as excinfo:
        cls().search("STM32F103")
    message = str(excinfo.value)
    for var in cls.env_vars:
        assert var in message, (
            f"{cls.name} must name {var}; 'not configured' is not "
            f"something the user can act on")


@pytest.mark.parametrize("cls", ALL_DISTRIBUTORS)
def test_no_credential_ships_with_the_project(cls):
    """No default key, for the same reason no default registry ships.

    A bundled credential would make this project the operator's client
    and put every user's traffic through one account.
    """
    provider = cls()
    with pytest.raises(ProviderUnavailable):
        provider._credentials()


# ---- the measured trap ------------------------------------------------

def test_an_error_under_http_200_is_not_an_empty_result(monkeypatch):
    """Measured live: Mouser answers a bad key with 200 + Errors.

    This is the single most important assertion in the file. Judged on
    status alone the search "succeeded" and found nothing.
    """
    monkeypatch.setenv("MOUSER_API_KEY", "wrong-key")
    _respond(monkeypatch, {
        "Errors": [{"Id": 0, "Code": "Invalid",
                    "Message": "Invalid unique identifier."}],
        "SearchResults": None,
    })

    with pytest.raises(ProviderUnavailable) as excinfo:
        MouserProvider().search("STM32F103")

    assert "NOT an empty result" in str(excinfo.value)


def test_an_empty_errors_key_is_still_a_success(monkeypatch):
    """These APIs send the key unconditionally; empty means fine.

    Without this the guard above would reject every successful search,
    which is the failure mode that gets a safety check deleted.
    """
    monkeypatch.setenv("MOUSER_API_KEY", "good-key")
    _respond(monkeypatch, {
        "Errors": [],
        "SearchResults": {"Parts": [
            {"ManufacturerPartNumber": "STM32F103C8T6",
             "Manufacturer": "STMicroelectronics",
             "Description": "ARM MCU",
             "DataSheetUrl": "https://example.invalid/ds.pdf"},
        ]},
    })

    hits = MouserProvider().search("STM32F103")
    assert [h.mpn for h in hits] == ["STM32F103C8T6"]
    assert hits[0].datasheet.endswith("ds.pdf")


# ---- failure classification ------------------------------------------

def test_a_rejected_key_is_unavailable_not_an_error(monkeypatch):
    """401 means "fix your key", which is a different act from "retry"."""
    monkeypatch.setenv("MOUSER_API_KEY", "expired")
    _raise(monkeypatch, urllib.error.HTTPError(
        "https://api.mouser.com/x", 401, "Unauthorized", {}, None))

    with pytest.raises(ProviderUnavailable) as excinfo:
        MouserProvider().search("anything")
    assert "MOUSER_API_KEY" in str(excinfo.value)


def test_rate_limiting_says_the_part_may_still_exist(monkeypatch):
    monkeypatch.setenv("MOUSER_API_KEY", "k")
    _raise(monkeypatch, urllib.error.HTTPError(
        "https://api.mouser.com/x", 429, "Too Many", {}, None))

    with pytest.raises(ProviderUnavailable) as excinfo:
        MouserProvider().search("anything")
    assert "may still exist" in str(excinfo.value)


def test_a_network_failure_is_unavailable_not_empty(monkeypatch):
    monkeypatch.setenv("MOUSER_API_KEY", "k")
    _raise(monkeypatch, urllib.error.URLError("no route to host"))

    with pytest.raises(ProviderUnavailable) as excinfo:
        MouserProvider().search("anything")
    assert "not evidence" in str(excinfo.value)


def test_a_server_error_is_an_error_not_a_credential_problem(monkeypatch):
    """500 must not send the user off to re-check a key that is fine."""
    monkeypatch.setenv("MOUSER_API_KEY", "k")
    _raise(monkeypatch, urllib.error.HTTPError(
        "https://api.mouser.com/x", 500, "Server Error", {}, None))

    with pytest.raises(ProviderError) as excinfo:
        MouserProvider().search("anything")
    assert not isinstance(excinfo.value, ProviderUnavailable)


# ---- credentials must not leak ---------------------------------------

def test_a_key_in_the_query_string_never_reaches_an_error_message(
        monkeypatch):
    """Mouser puts the key in the URL, so errors must not echo the URL.

    Not hypothetical: these messages are returned to the caller and end
    up in logs and transcripts. The provider does not choose where the
    key goes, but it does choose what it repeats back.
    """
    secret = "SUPERSECRETKEY123"
    monkeypatch.setenv("MOUSER_API_KEY", secret)
    _raise(monkeypatch, urllib.error.HTTPError(
        f"https://api.mouser.com/api/v1/search/keyword?apiKey={secret}",
        500, "Server Error", {}, None))

    with pytest.raises(ProviderError) as excinfo:
        MouserProvider().search("anything")
    assert secret not in str(excinfo.value), (
        "the API key leaked into an error message")


def test_a_transport_failure_does_not_echo_the_credential(monkeypatch):
    """URLError stringifies whatever it was given; check the real path."""
    secret = "ANOTHERSECRET456"
    monkeypatch.setenv("MOUSER_API_KEY", secret)
    _raise(monkeypatch, urllib.error.URLError("connection refused"))

    with pytest.raises(ProviderUnavailable) as excinfo:
        MouserProvider().search("anything")
    assert secret not in str(excinfo.value)


# ---- what a catalogue is ----------------------------------------------

@pytest.mark.parametrize("cls", ALL_DISTRIBUTORS)
def test_a_catalogue_yields_no_geometry(cls):
    """The distinction that stops a caller choosing an unbuildable part."""
    provider = cls()
    assert provider.kind == "catalogue"
    assert provider.formats == ()
    assert provider.native_to == ()


@pytest.mark.parametrize("cls", ALL_DISTRIBUTORS)
def test_endpoints_are_https_and_not_placeholders(cls):
    """Every URL here was probed live before being written down."""
    provider = cls()
    urls = [v for k, v in vars(cls).items()
            if k.endswith("_URL") and isinstance(v, str)]
    assert urls, f"{provider.name} declares no endpoint"
    for url in urls:
        assert url.startswith("https://"), f"{url} is not https"
        assert "example" not in url and "TODO" not in url


@pytest.mark.parametrize("cls", ALL_DISTRIBUTORS)
def test_the_unverified_claim_is_published_not_hidden(cls):
    """The endpoint was measured; the response shape was not.

    Saying so is the same discipline the tool catalog already applies to
    maturity: a claim nobody checked is worth less than an honest blank.
    """
    assert cls.verified_live is False, (
        "flip this only when the client has actually run against the "
        "live API with a real credential")


def test_a_catalogue_hit_states_it_carries_no_symbol(monkeypatch):
    monkeypatch.setenv("MOUSER_API_KEY", "k")
    _respond(monkeypatch, {"SearchResults": {"Parts": [
        {"ManufacturerPartNumber": "NE555P", "Manufacturer": "TI"}]}})

    hit = MouserProvider().search("NE555")[0]
    assert "no symbol or footprint" in hit.provenance


# ---- shape drift ------------------------------------------------------

def test_a_renamed_field_degrades_a_hit_rather_than_losing_the_search():
    """Distributor payloads rename fields between API versions.

    Losing the whole result set because one key moved would turn a
    cosmetic upstream change into an outage.
    """
    assert first_string({"a": {"b": "x"}}, ("a", "b")) == "x"
    assert first_string({"old": "v"}, ("new",), ("old",)) == "v"
    assert first_string({"a": None}, ("a",)) == ""
    # A non-dict midway must not raise.
    assert first_string({"a": "scalar"}, ("a", "b")) == ""


def test_a_result_missing_its_mpn_is_skipped_not_faked(monkeypatch):
    """A hit with no part number is not addressable, so it is dropped."""
    monkeypatch.setenv("MOUSER_API_KEY", "k")
    _respond(monkeypatch, {"SearchResults": {"Parts": [
        {"Manufacturer": "TI", "Description": "mystery"},
        {"ManufacturerPartNumber": "NE555P", "Manufacturer": "TI"},
    ]}})

    hits = MouserProvider().search("x")
    assert [h.mpn for h in hits] == ["NE555P"]


def test_a_non_json_body_is_reported_as_such(monkeypatch):
    monkeypatch.setenv("MOUSER_API_KEY", "k")

    class _Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(
        "eda_agent.libimport.providers._distributor.urllib.request.urlopen",
        lambda *a, **k: _Response(b"<html>maintenance</html>"))

    with pytest.raises(ProviderError) as excinfo:
        MouserProvider().search("x")
    assert "not JSON" in str(excinfo.value)


# ---- the signed one ---------------------------------------------------

def test_tme_signature_is_deterministic_and_key_dependent():
    """A signature that ignored the secret would authenticate nothing."""
    provider = TmeProvider()
    params = {"Token": "t", "SearchPlain": "NE555"}
    one = provider._sign(TmeProvider._SEARCH_URL, params, "secret-a")
    two = provider._sign(TmeProvider._SEARCH_URL, params, "secret-a")
    three = provider._sign(TmeProvider._SEARCH_URL, params, "secret-b")

    assert one == two, "signing must be deterministic"
    assert one != three, "the signature must depend on the secret"
    assert one != provider._sign(
        TmeProvider._SEARCH_URL, {"Token": "t", "SearchPlain": "LM358"},
        "secret-a"), "the signature must depend on the parameters"


def test_tme_signature_does_not_depend_on_parameter_order():
    """TME sorts parameters before signing; a dict order must not leak."""
    provider = TmeProvider()
    a = provider._sign("https://api.tme.eu/x",
                       {"A": "1", "B": "2"}, "s")
    b = provider._sign("https://api.tme.eu/x",
                       {"B": "2", "A": "1"}, "s")
    assert a == b


# ---- Nexar pricing/stock enrichment ------------------------------------

def test_nexar_search_surfaces_pricing_stock_and_lifecycle(monkeypatch):
    """NexarProvider's query carries pricing/stock/lifecycle per Nexar's
    published Supply examples; this checks it reaches PartHit.extra and
    PartHit.to_dict(), not just that the query string changed."""
    monkeypatch.setenv("NEXAR_CLIENT_ID", "id")
    monkeypatch.setenv("NEXAR_CLIENT_SECRET", "secret")
    # One combined body answers both calls NexarProvider.search() makes:
    # _token() reads "access_token" directly off the payload; search()
    # reads "data" off the same payload. Each ignores the other's key.
    _respond(monkeypatch, {
        "access_token": "tok", "expires_in": 86400,
        "data": {"supSearchMpn": {"results": [{"part": {
            "mpn": "NE555P", "manufacturer": {"name": "TI"},
            "shortDescription": "Timer IC", "bestDatasheet": {"url": "https://example.invalid/ne555.pdf"},
            "totalAvail": 123456,
            "medianPrice1000": {"quantity": 1000, "convertedPrice": 0.08},
            "sellers": [{"company": {"name": "Mouser"}, "offers": [
                {"inventoryLevel": 500, "prices": [{"quantity": 1, "price": 0.5, "currency": "USD"}]}]}],
            "specs": [
                {"attribute": {"shortname": "lifecyclestatus",
                               "name": "Lifecycle Status"},
                 "displayValue": "Active"},
                {"attribute": {"shortname": "supplyvoltage",
                               "name": "Supply Voltage"},
                 "displayValue": "4.5 V ~ 16 V"},
            ],
            "similarParts": [{"mpn": "NE555D", "manufacturer": {"name": "TI"}}],
        }}]}},
    })

    hits = NexarProvider().search("NE555")
    assert [h.mpn for h in hits] == ["NE555P"]
    extra = hits[0].extra
    assert extra["total_available"] == 123456
    assert extra["median_price_1000"] == {"quantity": 1000, "convertedPrice": 0.08}
    assert extra["best_unit_price"] == {"price": 0.5, "currency": "USD", "seller": "Mouser"}
    assert extra["lifecycle"] == "Active"
    assert extra["authorized_sellers"] == 1
    assert extra["similar_parts"] == [{"mpn": "NE555D", "manufacturer": "TI"}]
    assert hits[0].to_dict()["extra"] == extra


def test_nexar_publishes_the_whole_parametric_map_not_just_lifecycle(monkeypatch):
    """The query has always asked for every spec and the parser has
    always built the map; only lifecyclestatus was published and the
    rest was dropped. That map IS the input to a parametric comparison,
    so losing it made the richest source the least useful one."""
    extra = NexarProvider._pricing_extra({
        "specs": [
            {"attribute": {"shortname": "lifecyclestatus",
                           "name": "Lifecycle Status"},
             "displayValue": "Active"},
            {"attribute": {"shortname": "capacitance",
                           "name": "Capacitance"}, "displayValue": "100 nF"},
            {"attribute": {"shortname": "tolerance"}, "displayValue": "10%"},
        ],
    })
    assert extra["parameters"] == {"lifecyclestatus": "Active",
                                   "capacitance": "100 nF",
                                   "tolerance": "10%"}
    # The human label is kept beside the key, not instead of it: the key
    # is what two parts are compared on, the label is what a report says.
    assert extra["parameter_names"] == {"lifecyclestatus": "Lifecycle Status",
                                        "capacitance": "Capacitance"}
    assert extra["lifecycle"] == "Active"


def test_nexar_keeps_every_similar_part_and_keeps_the_two_halves_apart():
    """Flattening to "<manufacturer> <mpn>" cannot be undone: either half
    may contain a space, so no split of the string is safe. The cap of
    five went with it -- truncating a candidate set in silence is how the
    best replacement goes missing."""
    extra = NexarProvider._pricing_extra({"similarParts": [
        {"mpn": f"PART-{i}", "manufacturer": {"name": "Texas Instruments"}}
        for i in range(9)
    ]})
    assert len(extra["similar_parts"]) == 9
    assert extra["similar_parts"][0] == {"mpn": "PART-0",
                                         "manufacturer": "Texas Instruments"}


def test_nexar_pricing_extra_degrades_rather_than_raises_on_missing_fields():
    """A bare-minimum part payload (no sellers/specs/similarParts) must
    still produce a hit with empty-but-present extra fields."""
    extra = NexarProvider._pricing_extra({"mpn": "X", "manufacturer": {"name": "Y"}})
    assert extra == {
        "total_available": None, "median_price_1000": None,
        "best_unit_price": None, "lifecycle": "", "authorized_sellers": 0,
        "parameters": {}, "parameter_names": {}, "similar_parts": [],
    }


def test_a_hit_with_no_extra_data_keeps_to_dict_unchanged(monkeypatch):
    """Providers that never set extra must not suddenly carry an empty key."""
    monkeypatch.setenv("MOUSER_API_KEY", "k")
    _respond(monkeypatch, {"SearchResults": {"Parts": [
        {"ManufacturerPartNumber": "NE555P", "Manufacturer": "TI"}]}})
    hit = MouserProvider().search("NE555")[0]
    assert "extra" not in hit.to_dict()


# ---- fetch ------------------------------------------------------------

def test_fetch_says_plainly_that_there_is_nothing_to_place(monkeypatch):
    monkeypatch.setenv("MOUSER_API_KEY", "k")
    _respond(monkeypatch, {"SearchResults": {"Parts": [
        {"ManufacturerPartNumber": "NE555P", "Manufacturer": "TI",
         "DataSheetUrl": "https://example.invalid/ne555.pdf"}]}})

    detail = MouserProvider().fetch("NE555P")
    assert detail["kind"] == "catalogue"
    assert detail["files"] == {}
    assert "NOT a symbol or footprint" in detail["note"]


def test_fetch_of_an_absent_part_is_an_error_not_a_blank(monkeypatch):
    monkeypatch.setenv("MOUSER_API_KEY", "k")
    _respond(monkeypatch, {"SearchResults": {"Parts": []}})

    with pytest.raises(ProviderError):
        MouserProvider().fetch("NOSUCHPART")


# ---- the base class contract -----------------------------------------

def test_the_base_refuses_to_be_used_directly():
    """A subclass that forgets search must fail loudly, not silently."""
    class Incomplete(DistributorProvider):
        name = "incomplete"
        env_vars = ()

    with pytest.raises(NotImplementedError):
        Incomplete().search("x")


# ---- lifecycle / stock / parameters across the other sources -----------
#
# Each class description used to promise "lifecycle and stock" and three
# of the four parsers asked for neither, so the promise was the thing
# that was wrong. These cover the parsers that now back it, and the
# guard at the bottom keeps a description from drifting ahead of its
# parser again.

def test_digikey_lifts_lifecycle_stock_and_parameters():
    extra = DigiKeyProvider._catalogue_extra({
        "ProductStatus": {"Id": 0, "Status": "Active"},
        "QuantityAvailable": 42000,
        "Parameters": [
            {"ParameterText": "Capacitance", "ValueText": "100 nF"},
            {"ParameterText": "Tolerance", "ValueText": "10%"},
        ],
    })
    assert extra == {"lifecycle": "Active", "total_available": 42000,
                     "parameters": {"Capacitance": "100 nF",
                                    "Tolerance": "10%"}}


def test_mouser_parses_its_free_text_availability_and_keeps_the_original():
    """Mouser states stock as prose. The digits are lifted so a number
    exists to compare, and the words are kept so a figure that did not
    parse cleanly can still be read by a human."""
    extra = MouserProvider._catalogue_extra({
        "LifecycleStatus": "Not Recommended for New Designs",
        "Availability": "1,234 In Stock",
        "ProductAttributes": [
            {"AttributeName": "Voltage Rating", "AttributeValue": "50 V"},
        ],
    })
    assert extra["lifecycle"] == "Not Recommended for New Designs"
    assert extra["total_available"] == 1234
    assert extra["availability_text"] == "1,234 In Stock"
    assert extra["parameters"] == {"Voltage Rating": "50 V"}


def test_element14_lifts_stock_level_and_units_onto_its_attributes():
    extra = Element14Provider._catalogue_extra({
        "stock": {"level": 7},
        "attributes": [
            {"attributeLabel": "Capacitance", "attributeValue": "100",
             "attributeUnit": "nF"},
            {"attributeLabel": "Dielectric", "attributeValue": "X7R"},
        ],
    })
    assert extra["total_available"] == 7
    assert extra["parameters"] == {"Capacitance": "100 nF",
                                   "Dielectric": "X7R"}
    # No lifecycle: element14's status vocabulary does not map onto the
    # others, so it is left out rather than guessed at.
    assert "lifecycle" not in extra


@pytest.mark.parametrize("cls,parser", [
    (DigiKeyProvider, "_catalogue_extra"),
    (MouserProvider, "_catalogue_extra"),
    (Element14Provider, "_catalogue_extra"),
])
def test_a_missing_figure_is_an_absent_key_not_a_zero(cls, parser):
    """"No stock figure" and "none in stock" are different answers, and
    only one of them should stop a design. A parser that defaulted to 0
    would make the first unreadable."""
    extra = getattr(cls, parser)({})
    assert extra == {}
    assert "total_available" not in extra


def test_no_provider_description_promises_data_its_parser_never_reads():
    """The defect this section came out of: four of five descriptions
    advertised lifecycle or stock, and only Nexar's parser asked for
    any. A description is the only thing a caller reads before choosing
    a source, so it drifting ahead of the parser is not cosmetic."""
    import inspect

    offenders = []
    for cls in ALL_DISTRIBUTORS:
        source = inspect.getsource(cls)
        description = cls.description.lower()
        # "NO stock" / "does not" are how a class states an absence; the
        # check is for an unbacked PROMISE, not for the word appearing.
        promises_stock = ("stock" in description
                          and "no stock" not in description)
        promises_lifecycle = ("lifecycle" in description
                              and "no lifecycle" not in description)
        if promises_stock and "total_available" not in source:
            offenders.append(f"{cls.name}: description says stock, parser "
                             f"never sets total_available")
        if promises_lifecycle and '"lifecycle"' not in source:
            offenders.append(f"{cls.name}: description says lifecycle, "
                             f"parser never sets lifecycle")
    assert not offenders, "\n  ".join([""] + offenders)
