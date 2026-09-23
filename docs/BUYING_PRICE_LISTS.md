# Supplier Excel imports

Configure each supplier explicitly through the Buying supplier API. `PriceListParser`
is the replaceable interface. `ColumnPriceListParser` with `columns-v1` implements
stable supplier layouts: worksheet number, first data row, name/price columns and
optional SKU column. No universal layout guessing, automatic fuzzy choice or macros.
The worksheet number currently identifies XLSX `sheetN.xml`, not a displayed tab name.
`.xls` and `.xlsm` are rejected; resave as values-only `.xlsx`.

Upload raw binary with a plain filename. The reader never extracts files to disk,
executes formulas or follows external workbook links. Limits: 2 MiB compressed,
20 MiB expanded, 200 ZIP entries, 5000 data rows. Duplicate archive members,
traversal paths, DTD/entities, external relationships and VBA are rejected.
Formula-containing rows are invalid even when Excel stores a cached value.
All prices parse through Decimal to integer minor units; negative/NaN/infinite,
fractional minor units and duplicate supplier mapping keys are invalid.

Preview persists filename, supplier/currency/parser version, parsed rows and counts:
`total,valid,invalid,new,changed,unchanged,ambiguous`. Workbook bytes are not retained
or logged. Parsed source names/prices remain internal import metadata.
Mapping order is existing supplier SKU/name mapping, exact normalized canonical name,
then local canonical creation. Multiple exact canonical names require a manual
candidate choice in `mappings` on confirm. There is no live MoySklad product creation.
Register existing catalog product/offer identities before the first import when known.

Confirm is one transaction and uses a PostgreSQL workspace advisory lock. Any invalid
row blocks the whole import. Ambiguous rows require listed candidate IDs. If an offer
changed since preview, confirm rejects it and asks for a fresh preview. Retrying an
already imported ID returns its original result. Supplier mapping uniqueness prevents
duplicates. Imports are incremental; missing products stay active. Every changed
offer price records old/new price, currency, timestamp and source import ID. Updates
through the internal supply API also record history (without an import ID).

For another stable supplier layout, configure another `ParserConfig`. For genuinely
different workbook semantics, implement `PriceListParser` and explicit version
dispatch with fixtures before enabling it; never silently reinterpret a supplier file.
