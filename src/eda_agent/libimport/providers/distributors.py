# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Distributor and aggregator catalogues.

Five sources of part IDENTITY: manufacturer part number, datasheet,
and -- from four of the five -- lifecycle, stock and the parametric
map. None of them yields a symbol or a footprint, which is why they all
declare ``kind = "catalogue"`` and no importable format.

WHO ANSWERS WHAT, since this used to be stated once for all five and
was wrong for four of them. Digi-Key, Mouser and Nexar return lifecycle,
stock and parameters; element14 returns stock and parameters but no
lifecycle; TME's Search.json returns neither, because stock lives behind
Products/GetPrices, which this client does not call. Each class says so
itself, and the shared key names are listed in
``_distributor.EXTRA_KEYS``. An absent figure is an ABSENT KEY, never a
zero: "no stock figure" and "none in stock" must not read alike.

They matter because of what this project measures everything against.
``lib_audit_footprint_vs_datasheet`` and the design checks all want a
datasheet URL, and until now nothing here could find one: the library
providers know a symbol's name but not the part's paperwork.

Every endpoint below was probed live before being written down. The
comment on each class records what it answered, because "I checked" is
worth nothing without the result. Three further candidates were probed
and DROPPED for answering 404 on the recalled URL rather than being
shipped as plausible guesses.

WHAT IS AND IS NOT VERIFIED HERE. The endpoints exist: measured. The
auth mechanisms follow each vendor's published scheme. The response
parsing has NOT been exercised against a live credentialed reply by this
project, which every class states through ``verified_live = False`` and
which the provider catalogue reports. The parsers are written to survive
a renamed field rather than assume one, so a shape drift degrades a hit
instead of losing the whole search.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import urllib.parse
from typing import Any

from eda_agent.libimport.providers._distributor import (
    DistributorProvider,
    first_string,
)
from eda_agent.libimport.providers.base import PartHit

__all__ = [
    "DigiKeyProvider",
    "Element14Provider",
    "MouserProvider",
    "NexarProvider",
    "TmeProvider",
]


class DigiKeyProvider(DistributorProvider):
    """Digi-Key's product search.

    Probed: ``POST /products/v4/search/keyword`` answered HTTP 400 to an
    unauthenticated request and ``POST /v1/oauth2/token`` answered 400 to
    an empty grant, so both paths exist and reject rather than 404.
    """

    name = "digikey"
    description = (
        "Digi-Key catalogue: MPN, datasheet, lifecycle and live stock. "
        "Identity only, no symbol or footprint. Needs an OAuth client "
        "from the Digi-Key developer portal via DIGIKEY_CLIENT_ID and "
        "DIGIKEY_CLIENT_SECRET.")
    env_vars = ("DIGIKEY_CLIENT_ID", "DIGIKEY_CLIENT_SECRET")

    _TOKEN_URL = "https://api.digikey.com/v1/oauth2/token"
    _SEARCH_URL = "https://api.digikey.com/products/v4/search/keyword"

    def _token(self) -> str:
        creds = self._credentials()
        body = urllib.parse.urlencode({
            "client_id": creds["DIGIKEY_CLIENT_ID"],
            "client_secret": creds["DIGIKEY_CLIENT_SECRET"],
            "grant_type": "client_credentials",
        }).encode("ascii")
        payload = self._request(
            self._TOKEN_URL, method="POST", body=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        token = first_string(payload, ("access_token",))
        if not token:
            raise self._no_token()
        return token

    def _no_token(self):
        from eda_agent.libimport.providers.base import ProviderUnavailable
        return ProviderUnavailable(
            f"{self.name} accepted the credential exchange but returned no "
            f"access_token")

    def search(self, query: str, limit: int = 20) -> list[PartHit]:
        token = self._token()
        creds = self._credentials()
        body = json.dumps({
            "Keywords": str(query),
            "Limit": int(limit),
            "Offset": 0,
        }).encode("utf-8")
        payload = self._request(
            self._SEARCH_URL, method="POST", body=body,
            headers={
                "Authorization": f"Bearer {token}",
                "X-DIGIKEY-Client-Id": creds["DIGIKEY_CLIENT_ID"],
                "Content-Type": "application/json",
            })
        products = payload.get("Products") or payload.get("products") or []
        hits = []
        for product in products[:limit]:
            mpn = first_string(product,
                               ("ManufacturerProductNumber",),
                               ("ManufacturerPartNumber",),
                               ("Description", "ProductDescription"))
            if not mpn:
                continue
            hits.append(self._hit(
                mpn,
                mpn=mpn,
                manufacturer=first_string(product,
                                          ("Manufacturer", "Name"),
                                          ("Manufacturer", "Value")),
                description=first_string(
                    product, ("Description", "ProductDescription"),
                    ("DetailedDescription",)),
                datasheet=first_string(product, ("DatasheetUrl",),
                                       ("PrimaryDatasheet",)),
                extra=self._catalogue_extra(product),
            ))
        return hits

    @staticmethod
    def _catalogue_extra(product: dict) -> dict[str, Any]:
        """Lifecycle, stock and parameters, in the shared EXTRA_KEYS shape.

        The class description has always promised "lifecycle and live
        stock" and the parser asked for neither, so the promise was
        wrong rather than the parser. Reading them is the fix that keeps
        the description true.

        Field names are Digi-Key's v4 product shape; like every other
        response mapping in this module they are UNVERIFIED against a
        credentialed reply (``verified_live = False``), so each one is
        read defensively and an absent field omits its key instead of
        publishing a zero that would read as "none in stock".
        """
        extra: dict[str, Any] = {}
        status = first_string(product, ("ProductStatus", "Status"),
                              ("ProductStatus",))
        if status:
            extra["lifecycle"] = status
        quantity = product.get("QuantityAvailable")
        # bool is an int in Python, so a True here would publish as one
        # unit in stock rather than as the shape error it is.
        if isinstance(quantity, (int, float)) and not isinstance(
                quantity, bool):
            extra["total_available"] = int(quantity)
        parameters = {}
        for row in product.get("Parameters") or []:
            if not isinstance(row, dict):
                continue
            key = first_string(row, ("ParameterText",), ("Parameter",))
            value = first_string(row, ("ValueText",), ("Value",))
            if key:
                parameters[key] = value
        if parameters:
            extra["parameters"] = parameters
        return extra


class MouserProvider(DistributorProvider):
    """Mouser's keyword search.

    Probed: ``POST /api/v1/search/keyword`` answered HTTP **200** with
    ``{"Errors":[{"Code":"Invalid",...}]}`` to a bogus key. That is the
    reason ``_reject_error_payload`` exists in the base class: judged on
    status alone, a rejected credential here is indistinguishable from a
    search that ran and matched nothing.
    """

    name = "mouser"
    description = (
        "Mouser catalogue: MPN, datasheet, lifecycle and stock. Identity "
        "only, no symbol or footprint. Needs a search API key from the "
        "Mouser developer portal via MOUSER_API_KEY.")
    env_vars = ("MOUSER_API_KEY",)

    _SEARCH_URL = "https://api.mouser.com/api/v1/search/keyword"

    def search(self, query: str, limit: int = 20) -> list[PartHit]:
        creds = self._credentials()
        body = json.dumps({
            "SearchByKeywordRequest": {
                "keyword": str(query),
                "records": int(limit),
                "startingRecord": 0,
            }
        }).encode("utf-8")
        payload = self._request(
            self._query(self._SEARCH_URL,
                        {"apiKey": creds["MOUSER_API_KEY"]}),
            method="POST", body=body,
            headers={"Content-Type": "application/json"})
        results = (payload.get("SearchResults") or {})
        parts = results.get("Parts") or []
        hits = []
        for part in parts[:limit]:
            mpn = first_string(part, ("ManufacturerPartNumber",))
            if not mpn:
                continue
            hits.append(self._hit(
                mpn,
                mpn=mpn,
                manufacturer=first_string(part, ("Manufacturer",)),
                description=first_string(part, ("Description",)),
                datasheet=first_string(part, ("DataSheetUrl",)),
                extra=self._catalogue_extra(part),
            ))
        return hits

    @staticmethod
    def _catalogue_extra(part: dict) -> dict[str, Any]:
        """Lifecycle, stock and attributes, in the shared EXTRA_KEYS shape.

        Same correction as Digi-Key's: the class description promised
        "lifecycle and stock" that the parser never read. Mouser states
        availability as free text ("1,234 In Stock"), so the digits are
        lifted out and the original string is kept alongside rather than
        thrown away -- a figure that cannot be parsed must not silently
        become a number that can.

        UNVERIFIED against a credentialed reply, like the rest of this
        module (``verified_live = False``).
        """
        extra: dict[str, Any] = {}
        lifecycle = first_string(part, ("LifecycleStatus",),
                                 ("ProductStatus",))
        if lifecycle:
            extra["lifecycle"] = lifecycle
        availability = first_string(part, ("AvailabilityInStock",),
                                    ("Availability",))
        if availability:
            extra["availability_text"] = availability
            digits = "".join(c for c in availability.split(" ")[0]
                             if c.isdigit())
            if digits:
                extra["total_available"] = int(digits)
        parameters = {}
        for row in part.get("ProductAttributes") or []:
            if not isinstance(row, dict):
                continue
            key = first_string(row, ("AttributeName",))
            value = first_string(row, ("AttributeValue",))
            if key:
                parameters[key] = value
        if parameters:
            extra["parameters"] = parameters
        return extra


class NexarProvider(DistributorProvider):
    """Nexar, the API behind Octopart.

    Probed: ``POST https://api.nexar.com/graphql`` answered a valid
    GraphQL body to an unauthenticated ``{__typename}`` introspection,
    and ``https://identity.nexar.com/connect/token`` answered 400 to an
    empty grant. Both exist; real part queries need the token.
    """

    name = "nexar"
    description = (
        "Nexar (Octopart) aggregator: MPN, datasheet and offers across "
        "many distributors at once. Identity only, no symbol or "
        "footprint. Needs NEXAR_CLIENT_ID and NEXAR_CLIENT_SECRET.")
    env_vars = ("NEXAR_CLIENT_ID", "NEXAR_CLIENT_SECRET")

    _TOKEN_URL = "https://identity.nexar.com/connect/token"
    _API_URL = "https://api.nexar.com/graphql"

    #: Pricing, stock and lifecycle fields per Nexar's published Supply
    #: query examples (support.nexar.com); not yet exercised against the
    #: live schema with a real credential (verified_live stays False), so
    #: a renamed or absent field degrades that one hit's extra data
    #: rather than failing the search (see _pricing_extra).
    _PART_FIELDS = """
      mpn
      manufacturer { name }
      shortDescription
      bestDatasheet { url }
      totalAvail
      medianPrice1000 { quantity convertedPrice }
      sellers(authorizedOnly: true) {
        company { name }
        offers { inventoryLevel prices { quantity price currency } }
      }
      specs { attribute { shortname name } displayValue }
      similarParts { mpn manufacturer { name } }
    """

    _QUERY = """
    query SearchMpn($q: String!, $limit: Int!) {
      supSearchMpn(q: $q, limit: $limit) {
        results {
          part {
            %s
          }
        }
      }
    }
    """ % _PART_FIELDS

    def _token(self) -> str:
        creds = self._credentials()
        body = urllib.parse.urlencode({
            "client_id": creds["NEXAR_CLIENT_ID"],
            "client_secret": creds["NEXAR_CLIENT_SECRET"],
            "grant_type": "client_credentials",
        }).encode("ascii")
        payload = self._request(
            self._TOKEN_URL, method="POST", body=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        token = first_string(payload, ("access_token",))
        if not token:
            from eda_agent.libimport.providers.base import ProviderUnavailable
            raise ProviderUnavailable(
                f"{self.name} returned no access_token")
        return token

    @staticmethod
    def _pricing_extra(part: dict) -> dict[str, Any]:
        """Pricing, stock, lifecycle and parametric data for PartHit.extra.

        Read defensively (every field optional, no raw indexing) for the
        same reason first_string walks several paths: a renamed or
        absent field here must shrink this one hit's extra data, not
        raise and lose the hit.

        The query has always asked for the full ``specs`` list and this
        method has always built the map, but until now it published only
        ``lifecyclestatus`` out of it and dropped the rest on the floor.
        The whole map is what a parametric comparison runs on, so it is
        published as ``parameters`` (see _distributor.EXTRA_KEYS).
        """
        specs = {}
        labels = {}
        for s in part.get("specs") or []:
            attr = s.get("attribute") or {}
            key = attr.get("shortname") or attr.get("name")
            if key:
                specs[key] = s.get("displayValue", "")
                label = attr.get("name") or attr.get("shortname")
                if label and label != key:
                    labels[key] = label
        sellers = part.get("sellers") or []
        best_price = None
        for seller in sellers:
            for offer in seller.get("offers") or []:
                for price in offer.get("prices") or []:
                    if price.get("quantity") == 1 and (
                            best_price is None
                            or price.get("price", float("inf")) < best_price["price"]):
                        company = (seller.get("company") or {}).get("name")
                        best_price = {"price": price.get("price"),
                                      "currency": price.get("currency"),
                                      "seller": company}
        # Kept as a pair rather than flattened to "<manufacturer> <mpn>":
        # a replacement search has to look up the MPN on its own, and no
        # split of that string is safe when either half can contain a
        # space. The cap of 5 went with it, since Nexar bounds the list
        # itself and truncating a candidate set silently is how the best
        # answer goes missing.
        similar = [
            {"mpn": str(x.get("mpn") or "").strip(),
             "manufacturer": str(
                 (x.get("manufacturer") or {}).get("name") or "").strip()}
            for x in (part.get("similarParts") or [])
        ]
        similar = [x for x in similar if x["mpn"] or x["manufacturer"]]
        return {
            "total_available": part.get("totalAvail"),
            "median_price_1000": part.get("medianPrice1000"),
            "best_unit_price": best_price,
            "lifecycle": specs.get("lifecyclestatus", ""),
            "authorized_sellers": len(sellers),
            "parameters": specs,
            "parameter_names": labels,
            "similar_parts": similar,
        }

    def search(self, query: str, limit: int = 20) -> list[PartHit]:
        token = self._token()
        body = json.dumps({
            "query": self._QUERY,
            "variables": {"q": str(query), "limit": int(limit)},
        }).encode("utf-8")
        payload = self._request(
            self._API_URL, method="POST", body=body,
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"})
        results = (((payload.get("data") or {}).get("supSearchMpn") or {})
                   .get("results") or [])
        hits = []
        for entry in results[:limit]:
            part = (entry or {}).get("part") or {}
            mpn = first_string(part, ("mpn",))
            if not mpn:
                continue
            hits.append(self._hit(
                mpn,
                mpn=mpn,
                manufacturer=first_string(part, ("manufacturer", "name")),
                description=first_string(part, ("shortDescription",)),
                datasheet=first_string(part, ("bestDatasheet", "url")),
                extra=self._pricing_extra(part),
            ))
        return hits


class Element14Provider(DistributorProvider):
    """element14 / Farnell / Newark product search.

    Probed: ``GET /catalog/products`` answered HTTP 403 with the body
    ``Developer Inactive``, so the path exists and gates on the key.
    """

    name = "element14"
    description = (
        "element14 (Farnell, Newark) catalogue: MPN, datasheet and "
        "stock. Identity only, no symbol or footprint. Needs a product "
        "search key via ELEMENT14_API_KEY; set ELEMENT14_STORE to pick "
        "a regional store.")
    env_vars = ("ELEMENT14_API_KEY",)

    _SEARCH_URL = "https://api.element14.com/catalog/products"
    #: Regional storefront. element14 serves different catalogues per
    #: store, so this changes which parts and prices come back.
    _DEFAULT_STORE = "uk.farnell.com"

    def search(self, query: str, limit: int = 20) -> list[PartHit]:
        import os

        creds = self._credentials()
        store = os.environ.get("ELEMENT14_STORE", "").strip() \
            or self._DEFAULT_STORE
        url = self._query(self._SEARCH_URL, {
            "term": f"any:{query}",
            "storeInfo.id": store,
            "callInfo.apiKey": creds["ELEMENT14_API_KEY"],
            "callInfo.responseDataFormat": "json",
            "resultsSettings.numberOfResults": str(int(limit)),
            "resultsSettings.offset": "0",
            "resultsSettings.responseGroup": "large",
        })
        payload = self._request(url)
        products = ((payload.get("premierFarnellPartNumberReturn") or {})
                    .get("products") or [])
        hits = []
        for product in products[:limit]:
            mpn = first_string(product, ("translatedManufacturerPartNumber",),
                               ("sku",))
            if not mpn:
                continue
            datasheets = product.get("datasheets") or []
            datasheet = ""
            if isinstance(datasheets, list) and datasheets:
                datasheet = first_string(datasheets[0], ("url",))
            hits.append(self._hit(
                mpn,
                mpn=mpn,
                manufacturer=first_string(product, ("brandName",),
                                          ("vendorName",)),
                description=first_string(
                    product, ("displayName",),
                    ("productOverview", "description")),
                datasheet=datasheet,
                extra=self._catalogue_extra(product),
            ))
        return hits

    @staticmethod
    def _catalogue_extra(product: dict) -> dict[str, Any]:
        """Stock and attributes, in the shared EXTRA_KEYS shape.

        The search already asks for ``responseGroup=large``, which is
        what carries ``stock`` and ``attributes``; both arrived and were
        dropped, leaving the class description's "and stock" unbacked.

        No lifecycle: element14 publishes a product status in its own
        vocabulary that does not map onto the others cleanly, so it is
        left out rather than guessed at. UNVERIFIED, as everywhere here.
        """
        extra: dict[str, Any] = {}
        stock = product.get("stock")
        level = stock.get("level") if isinstance(stock, dict) else None
        if isinstance(level, (int, float)) and not isinstance(level, bool):
            extra["total_available"] = int(level)
        parameters = {}
        for row in product.get("attributes") or []:
            if not isinstance(row, dict):
                continue
            key = first_string(row, ("attributeLabel",))
            value = first_string(row, ("attributeValue",))
            unit = first_string(row, ("attributeUnit",))
            if key:
                parameters[key] = f"{value} {unit}".strip() if unit else value
        if parameters:
            extra["parameters"] = parameters
        return extra


class TmeProvider(DistributorProvider):
    """TME (Transfer Multisort Elektronik) product search.

    Probed: ``POST /Products/Search.json`` answered HTTP 403 with
    ``{"Status":"E_ACTION_FORBIDDEN"}``, so the path exists and gates on
    the signed request.

    TME signs every call: HMAC-SHA1 over the method, the URL and the
    sorted parameters, base64 encoded. That is implemented here but, like
    every parser in this module, has not been exercised against a live
    credentialed reply.
    """

    name = "tme"
    description = (
        "TME catalogue: MPN and product page, strongest on European "
        "availability. Identity only, no symbol or footprint, and NO "
        "stock figure: Search.json does not return one, it comes from "
        "Products/GetPrices, which this client does not call. Needs "
        "TME_TOKEN and TME_SECRET; requests are HMAC signed.")
    env_vars = ("TME_TOKEN", "TME_SECRET")

    _SEARCH_URL = "https://api.tme.eu/Products/Search.json"

    def _sign(self, url: str, params: dict[str, str], secret: str) -> str:
        """TME's HMAC-SHA1 request signature.

        The signed string is METHOD&url&params, each percent-encoded as a
        whole, with parameters sorted by key.
        """
        encoded = urllib.parse.urlencode(sorted(params.items()))
        base = "&".join([
            "POST",
            urllib.parse.quote(url, safe=""),
            urllib.parse.quote(encoded, safe=""),
        ])
        digest = hmac.new(secret.encode("utf-8"), base.encode("utf-8"),
                          hashlib.sha1).digest()
        return base64.b64encode(digest).decode("ascii")

    def search(self, query: str, limit: int = 20) -> list[PartHit]:
        import os

        creds = self._credentials()
        params = {
            "Token": creds["TME_TOKEN"],
            "SearchPlain": str(query),
            "Country": os.environ.get("TME_COUNTRY", "").strip() or "GB",
            "Language": "EN",
        }
        params["ApiSignature"] = self._sign(
            self._SEARCH_URL, params, creds["TME_SECRET"])
        body = urllib.parse.urlencode(params).encode("utf-8")
        payload = self._request(
            self._SEARCH_URL, method="POST", body=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        products = ((payload.get("Data") or {}).get("ProductList") or [])
        hits = []
        for product in products[:limit]:
            mpn = first_string(product, ("OriginalSymbol",), ("Symbol",))
            if not mpn:
                continue
            hits.append(self._hit(
                mpn,
                mpn=mpn,
                manufacturer=first_string(product, ("Producer",)),
                description=first_string(product, ("Description",)),
                # TME returns documents from a separate endpoint; the
                # search reply carries a product page rather than a
                # datasheet. Stated as a page, not passed off as one.
                datasheet=first_string(product, ("ProductInformationPage",)),
            ))
        return hits
