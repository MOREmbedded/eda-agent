---
name: part-docs
description: Download a component's official documents (datasheet, package drawing and land pattern, application notes, STEP 3D model) from the manufacturer's own site, unattended, into the design workspace's .eda folder. Apply whenever a datasheet, package drawing, footprint source, app note or 3D model is needed for a part number, before building a symbol, footprint or 3D body, checking a land pattern, or citing a value. Never ask the person to download or click anything.
---

# Part documents from the manufacturer (eda-agent)

Get the paperwork for a part number yourself. The person does not
download, click, or save anything, and is never asked to.

## Do this

1. Know the design workspace: the folder that holds the projects and its
   `.eda` folder (for example `D:\workspace_<client>_hw`). Files go under
   its `.eda`. Never use a folder on the system drive for documents,
   temp files or browser profiles.
2. Fetch:
   - MCP tool, when the eda-agent server is connected:
     `part_fetch_docs(mpn="QPA9903", workspace="<design workspace>")`
   - otherwise the command line:
     ```
     eda-agent part-fetch-docs QPA9903 --workspace "<design workspace>" --json
     ```
     (`python -m eda_agent.server part-fetch-docs ...` does the same.)
3. Read the result:
   - `datasheet`: the PDF path. `files`: every file kept, each with its
     `kind` (datasheet, package, appnote, model3d), source `url`, `via`
     (http or browser), `sha256` and `title`.
   - `missing`: what was looked for and not found, and why.
   - `notes`: for example that a 3D model is only offered through
     SamacSys or Ultra Librarian (sign-in portals, not fetched).
4. Use the files: `datasheet_find_pages` finds the package drawing and
   land pattern pages, `datasheet_view_page` zooms into a drawing. Cite
   the URL, revision and page numbers.

A second call for the same part is a cache hit and costs nothing. Pass
`refresh` (`--refresh`) only to pick up a new revision.

## Where the files are

```
<workspace>\.eda\datasheets\<MPN>.pdf            the datasheet
<workspace>\.eda\datasheets\<MPN>\package-*.pdf  package drawings
<workspace>\.eda\datasheets\<MPN>\appnote-*.pdf  application notes
<workspace>\.eda\models\<MPN>\*.step             STEP models
<workspace>\.eda\datasheets\<MPN>.json           sources, hashes, what was missing
<workspace>\.eda\browser-profile\                the browser's own profile
```

`#` and other characters unsafe in file names become `_`
(`LTC3780EGN#PBF` is kept as `LTC3780EGN_PBF`).

## How it gets the files

- The manufacturer's site first: known product-page and datasheet URL
  patterns for Texas Instruments, Analog Devices (and Linear, Maxim),
  Qorvo, Microchip and ST. For any other maker, the Nexar (Octopart),
  Digi-Key, Mouser or element14 catalogue gives the manufacturer and its
  datasheet URL, when their credentials are set.
- Plain HTTP with an honest User-Agent first. A site that refuses it
  (HTTP 429 or 403, a JavaScript check page, no answer, as Qorvo and
  Analog Devices do) is read by a real Edge or Chrome window placed off
  screen. The browser runs the site's own scripts as it would for a
  person, and the file is taken from the response in memory: no Save-as
  dialog, nothing in the Downloads folder.
- Every file is checked by its bytes (`%PDF`, `ISO-10303-21`); a sign-in
  or check page saved under a .pdf name is refused.

## When it does not find everything

- The maker was not recognised: pass `manufacturer` (`--manufacturer`),
  or the product page with `product_url` (`--product-url`).
- `notes` says a site asked for a person (a captcha, a press-and-hold
  check): that is never answered. Try again later, or use the catalogue
  datasheet URL with `datasheet_fetch`. Do not hand it to the person.
- No STEP model: most makers publish models only through sign-in
  portals. Build the body from the package drawing with
  `lib_create_3d_model` (Altium) instead.
- No separate package drawing: the datasheet's mechanical section is the
  source (TI and Qorvo put it there).
- Two package drawings (a part offered in two packages): pick the one
  whose package code matches the orderable MPN.

## Rules

- Never ask the person to download, click, save or sign in.
- Use normal browser fetching only. No captcha solving, no forged
  browser identity, no sign-in on the person's behalf.
- Nothing on the system drive: documents, temp files and the browser
  profile stay in the design workspace (or `EDA_AGENT_DOCS_ROOT`,
  `EDA_AGENT_BROWSER_PROFILE` when set).
